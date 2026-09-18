"""In-memory TTL cache of successful extract results."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from urllib.parse import urlparse, urlunparse

from cachetools import TTLCache

from app.config import CACHE_MAXSIZE, CACHE_TTL_SECONDS
from app.models import ExtractResponse


def normalize_cache_url(url: str) -> str:
    """Lower scheme/host, drop default port and fragment, keep query order."""
    parsed = urlparse((url or "").strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return (url or "").strip()

    port = parsed.port
    default_port = 80 if scheme == "http" else 443 if scheme == "https" else None
    host_part = f"[{host}]" if ":" in host else host
    if port is None or (default_port is not None and port == default_port):
        netloc = host_part
    else:
        netloc = f"{host_part}:{port}"

    return urlunparse((scheme, netloc, parsed.path, parsed.params, parsed.query, ""))


def session_fingerprint(headers: dict[str, str], cookies: dict[str, str]) -> str:
    """sha256 of a canonical JSON serialization of session headers + cookies."""
    payload = {
        "cookies": {str(k): str(v) for k, v in sorted(cookies.items())},
        "headers": {str(k): str(v) for k, v in sorted(headers.items())},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


CacheKey = tuple[str, str, str, str]


def cache_key(
    url: str,
    headers: dict[str, str],
    cookies: dict[str, str],
    *,
    render: bool = False,
    ua_strategy: str = "default",
) -> CacheKey:
    """Key is normalized URL + merged session fingerprint + render flag + ua_strategy."""
    strategy = ua_strategy or "default"
    return (
        normalize_cache_url(url),
        session_fingerprint(headers, cookies),
        "1" if render else "0",
        strategy,
    )


class ResultCache:
    """TTLCache of successful ExtractResponse values. Not used for errors."""

    def __init__(
        self,
        maxsize: int = CACHE_MAXSIZE,
        ttl: float = CACHE_TTL_SECONDS,
        timer: Callable[[], float] | None = None,
    ) -> None:
        if timer is None:
            self._cache = TTLCache(maxsize=maxsize, ttl=ttl)
        else:
            self._cache = TTLCache(maxsize=maxsize, ttl=ttl, timer=timer)
        self._lock = asyncio.Lock()

    async def get(self, key: CacheKey | tuple[str, ...]) -> ExtractResponse | None:
        async with self._lock:
            value = self._cache.get(key)
            if value is None:
                return None
            return value.model_copy(deep=True)

    async def set(self, key: CacheKey | tuple[str, ...], value: ExtractResponse) -> None:
        async with self._lock:
            self._cache[key] = value.model_copy(deep=True)

    def __len__(self) -> int:
        return len(self._cache)
