"""Async HTTP fetch with timeouts, redirect limit, body cap, and SSRF hooks."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import (
    CONNECT_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_REDIRECTS,
    READ_TIMEOUT,
    USER_AGENT,
)
from app.errors import ArachneError, fetch_failed, timeout_error, too_large
from app.proxy_util import configured_proxy_url, redact_proxy_url
from app.ssrf import assert_public_http_url
from app.tls_aia import is_incomplete_chain_error, is_ssl_connect_error, recover_verify_context, resolve_verify
from app.transport import PinIPAsyncTransport

logger = logging.getLogger("arachne")

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})

# httpx requires all four fields; connect/read are the locked P0 values.
HTTP_TIMEOUT = httpx.Timeout(
    connect=CONNECT_TIMEOUT,
    read=READ_TIMEOUT,
    write=READ_TIMEOUT,
    pool=CONNECT_TIMEOUT,
)


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    body: str


async def ssrf_request_hook(request: httpx.Request) -> None:
    """httpx request hook: SSRF-check the original URL and every redirect hop."""
    await assert_public_http_url(str(request.url))


def create_http_client(
    verify: bool | ssl.SSLContext = True,
    *,
    timeout: httpx.Timeout | None = None,
) -> httpx.AsyncClient:
    """Shared client. Pin-IP when no proxy; httpx proxy transport when ARACHNE_HTTP_PROXY is set.

    `trust_env` is off so HTTP_PROXY / HTTPS_PROXY / ALL_PROXY do not apply.
    The request hook still runs assert_public_http_url (scheme, egress, at least
    one public IP) before the proxy or the pinned connection is used.

    ``verify=True`` (the lifespan client) is certifi, plus ``./data/ca-bundle.pem``
    when that file loads. An explicit ``SSLContext`` is the one-shot AIA retry
    and is not merged again. Page fetches do not pass ``verify=False``.
    """
    resolved = resolve_verify(verify)
    client_timeout = timeout if timeout is not None else HTTP_TIMEOUT
    shared = {
        "follow_redirects": True,
        "max_redirects": MAX_REDIRECTS,
        "timeout": client_timeout,
        "verify": resolved,
        "trust_env": False,
        "headers": {"User-Agent": USER_AGENT},
        "event_hooks": {"request": [ssrf_request_hook]},
    }
    proxy = configured_proxy_url()
    if proxy is None:
        return httpx.AsyncClient(transport=PinIPAsyncTransport(verify=resolved), **shared)
    logger.info("http proxy enabled proxy=%s", redact_proxy_url(proxy))
    try:
        return httpx.AsyncClient(proxy=proxy, **shared)
    except (ValueError, httpx.InvalidURL) as exc:
        raise fetch_failed(
            "Invalid ARACHNE_HTTP_PROXY",
            {"proxy": redact_proxy_url(proxy)},
        ) from exc


def media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_html_content_type(content_type: str) -> bool:
    media = media_type(content_type)
    if not media:
        return True
    return media in HTML_TYPES


def _decode_body(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    lower = content_type.lower()
    if "charset=" in lower:
        charset = lower.split("charset=", 1)[1].split(";", 1)[0].strip().strip("\"'")
        if not charset:
            charset = "utf-8"
    try:
        return body.decode(charset)
    except LookupError:
        return body.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def _connect_failure(exc: BaseException) -> ArachneError:
    kind = "ssl" if is_ssl_connect_error(exc) else "connect"
    return fetch_failed("Failed to fetch URL", {"reason": str(exc), "kind": kind})


def _failing_url(exc: httpx.ConnectError, fallback: str) -> str:
    request = getattr(exc, "request", None)
    if request is None:
        return fallback
    try:
        rendered = str(request.url)
    except Exception:
        return fallback
    return rendered or fallback


async def _read_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> FetchedPage:
    """GET `url`, following redirects, capping the body at MAX_BODY_BYTES."""
    await assert_public_http_url(url)
    async with client.stream(
        "GET",
        url,
        headers=headers or None,
        cookies=cookies or None,
    ) as response:
        content_type = response.headers.get("content-type", "")
        length_header = response.headers.get("content-length")
        if length_header is not None:
            try:
                declared = int(length_header)
            except ValueError:
                declared = 0
            if declared > MAX_BODY_BYTES:
                raise too_large(
                    "Response body exceeds 2MB limit",
                    {"max_bytes": MAX_BODY_BYTES, "content_length": declared},
                )

        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise too_large(
                    "Response body exceeds 2MB limit",
                    {"max_bytes": MAX_BODY_BYTES},
                )
            chunks.append(chunk)

        body = _decode_body(b"".join(chunks), content_type)
        return FetchedPage(
            requested_url=url,
            final_url=str(response.url),
            status_code=response.status_code,
            content_type=content_type,
            body=body,
        )


async def _retry_with_recovered_context(
    exc: httpx.ConnectError,
    url: str,
    *,
    headers: dict[str, str] | None,
    cookies: dict[str, str] | None,
) -> FetchedPage:
    """One AIA attempt. A second chain failure keeps the original error."""
    target = _failing_url(exc, url)
    ctx = await recover_verify_context(target)
    if ctx is None:
        raise _connect_failure(exc) from exc
    try:
        async with create_http_client(verify=ctx) as retry_client:
            page = await _read_page(retry_client, url, headers=headers, cookies=cookies)
    except ArachneError:
        raise
    except httpx.TimeoutException as retry_exc:
        raise timeout_error() from retry_exc
    except httpx.TooManyRedirects as retry_exc:
        raise fetch_failed("Too many redirects", {"max_redirects": MAX_REDIRECTS}) from retry_exc
    except httpx.ConnectError as retry_exc:
        if is_ssl_connect_error(retry_exc):
            raise _connect_failure(exc) from exc
        raise _connect_failure(retry_exc) from retry_exc
    except httpx.HTTPError as retry_exc:
        raise fetch_failed("Failed to fetch URL", {"reason": str(retry_exc)}) from retry_exc
    host = urlsplit(target).hostname or ""
    logger.info("aia recovered host=%s aia_recovered=1", host)
    return page


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> FetchedPage:
    """GET `url`, following redirects, capping the body at MAX_BODY_BYTES.

    An incomplete-chain TLS error is retried once with a temporary client.
    The shared client's verify setting is left alone.
    """
    try:
        return await _read_page(client, url, headers=headers, cookies=cookies)
    except ArachneError:
        raise
    except httpx.TimeoutException as exc:
        raise timeout_error() from exc
    except httpx.TooManyRedirects as exc:
        raise fetch_failed("Too many redirects", {"max_redirects": MAX_REDIRECTS}) from exc
    except httpx.ConnectError as exc:
        if is_incomplete_chain_error(exc):
            return await _retry_with_recovered_context(exc, url, headers=headers, cookies=cookies)
        raise _connect_failure(exc) from exc
    except httpx.HTTPError as exc:
        raise fetch_failed("Failed to fetch URL", {"reason": str(exc)}) from exc
