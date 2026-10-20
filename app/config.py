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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes"}


def _env_csv(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


def _env_csv_or_default(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Like `_env_csv`, but an unset variable keeps `default`.

    An explicit empty string (or whitespace) is an empty tuple, not `default`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not str(raw).strip():
        return ()
    return tuple(part.strip() for part in str(raw).split(",") if part.strip())


MAX_IMAGES = _env_int("ARACHNE_MAX_IMAGES", 10)

MAX_CONCURRENCY = _env_int("ARACHNE_MAX_CONCURRENCY", 10)
QPS = _env_int("ARACHNE_QPS", 5)


def _env_qps_mode() -> str:
    """`reject` is immediate 429. Anything else, including unset or invalid, is `queue`."""
    raw = os.environ.get("ARACHNE_QPS_MODE")
    if raw is not None and str(raw).strip().lower() == "reject":
        return "reject"
    return "queue"


QPS_MODE = _env_qps_mode()
QPS_WAIT_SECONDS = _env_float("ARACHNE_QPS_WAIT_SECONDS", 30.0)
AUTO_RENDER = _env_bool("ARACHNE_AUTO_RENDER", True)

# CSV tokens → error codes that may escalate to one Playwright attempt.
# Unset → both. Explicit empty → no codes. Unknown tokens are ignored.
_AUTO_RENDER_TOKEN_TO_CODE = {
    "challenge": "challenge_detected",
    "unauthorized": "unauthorized_upstream",
}


def auto_render_on_codes() -> frozenset[str]:
    """Error codes selected by `ARACHNE_AUTO_RENDER_ON` (read from the environment)."""
    tokens = _env_csv_or_default("ARACHNE_AUTO_RENDER_ON", ("challenge", "unauthorized"))
    return frozenset(
        _AUTO_RENDER_TOKEN_TO_CODE[token.lower()]
        for token in tokens
        if token.lower() in _AUTO_RENDER_TOKEN_TO_CODE
    )


AUTO_RENDER_ON = auto_render_on_codes()
# Writes of auto-<host> sessions. Reads stay enabled when this is false.
SESSION_PERSIST = _env_bool("ARACHNE_SESSION_PERSIST", True)
# Empty disables proxy. Read only here; callers use app.proxy_util.
HTTP_PROXY = os.environ.get("ARACHNE_HTTP_PROXY", "").strip()
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
# 0 = keep completed jobs forever; default 7 days. >0 deletes completed/cancelled rows older than TTL.
JOB_DB_TTL_SECONDS = _env_int("ARACHNE_JOB_DB_TTL_SECONDS", 604800)
DATABASE_URL = os.environ.get("ARACHNE_DATABASE_URL", "sqlite+aiosqlite:///./data/arachne.db")

# Optional API keys for a trusted single-instance / intranet sidecar. Not OAuth.
API_KEYS = _env_csv("ARACHNE_API_KEYS")
REQUIRE_AUTH = _env_bool("ARACHNE_REQUIRE_AUTH", False)
METRICS_PUBLIC = _env_bool("ARACHNE_METRICS_PUBLIC", False)
STATS_PUBLIC = _env_bool("ARACHNE_STATS_PUBLIC", False)
PROFILES_WRITE = _env_bool("ARACHNE_PROFILES_WRITE", False)

# Empty = disabled. Matched as host == suffix or host.endswith('.' + suffix).
EGRESS_ALLOWLIST = _env_csv("ARACHNE_EGRESS_ALLOWLIST")

# Human /admin UI on this process. False hides the routes (404). Default on.
# Loopback peer check still applies when this is true. Not a separate admin key.
ADMIN_ENABLED = _env_bool("ARACHNE_ADMIN_ENABLED", True)
# Personal Compose only. When true, a non-loopback peer (the Docker bridge) may
# open /admin if the Host header is 127.0.0.1 or localhost, with an optional port.
# X-Forwarded-For is never consulted. Default off.
ADMIN_TRUST_DOCKER = _env_bool("ARACHNE_ADMIN_TRUST_DOCKER", False)
FAILURE_RING_SIZE = 50

LOG_JSON = _env_bool("ARACHNE_LOG_JSON", False)
