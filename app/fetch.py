"""Async HTTP fetch with timeouts, redirect limit, body cap, and SSRF hooks."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from urllib.parse import urljoin, urlsplit, urlunsplit

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
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HOST_ONLY_POLICY = DefaultCookiePolicy(
    strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain,
)

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


class _NoStoreCookies(httpx.Cookies):
    """Shared-client jar that never keeps Set-Cookie from responses."""

    def extract_cookies(self, response: httpx.Response) -> None:
        return


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

    Automatic redirects and the client cookie jar are off. ``_read_page``
    follows redirects itself and keeps cookies only for that one fetch.
    """
    resolved = resolve_verify(verify)
    client_timeout = timeout if timeout is not None else HTTP_TIMEOUT
    shared = {
        "follow_redirects": False,
        "max_redirects": MAX_REDIRECTS,
        "timeout": client_timeout,
        "verify": resolved,
        "trust_env": False,
        "headers": {"User-Agent": USER_AGENT},
        "event_hooks": {"request": [ssrf_request_hook]},
    }
    proxy = configured_proxy_url()
    if proxy is None:
        client = httpx.AsyncClient(transport=PinIPAsyncTransport(verify=resolved), **shared)
    else:
        logger.info("http proxy enabled proxy=%s", redact_proxy_url(proxy))
        try:
            client = httpx.AsyncClient(proxy=proxy, **shared)
        except (ValueError, httpx.InvalidURL) as exc:
            raise fetch_failed(
                "Invalid ARACHNE_HTTP_PROXY",
                {"proxy": redact_proxy_url(proxy)},
            ) from exc
    # AsyncClient wraps cookies= in a plain Cookies() copy, which would
    # drop this subclass. Assign the no-store jar after construction.
    client._cookies = _NoStoreCookies()
    return client


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


def _idna_host(host: str) -> str:
    lowered = host.strip().lower()
    try:
        return lowered.encode("idna").decode("ascii")
    except UnicodeError:
        return lowered


def _ephemeral_cookie_jar(url: str, cookies: dict[str, str] | None) -> httpx.Cookies:
    """Per-fetch jar: caller cookies are host-only; Set-Cookie uses httpx rules.

    httpx.Cookies.set() without a domain is sent to every host, and
    set(..., domain=host) also covers subdomains such as www. Seed a
    host-only Cookie instead, with DomainStrictNonDomain so it stays
    on the exact hostname.
    """
    jar = httpx.Cookies()
    jar.jar = CookieJar(policy=_HOST_ONLY_POLICY)
    if not cookies:
        return jar
    parsed = urlsplit(url)
    host = _idna_host(parsed.hostname or "")
    if not host:
        return jar
    secure = (parsed.scheme or "").lower() == "https"
    for name, value in cookies.items():
        jar.jar.set_cookie(
            Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=host,
                domain_specified=False,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=secure,
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None},
                rfc2109=False,
            )
        )
    return jar


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return scheme, host, -1
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def _is_https_redirect(current: str, next_url: str) -> bool:
    scheme, host, port = _origin(current)
    next_scheme, next_host, next_port = _origin(next_url)
    return (
        host == next_host
        and scheme == "http"
        and port == 80
        and next_scheme == "https"
        and next_port == 443
    )


def _headers_for_redirect(headers: dict[str, str], current: str, next_url: str) -> dict[str, str]:
    """Drop Authorization when the origin changes, except http:80 → https:443 on the same host."""
    out = dict(headers)
    if not _same_origin(current, next_url) and not _is_https_redirect(current, next_url):
        for key in list(out):
            if key.lower() == "authorization":
                out.pop(key)
    return out


def _redirect_location(response: httpx.Response) -> str | None:
    # Empty or whitespace Location is a final 3xx, not a self-redirect loop.
    if response.status_code not in _REDIRECT_STATUSES:
        return None
    raw = response.headers.get("location")
    if raw is None:
        return None
    location = str(raw).strip()
    return location or None


def _join_redirect_url(current: str, location: str) -> str:
    joined = urljoin(current, location)
    current_parts = urlsplit(current)
    joined_parts = urlsplit(joined)
    if current_parts.fragment and not joined_parts.fragment:
        joined = urlunsplit(
            (
                joined_parts.scheme,
                joined_parts.netloc,
                joined_parts.path,
                joined_parts.query,
                current_parts.fragment,
            )
        )
    return joined


async def _drain_body(response: httpx.Response) -> None:
    async for _chunk in response.aiter_bytes():
        pass


async def _read_capped_body(response: httpx.Response) -> tuple[str, str]:
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
    return content_type, _decode_body(b"".join(chunks), content_type)


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


def _build_fetch_request(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    cookie_jar: httpx.Cookies,
) -> httpx.Request:
    """Build a GET that carries client defaults, but cookies only from this fetch's jar.

    ``client.stream(..., cookies=jar)`` and ``build_request`` merge
    ``client.cookies`` into a new jar with the default policy, which
    drops host-only semantics and can leak cookies across fetches.
    """
    merged = httpx.Headers(client.headers)
    merged.update(headers)
    merged.pop("cookie", None)
    request = httpx.Request("GET", url, headers=merged)
    cookie_jar.set_cookie_header(request)
    return request


async def _read_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> FetchedPage:
    """GET `url`, following redirects, capping the final body at MAX_BODY_BYTES."""
    cookie_jar = _ephemeral_cookie_jar(url, cookies)
    request_headers = dict(headers or {})
    current = url
    history = 0

    while True:
        if history > MAX_REDIRECTS:
            raise fetch_failed("Too many redirects", {"max_redirects": MAX_REDIRECTS})
        await assert_public_http_url(current)
        request = _build_fetch_request(client, current, request_headers, cookie_jar)
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            cookie_jar.extract_cookies(response)
            location = _redirect_location(response)
            if location is None:
                content_type, body = await _read_capped_body(response)
                return FetchedPage(
                    requested_url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    content_type=content_type,
                    body=body,
                )
            await _drain_body(response)
            next_url = _join_redirect_url(str(response.url), location)
            request_headers = _headers_for_redirect(
                request_headers,
                str(response.url),
                next_url,
            )
            current = next_url
            history += 1
        finally:
            await response.aclose()


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
