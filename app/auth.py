"""Optional API-key auth for a trusted single-instance sidecar.

Not OAuth, not per-key client binding. Keys are compared with hmac.compare_digest.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from app import config
from app.errors import unauthorized

_BEARER_PREFIX = "bearer "
HEALTH_PATHS = frozenset({"/health", "/ready"})


def parse_api_keys(raw: str | None) -> tuple[str, ...]:
    """Comma-separated keys: trim whitespace and skip empty segments."""
    if raw is None or not str(raw).strip():
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def assert_auth_configured() -> None:
    """Fail closed when auth is required but no keys are configured."""
    if config.REQUIRE_AUTH and not config.API_KEYS:
        raise RuntimeError("ARACHNE_REQUIRE_AUTH is enabled but ARACHNE_API_KEYS has no keys")


def key_matches(presented: str, configured: str) -> bool:
    if len(presented) != len(configured):
        return False
    return hmac.compare_digest(presented, configured)


def any_key_matches(presented: str, configured: tuple[str, ...]) -> bool:
    return any(key_matches(presented, key) for key in configured)


def presented_keys(request: Request) -> list[str]:
    """Keys offered via Authorization: Bearer or X-Arachne-Key (either is enough)."""
    found: list[str] = []
    authorization = request.headers.get("Authorization")
    if authorization is not None and authorization[:7].lower() == _BEARER_PREFIX:
        token = authorization[7:]
        if token:
            found.append(token)
    header_key = request.headers.get("X-Arachne-Key")
    if header_key:
        found.append(header_key)
    return found


def normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def path_is_exempt(path: str) -> bool:
    normalized = normalize_path(path)
    if normalized in HEALTH_PATHS:
        return True
    if normalized == "/metrics" and config.METRICS_PUBLIC:
        return True
    if normalized == "/stats" and config.STATS_PUBLIC:
        return True
    return False


def enforce_auth(request: Request) -> None:
    """App-wide dependency. When REQUIRE_AUTH is false, headers are ignored."""
    if not config.REQUIRE_AUTH:
        return
    if path_is_exempt(request.url.path):
        return
    for presented in presented_keys(request):
        if any_key_matches(presented, config.API_KEYS):
            return
    raise unauthorized()
