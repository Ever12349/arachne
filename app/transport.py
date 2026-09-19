"""httpx transport that connects to resolved public IPs (SSRF pin-IP).

TLS SNI / server_hostname stays the original host; the HTTP Host header is
unchanged. Playwright is not pinned — see docs/DESIGN.md.
"""

from __future__ import annotations

import httpx

from app.errors import bad_url
from app.ssrf import is_blocked_ip, resolve_host_ips


def pin_request_to_ip(request: httpx.Request, ip: str, original_host: str) -> httpx.Request:
    """Rewrite the request URL to `ip` while keeping Host and TLS server_hostname."""
    pinned_url = request.url.copy_with(host=ip)
    extensions = dict(request.extensions)
    if request.url.scheme == "https":
        extensions["sni_hostname"] = original_host
    headers = httpx.Headers(request.headers)
    host_header = request.headers.get("host") or original_host
    headers["host"] = host_header
    return httpx.Request(
        method=request.method,
        url=pinned_url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


class PinIPAsyncTransport(httpx.AsyncBaseTransport):
    """Resolve, skip non-public IPs, then TCP-connect to each remaining address."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport if transport is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if not host:
            raise bad_url("URL is missing a hostname")
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        ips = await resolve_host_ips(host, port)
        public_ips = [ip for ip in ips if not is_blocked_ip(ip)]
        if not public_ips:
            raise bad_url("URL points to a non-public address", {"host": host})

        last_error: BaseException | None = None
        for ip in public_ips:
            pinned = pin_request_to_ip(request, ip, host)
            try:
                return await self._transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = exc
                continue
        assert last_error is not None
        raise last_error

    async def aclose(self) -> None:
        await self._transport.aclose()
