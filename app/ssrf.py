"""SSRF guards: only public http(s) URLs, resolved before fetch.

Direct httpx connects via PinIPAsyncTransport (TCP to each resolved public IP,
TLS server_hostname = original host). `assert_public_http_url` matches that
rule: `bad_url` only when every resolved address is non-public. A mixed
private+public answer is allowed here; the transport then skips the private
addresses. When ARACHNE_HTTP_PROXY is set, httpx uses that proxy instead of
pin-IP; this check still runs first. Playwright navigates by hostname (through
the same proxy when set) and is not pin-IP, so the render path keeps a
DNS-rebinding window; see docs/DESIGN.md.
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
    """Return (scheme, hostname, port). Raises ArachneError(bad_url) if not http(s).

    Userinfo (`user:pass@host`) is rejected. A path like `/@alice` is not
    userinfo and is allowed. Port 0 is rejected; 1–65535 are allowed.
    An omitted port becomes 443 for https and 80 for http.
    """
    if not url or not url.strip():
        raise bad_url("URL is empty")
    try:
        parsed = urlparse(url.strip())
        username = parsed.username
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise bad_url("URL is invalid") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES or scheme not in {"http", "https"}:
        raise bad_url("Only http and https URLs are allowed", {"scheme": scheme})
    if username is not None:
        raise bad_url("URL must not include userinfo")
    if not host:
        raise bad_url("URL is missing a hostname")
    if port is None:
        port = 443 if scheme == "https" else 80
    elif port < 1 or port > 65535:
        raise bad_url("URL port is not allowed", {"port": port})
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
    """Validate scheme, optional egress allowlist, and require one public address.

    Mixed DNS (a private address beside a public one) is not `bad_url`. Pin-IP
    connects only to the public addresses; this gate uses the same leftover rule.
    """
    from app.egress import assert_egress_allowed

    _scheme, host, port = parse_http_url(url)
    assert_egress_allowed(host)
    ips = await resolve_host_ips(host, port)
    if not any(not is_blocked_ip(ip) for ip in ips):
        raise bad_url(
            "URL points to a non-public address",
            {"host": host},
        )
