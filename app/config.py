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

# OpenAI-compatible chat for POST /profiles/suggest strategy=llm|auto.
# BASE_URL should include the API root (typically …/v1); we append /chat/completions.
LLM_BASE_URL = os.environ.get("ARACHNE_LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = os.environ.get("ARACHNE_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("ARACHNE_LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT = _env_float("ARACHNE_LLM_TIMEOUT", 30.0)

SUGGEST_MIN_TITLE_CHARS = _env_int("ARACHNE_SUGGEST_MIN_TITLE_CHARS", 2)
SUGGEST_MIN_MAIN_CHARS = _env_int("ARACHNE_SUGGEST_MIN_MAIN_CHARS", 80)
SUGGEST_SKELETON_MAX_NODES = _env_int("ARACHNE_SUGGEST_SKELETON_MAX_NODES", 200)
SUGGEST_SKELETON_MAX_CHARS = _env_int("ARACHNE_SUGGEST_SKELETON_MAX_CHARS", 30_000)
SUGGEST_OVERLAP_RATIO = _env_float("ARACHNE_SUGGEST_OVERLAP_RATIO", 0.6)

MAX_RETRIES = _env_int("ARACHNE_MAX_RETRIES", 2)
RETRY_BACKOFF_SECONDS = _env_floats("ARACHNE_RETRY_BACKOFF_SECONDS", [0.5, 1.0])

RENDER_TIMEOUT = _env_float("ARACHNE_RENDER_TIMEOUT", 15.0)

JOB_MAX_URLS = _env_int("ARACHNE_JOB_MAX_URLS", 50)
JOB_CONCURRENCY = _env_int("ARACHNE_JOB_CONCURRENCY", 3)
# 0 = keep completed jobs forever; >0 deletes completed/cancelled rows older than TTL.
JOB_DB_TTL_SECONDS = _env_int("ARACHNE_JOB_DB_TTL_SECONDS", 0)
DATABASE_URL = os.environ.get("ARACHNE_DATABASE_URL", "sqlite+aiosqlite:///./data/arachne.db")
