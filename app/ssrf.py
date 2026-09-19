"""SSRF guards: only public http(s) URLs, resolved before fetch.

httpx connects via PinIPAsyncTransport (TCP to the resolved public IP, TLS
server_hostname = original host). Playwright still navigates by hostname, so
the render path keeps a DNS-rebinding window; see docs/DESIGN.md.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from app.errors import bad_url, fetch_failed

_BLOCKED_SCHEMES = frozenset({"javascript", "mailto", "data", "file", "ftp", "ws", "wss"})


def normalize_host(host: str | None) -> str:
    """Lowercase hostname and strip a single leading `www.` for same-host compares."""
    if not host:
        return ""
    host = host.strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_blocked_ip(raw: str) -> bool:
    """True if `raw` is private, loopback, link-local, or unspecified (incl. IPv4-mapped)."""
    candidate = raw.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def parse_http_url(url: str) -> tuple[str, str, int]:
    """Return (scheme, hostname, port). Raises ArachneError(bad_url) if not http(s)."""
    if not url or not url.strip():
        raise bad_url("URL is empty")
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES or scheme not in {"http", "https"}:
        raise bad_url("Only http and https URLs are allowed", {"scheme": scheme})
    host = parsed.hostname
    if not host:
        raise bad_url("URL is missing a hostname")
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


async def resolve_host_ips(host: str, port: int) -> list[str]:
    """Resolve hostname to IP strings via getaddrinfo (async)."""
    try:
        idna_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        idna_host = host
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(idna_host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise fetch_failed("Failed to resolve hostname", {"host": host, "reason": str(exc)}) from exc
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    if not ips:
        raise fetch_failed("Failed to resolve hostname", {"host": host})
    return ips


async def assert_public_http_url(url: str) -> None:
    """Validate scheme, optional egress allowlist, and reject non-public addresses."""
    from app.egress import assert_egress_allowed

    _scheme, host, port = parse_http_url(url)
    assert_egress_allowed(host)
    ips = await resolve_host_ips(host, port)
    blocked = [ip for ip in ips if is_blocked_ip(ip)]
    if blocked:
        raise bad_url(
            "URL points to a non-public address",
            {"host": host},
        )
