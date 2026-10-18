"""Orchestrate QPS → cache → fetch/extract → truncate for a single URL."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app import config
from app import render as render_mod
from app.antibot import fetch_with_retries, headers_for_upstream, raise_if_challenge
from app.cache import ResultCache, cache_key, session_fingerprint
from app.errors import (
    CHALLENGE_DETECTED,
    INTERNAL,
    RENDER_UNAVAILABLE,
    ArachneError,
    fetch_failed,
    unauthorized_upstream,
    unsupported_content,
)
from app.extract import apply_max_chars
from app.fetch import FetchedPage, is_html_content_type
from app.headers_policy import filter_request_headers
from app.limits import FixedWindowRateLimiter, Stats
from app.models import ExtractResponse
from app.profiles.apply import extract_with_profile
from app.profiles.loader import ProfileRegistry, resolve_profile
from app.profiles.models import SuggestResponse, SuggestStrategy
from app.profiles.suggest import suggest_from_html
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


def _accept_page(page: FetchedPage) -> None:
    raise_if_challenge(page)
    _reject_error_status(page)
    if not is_html_content_type(page.content_type):
        raise unsupported_content(
            "Unsupported content type",
            {"content_type": page.content_type, "status_code": page.status_code},
        )


async def fetch_ready_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    render: bool = False,
) -> tuple[FetchedPage, int]:
    """Fetch or render, then apply challenge / status / HTML gates.

    Returns `(page, auto_render)` where `auto_render` is 1 only after a
    successful Playwright fallback. Explicit `render=True` is browser-only.
    When `render` is false and the httpx page is `challenge_detected`, one
    render is attempted if `ARACHNE_AUTO_RENDER` is on and Playwright imports.
    Missing Playwright keeps the original `challenge_detected` (it is not
    rewritten to `render_unavailable`). Caller holds the extract semaphore
    and has already taken the single QPS slot.
    """
    requested = (url or "").strip()
    if render:
        page = await render_url(requested, headers=headers, cookies=cookies)
        _accept_page(page)
        return page, 0

    page = await fetch_with_retries(client, requested, headers=headers, cookies=cookies)
    try:
        _accept_page(page)
    except ArachneError as exc:
        if exc.code != CHALLENGE_DETECTED or not config.AUTO_RENDER:
            raise
        if not render_mod.playwright_available():
            raise
        try:
            rendered = await render_url(requested, headers=headers, cookies=cookies)
        except ArachneError as render_exc:
            if render_exc.code == RENDER_UNAVAILABLE:
                raise exc from render_exc
            raise
        _accept_page(rendered)
        return rendered, 1
    return page, 0


async def extract_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    render: bool = False,
    site_profile: str | None = None,
    profiles: ProfileRegistry | None = None,
) -> ExtractResponse:
    requested = (url or "").strip()
    page, _auto_render = await fetch_ready_page(
        requested,
        client,
        headers=headers,
        cookies=cookies,
        render=render,
    )
    return _extract_response(
        page,
        requested=requested,
        site_profile=site_profile,
        profiles=profiles,
    )


def _extract_response(
    page: FetchedPage,
    *,
    requested: str,
    site_profile: str | None,
    profiles: ProfileRegistry | None,
) -> ExtractResponse:
    try:
        profile = resolve_profile(profiles, site_profile=site_profile, url=page.final_url)
        return extract_with_profile(
            page.body,
            profile=profile,
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
    profile: str,
    qps_wait_ms: float,
    auto_render: int,
) -> None:
    # Structured fields only: never log Cookie / Authorization / cookies / session plaintext / proxy userinfo.
    logger.info(
        "extract url=%s cache=%s latency_ms=%.2f error_code=%s session=%s profile=%s qps_wait_ms=%.2f auto_render=%s",
        url,
        cache_status or "-",
        latency_ms,
        error_code or "-",
        session_prefix,
        profile or "-",
        qps_wait_ms,
        1 if auto_render else 0,
    )


async def _acquire_qps(limiter: FixedWindowRateLimiter, stats: Stats, slot: dict[str, float]) -> None:
    """One slot per run. `slot['ms']` is set even when acquire raises rate_limited."""
    try:
        slot["ms"] = await limiter.acquire()
    except ArachneError as exc:
        slot["ms"] = float(getattr(exc, "wait_ms", 0.0) or 0.0)
        stats.record_qps_wait(slot["ms"])
        raise
    stats.record_qps_wait(slot["ms"])


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
    site_profile: str | None = None,
    profiles: ProfileRegistry | None = None,
    cache: ResultCache,
    limiter: FixedWindowRateLimiter,
    semaphore: asyncio.Semaphore,
    stats: Stats,
) -> ExtractResponse:
    """QPS → cache lookup → (miss) semaphore → fetch or render / extract → store → max_chars."""
    t0 = time.perf_counter()
    stats.record_request()
    error_code: str | None = None
    cache_status: str | None = None
    session_prefix = "-"
    profile_label = "-"
    requested = (url or "").strip()
    entered_inflight = False
    qps_wait_ms = 0.0
    auto_render = 0

    def finish_log() -> None:
        latency_ms = (time.perf_counter() - t0) * 1000.0
        stats.record_latency(latency_ms)
        _log_extract(
            url=requested,
            cache_status=cache_status,
            latency_ms=latency_ms,
            error_code=error_code,
            session_prefix=session_prefix,
            profile=profile_label,
            qps_wait_ms=qps_wait_ms,
            auto_render=auto_render,
        )

    try:
        session = load_session(session_id)
        merged = merge_session_material(session, cookies, headers)
        safe_headers = filter_request_headers(merged.headers)
        safe_cookies = dict(merged.cookies)
        fingerprint = session_fingerprint(safe_headers, safe_cookies)
        session_prefix = fingerprint[:_SESSION_PREFIX_LEN]
        strategy = ua_strategy or "default"
        explicit = (site_profile or "").strip() or None
        lookup_profile = resolve_profile(profiles, site_profile=explicit, url=requested)
        lookup_id = lookup_profile.id if lookup_profile else ""
        lookup_version = lookup_profile.version if lookup_profile else ""
        profile_label = f"{lookup_id}@{lookup_version}" if lookup_id else "-"

        qps_slot = {"ms": 0.0}
        try:
            await _acquire_qps(limiter, stats, qps_slot)
        finally:
            qps_wait_ms = qps_slot["ms"]

        stats.enter_in_flight()
        entered_inflight = True
        key = cache_key(
            requested,
            safe_headers,
            safe_cookies,
            render=render,
            ua_strategy=strategy,
            profile_id=lookup_id,
            profile_version=lookup_version,
        )
        cached = await cache.get(key)
        if cached is not None:
            cache_status = "hit"
            stats.record_cache_hit()
            return apply_max_chars(cached, max_chars)

        cache_status = "miss"
        stats.record_cache_miss()
        upstream_headers = headers_for_upstream(safe_headers, strategy)
        async with semaphore:
            page, auto_render = await fetch_ready_page(
                requested,
                client,
                headers=upstream_headers or None,
                cookies=safe_cookies or None,
                render=render,
            )
            if auto_render:
                stats.record_auto_render()
            result = _extract_response(
                page,
                requested=requested,
                site_profile=explicit,
                profiles=profiles,
            )
        if auto_render:
            key = cache_key(
                requested,
                safe_headers,
                safe_cookies,
                render=True,
                ua_strategy=strategy,
                profile_id=lookup_id,
                profile_version=lookup_version,
            )
        await cache.set(key, result)
        return apply_max_chars(result, max_chars)
    except ArachneError as exc:
        error_code = exc.code
        stats.record_error(exc.code)
        raise
    except Exception:
        error_code = INTERNAL
        stats.record_error(INTERNAL)
        logger.exception("unhandled extract error")
        raise ArachneError(INTERNAL, "Internal server error") from None
    finally:
        if entered_inflight:
            stats.leave_in_flight()
        finish_log()


def _log_suggest(
    *,
    url: str,
    strategy: str,
    latency_ms: float,
    error_code: str | None,
    session_prefix: str,
    qps_wait_ms: float,
    auto_render: int,
) -> None:
    logger.info(
        "suggest url=%s strategy=%s latency_ms=%.2f error_code=%s session=%s qps_wait_ms=%.2f auto_render=%s",
        url,
        strategy or "-",
        latency_ms,
        error_code or "-",
        session_prefix,
        qps_wait_ms,
        1 if auto_render else 0,
    )


async def run_suggest(
    *,
    url: str,
    client: httpx.AsyncClient,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    session_id: str | None = None,
    render: bool = False,
    strategy: SuggestStrategy = "heuristic",
    limiter: FixedWindowRateLimiter,
    semaphore: asyncio.Semaphore,
    stats: Stats,
) -> SuggestResponse:
    """QPS → semaphore fetch/render → heuristic/LLM suggest. No cache, no disk write."""
    t0 = time.perf_counter()
    stats.record_request()
    error_code: str | None = None
    session_prefix = "-"
    requested = (url or "").strip()
    entered_inflight = False
    used_strategy = strategy or "heuristic"
    qps_wait_ms = 0.0
    auto_render = 0

    try:
        session = load_session(session_id)
        merged = merge_session_material(session, cookies, headers)
        safe_headers = filter_request_headers(merged.headers)
        safe_cookies = dict(merged.cookies)
        fingerprint = session_fingerprint(safe_headers, safe_cookies)
        session_prefix = fingerprint[:_SESSION_PREFIX_LEN]

        qps_slot = {"ms": 0.0}
        try:
            await _acquire_qps(limiter, stats, qps_slot)
        finally:
            qps_wait_ms = qps_slot["ms"]

        stats.enter_in_flight()
        entered_inflight = True
        upstream_headers = headers_for_upstream(safe_headers, "default")
        async with semaphore:
            page, auto_render = await fetch_ready_page(
                requested,
                client,
                headers=upstream_headers or None,
                cookies=safe_cookies or None,
                render=render,
            )
            if auto_render:
                stats.record_auto_render()
        result = await suggest_from_html(page.body, url=page.final_url, strategy=used_strategy)
        used_strategy = result.evidence.strategy_used
        return result
    except ArachneError as exc:
        error_code = exc.code
        stats.record_error(exc.code)
        raise
    except Exception:
        error_code = INTERNAL
        stats.record_error(INTERNAL)
        logger.exception("unhandled suggest error")
        raise ArachneError(INTERNAL, "Internal server error") from None
    finally:
        if entered_inflight:
            stats.leave_in_flight()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        stats.record_latency(latency_ms)
        _log_suggest(
            url=requested,
            strategy=used_strategy,
            latency_ms=latency_ms,
            error_code=error_code,
            session_prefix=session_prefix,
            qps_wait_ms=qps_wait_ms,
            auto_render=auto_render,
        )
