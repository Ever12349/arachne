"""Loopback peer gate for /admin.

The Host header is ignored unless ARACHNE_ADMIN_TRUST_DOCKER is on. That flag
only accepts Host 127.0.0.1 or localhost (optional :port). X-Forwarded-For is
never read.
"""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request

from app import config
from app.errors import forbidden


def is_loopback_peer(host: str | None) -> bool:
    """True for 127.0.0.0/8, ::1, and IPv4-mapped loopback. False for missing peers."""
    if not host:
        return False
    candidate = host.strip().strip("[]")
    if "%" in candidate:
        candidate = candidate.split("%", 1)[0]
    if candidate.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_loopback)


def host_is_local_docker_name(host_header: str | None) -> bool:
    """True for Host `127.0.0.1` or `localhost`, with an optional numeric port."""
    if not host_header:
        return False
    value = host_header.strip().lower()
    if not value or value.startswith("["):
        return False
    if ":" in value:
        name, _, port = value.rpartition(":")
        if not port.isdigit():
            return False
        value = name
    return value in {"127.0.0.1", "localhost"}


def require_admin(request: Request) -> None:
    """404 when admin is disabled. 403 when the caller is not a local admin peer."""
    if not config.ADMIN_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    peer = request.client.host if request.client is not None else None
    if is_loopback_peer(peer):
        return
    if config.ADMIN_TRUST_DOCKER and host_is_local_docker_name(request.headers.get("host")):
        return
    raise forbidden("Admin is only available from localhost")
