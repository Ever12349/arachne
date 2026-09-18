"""Orchestrate QPS → cache → fetch/extract → truncate for a single URL."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.antibot import fetch_with_retries, headers_for_upstream, raise_if_challenge
from app.cache import ResultCache, cache_key, session_fingerprint
from app.errors import (
    INTERNAL,
    ArachneError,
    fetch_failed,
    rate_limited,
    unauthorized_upstream,
    unsupported_content,
)
from app.extract import apply_max_chars, extract_html
from app.fetch import FetchedPage, is_html_content_type
from app.headers_policy import filter_request_headers
from app.limits import FixedWindowRateLimiter, Stats
from app.models import ExtractResponse
from app.render import render_url
from app.sessions import load_session, merge_session_material

logger = logging.getLogger("arachne")

_SESSION_PREFIX_LEN = 12


def _reject_error_status(page: FetchedPage) -> None:
    if page.status_code < 400:
        return
    detail = {"status_code": page.status_code}
    if page.status_code in {401, 403}:
        raise unauthorized_upstream(
            f"Upstream returned {page.status_code}",
            detail,
        )
    raise fetch_failed(f"Upstream returned {page.status_code}", detail)


async def extract_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    render: bool = False,
) -> ExtractResponse:
    requested = (url or "").strip()
    if render:
        page = await render_url(requested, headers=headers, cookies=cookies)
    else:
        page = await fetch_with_retries(client, requested, headers=headers, cookies=cookies)
    raise_if_challenge(page)
    _reject_error_status(page)
    if not is_html_content_type(page.content_type):
        raise unsupported_content(
            "Unsupported content type",
            {"content_type": page.content_type, "status_code": page.status_code},
        )
    try:
        return extract_html(
            page.body,
            requested_url=requested,
            final_url=page.final_url,
            status_code=page.status_code,
            content_type=page.content_type,
        )
    except ArachneError:
        raise
    except Exception:
        logger.exception("extract failed for %s", requested)
        raise


def _log_extract(
    *,
    url: str,
    cache_status: str | None,
    latency_ms: float,
    error_code: str | None,
    session_prefix: str,
) -> None:
    # Structured fields only: never log Cookie / Authorization / cookies / session plaintext.
    logger.info(
        "extract url=%s cache=%s latency_ms=%.2f error_code=%s session=%s",
        url,
        cache_status or "-",
        latency_ms,
        error_code or "-",
        session_prefix,
    )


async def run_extract(
    *,
    url: str,
    client: httpx.AsyncClient,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    max_chars: int | None = None,
    session_id: str | None = None,
    ua_strategy: str = "default",
    render: bool = False,
    cache: ResultCache,
    limiter: FixedWindowRateLimiter,
    semaphore: asyncio.Semaphore,
    stats: Stats,
) -> ExtractResponse:
    """QPS → cache lookup → (miss) semaphore → fetch or render / extract → store → max_chars."""
    t0 = time.perf_counter()
    stats.requests_total += 1
    error_code: str | None = None
    cache_status: str | None = None
    session_prefix = "-"
    requested = (url or "").strip()
    entered_inflight = False

    def finish_log() -> None:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        stats.latency_ms_sum += latency_ms
        stats.latency_ms_count += 1
        _log_extract(
            url=requested,
            cache_status=cache_status,
            latency_ms=latency_ms,
            error_code=error_code,
            session_prefix=session_prefix,
        )

    try:
        session = load_session(session_id)
        merged = merge_session_material(session, cookies, headers)
        safe_headers = filter_request_headers(merged.headers)
        safe_cookies = dict(merged.cookies)
        fingerprint = session_fingerprint(safe_headers, safe_cookies)
        session_prefix = fingerprint[:_SESSION_PREFIX_LEN]
        strategy = ua_strategy or "default"

        if not await limiter.try_acquire():
            raise rate_limited()

        stats.in_flight += 1
        entered_inflight = True
        key = cache_key(
            requested,
            safe_headers,
            safe_cookies,
            render=render,
            ua_strategy=strategy,
        )
        cached = await cache.get(key)
        if cached is not None:
            cache_status = "hit"
            stats.cache_hits += 1
            return apply_max_chars(cached, max_chars)

        cache_status = "miss"
        stats.cache_misses += 1
        upstream_headers = headers_for_upstream(safe_headers, strategy)
        async with semaphore:
            result = await extract_page(
                requested,
                client,
                headers=upstream_headers or None,
                cookies=safe_cookies or None,
                render=render,
            )
        await cache.set(key, result)
        return apply_max_chars(result, max_chars)
    except ArachneError as exc:
        error_code = exc.code
        stats.errors_by_code[exc.code] += 1
        raise
    except Exception:
        error_code = INTERNAL
        stats.errors_by_code[INTERNAL] += 1
        logger.exception("unhandled extract error")
        raise ArachneError(INTERNAL, "Internal server error") from None
    finally:
        if entered_inflight:
            stats.in_flight -= 1
        finish_log()
