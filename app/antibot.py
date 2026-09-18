"""Defensive antibot: timeout/connect retries, UA rotation, challenge-page detection."""

from __future__ import annotations

import asyncio
import logging
import random

import httpx

from app import config
from app.errors import FETCH_FAILED, TIMEOUT, ArachneError, challenge_detected
from app.fetch import FetchedPage, fetch_url, is_html_content_type

logger = logging.getLogger("arachne")

# High-precision markers (lowercase). Prefer CF/challenge tokens over generic "please wait".
CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "cf-challenge",
    "_cf_chl",
    "attention required",
    "challenge-platform",
    "cf-browser-verification",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "why have i been blocked",
    "managed challenge",
)

# Small static pool for ua_strategy=rotate. Explicit User-Agent from caller/session still wins.
UA_POOL: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
)


def looks_like_challenge(body: str) -> bool:
    if not body:
        return False
    lowered = body.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def raise_if_challenge(page: FetchedPage) -> None:
    """Challenge detection runs after fetch. 4xx that are not 401/403 stay fetch_failed."""
    if page.status_code in {401, 403}:
        if looks_like_challenge(page.body):
            raise challenge_detected(
                "Upstream returned a bot challenge page",
                {"status_code": page.status_code},
            )
        return
    if 200 <= page.status_code < 300 and is_html_content_type(page.content_type):
        if looks_like_challenge(page.body):
            raise challenge_detected(
                "Upstream returned a bot challenge page",
                {"status_code": page.status_code},
            )


def is_retryable(exc: ArachneError) -> bool:
    if exc.code == TIMEOUT:
        return True
    return exc.code == FETCH_FAILED and exc.detail.get("kind") == "connect"


def backoff_delay(retry_index: int) -> float:
    """`retry_index` is 0 for the first retry (after the initial attempt)."""
    delays = config.RETRY_BACKOFF_SECONDS or [0.5, 1.0]
    if retry_index < len(delays):
        return delays[retry_index]
    return delays[-1]


def pick_rotated_ua() -> str:
    return random.choice(UA_POOL)


def headers_for_upstream(headers: dict[str, str], ua_strategy: str) -> dict[str, str]:
    """Inject User-Agent for the upstream call. Caller/session UA always wins."""
    out = dict(headers)
    if "User-Agent" in out:
        return out
    if ua_strategy == "rotate":
        out["User-Agent"] = pick_rotated_ua()
    else:
        out["User-Agent"] = config.USER_AGENT
    return out


async def fetch_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> FetchedPage:
    """Retry only timeout/connect errors. Never retry 4xx or challenge pages."""
    attempts = max(int(config.MAX_RETRIES), 0) + 1
    last_error: ArachneError | None = None
    for i in range(attempts):
        try:
            return await fetch_url(client, url, headers=headers, cookies=cookies)
        except ArachneError as exc:
            last_error = exc
            if not is_retryable(exc) or i >= attempts - 1:
                raise
            delay = backoff_delay(i)
            logger.info("fetch retry attempt=%s delay_s=%s error_code=%s", i + 2, delay, exc.code)
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
