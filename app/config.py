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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return float(raw)


def _env_floats(name: str, default: list[float]) -> list[float]:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return list(default)
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return list(default)
    return [float(p) for p in parts]


MAX_CONCURRENCY = _env_int("ARACHNE_MAX_CONCURRENCY", 10)
QPS = _env_int("ARACHNE_QPS", 5)
CACHE_TTL_SECONDS = _env_int("ARACHNE_CACHE_TTL_SECONDS", 60)
CACHE_MAXSIZE = _env_int("ARACHNE_CACHE_MAXSIZE", 256)

SESSIONS_DIR = os.environ.get("ARACHNE_SESSIONS_DIR", "./data/sessions")
SESSION_KEY = os.environ.get("ARACHNE_SESSION_KEY", "")

# Default is an empty local dir, not profiles/examples (examples are docs only).
PROFILES_DIR = os.environ.get("ARACHNE_PROFILES_DIR", "./data/profiles")
PROFILE_RELOAD_DEBOUNCE_SECONDS = 1.0

MAX_RETRIES = _env_int("ARACHNE_MAX_RETRIES", 2)
RETRY_BACKOFF_SECONDS = _env_floats("ARACHNE_RETRY_BACKOFF_SECONDS", [0.5, 1.0])

RENDER_TIMEOUT = _env_float("ARACHNE_RENDER_TIMEOUT", 15.0)

JOB_MAX_URLS = _env_int("ARACHNE_JOB_MAX_URLS", 50)
JOB_CONCURRENCY = _env_int("ARACHNE_JOB_CONCURRENCY", 3)
JOB_TTL_SECONDS = _env_int("ARACHNE_JOB_TTL_SECONDS", 3600)
JOB_MAX_STORED = _env_int("ARACHNE_JOB_MAX_STORED", 100)
