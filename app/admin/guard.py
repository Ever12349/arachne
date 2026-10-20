"""Loopback peer gate for /admin. The Host header is not consulted."""

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


def require_admin(request: Request) -> None:
    """404 when admin is disabled. 403 when the TCP peer is not loopback."""
    if not config.ADMIN_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    peer = request.client.host if request.client is not None else None
    if not is_loopback_peer(peer):
        raise forbidden("Admin is only available from localhost")
