"""Runtime settings for the extract pipeline."""

from __future__ import annotations

import os

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_REDIRECTS = 20
MAX_BODY_BYTES = 2 * 1024 * 1024
MAIN_TEXT_MAX_CHARS = 100_000
LINK_TEXT_MAX_CHARS = 200
MAX_LINKS = 50

USER_AGENT = os.environ.get(
    "ARACHNE_USER_AGENT",
    "Arachne/0.1 (+https://github.com/Ever12349/arachne)",
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


MAX_CONCURRENCY = _env_int("ARACHNE_MAX_CONCURRENCY", 10)
QPS = _env_int("ARACHNE_QPS", 5)
CACHE_TTL_SECONDS = _env_int("ARACHNE_CACHE_TTL_SECONDS", 60)
CACHE_MAXSIZE = _env_int("ARACHNE_CACHE_MAXSIZE", 256)
