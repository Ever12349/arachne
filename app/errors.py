"""Machine-readable extract errors and their HTTP status mapping."""

from __future__ import annotations

from typing import Any

BAD_URL = "bad_url"
FETCH_FAILED = "fetch_failed"
TIMEOUT = "timeout"
UNSUPPORTED_CONTENT = "unsupported_content"
TOO_LARGE = "too_large"
UNAUTHORIZED_UPSTREAM = "unauthorized_upstream"
EXTRACT_EMPTY = "extract_empty"
RATE_LIMITED = "rate_limited"
INTERNAL = "internal"
SESSION_INVALID = "session_invalid"
PROFILE_INVALID = "profile_invalid"
CHALLENGE_DETECTED = "challenge_detected"
RENDER_UNAVAILABLE = "render_unavailable"
RENDER_FAILED = "render_failed"
JOB_NOT_FOUND = "job_not_found"
LLM_UNAVAILABLE = "llm_unavailable"
LLM_FAILED = "llm_failed"
PROFILE_EXISTS = "profile_exists"
UNAUTHORIZED = "unauthorized"
FORBIDDEN = "forbidden"
EGRESS_BLOCKED = "egress_blocked"
NOT_READY = "not_ready"

HTTP_STATUS: dict[str, int] = {
    BAD_URL: 400,
    SESSION_INVALID: 400,
    PROFILE_INVALID: 400,
    CHALLENGE_DETECTED: 403,
    FORBIDDEN: 403,
    EGRESS_BLOCKED: 403,
    UNAUTHORIZED: 401,
    UNSUPPORTED_CONTENT: 422,
    EXTRACT_EMPTY: 422,
    TOO_LARGE: 422,
    TIMEOUT: 504,
    FETCH_FAILED: 502,
    UNAUTHORIZED_UPSTREAM: 502,
    RENDER_FAILED: 502,
    LLM_FAILED: 502,
    RENDER_UNAVAILABLE: 501,
    LLM_UNAVAILABLE: 501,
    RATE_LIMITED: 429,
    INTERNAL: 500,
    JOB_NOT_FOUND: 404,
    PROFILE_EXISTS: 409,
    NOT_READY: 503,
}


class ArachneError(Exception):
    """Raised for expected extract failures; mapped to `{error: {code, message, detail}}`."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = detail or {}


def http_status_for(code: str) -> int:
    return HTTP_STATUS.get(code, HTTP_STATUS[INTERNAL])


def bad_url(message: str, detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(BAD_URL, message, detail)


def fetch_failed(message: str, detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(FETCH_FAILED, message, detail)


def timeout_error(message: str = "Upstream request timed out", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(TIMEOUT, message, detail)


def unsupported_content(message: str, detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(UNSUPPORTED_CONTENT, message, detail)


def too_large(message: str, detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(TOO_LARGE, message, detail)


def unauthorized_upstream(message: str, detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(UNAUTHORIZED_UPSTREAM, message, detail)


def extract_empty(message: str = "Could not extract title or main text", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(EXTRACT_EMPTY, message, detail)


def rate_limited(message: str = "Rate limit exceeded", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(RATE_LIMITED, message, detail)


def session_invalid(message: str = "Invalid session", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(SESSION_INVALID, message, detail)


def profile_invalid(message: str = "Invalid site profile", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(PROFILE_INVALID, message, detail)


def challenge_detected(message: str = "Upstream returned a bot challenge page", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(CHALLENGE_DETECTED, message, detail)


def render_unavailable(message: str = "Playwright is not installed", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(RENDER_UNAVAILABLE, message, detail)


def render_failed(message: str = "Browser render failed", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(RENDER_FAILED, message, detail)


def job_not_found(message: str = "Job not found", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(JOB_NOT_FOUND, message, detail)


def llm_unavailable(message: str = "LLM is not configured", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(LLM_UNAVAILABLE, message, detail)


def llm_failed(message: str = "LLM profile suggestion failed", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(LLM_FAILED, message, detail)


def profile_exists(message: str = "Profile already exists", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(PROFILE_EXISTS, message, detail)


def unauthorized(message: str = "Missing or invalid API key", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(UNAUTHORIZED, message, detail)


def forbidden(message: str = "Forbidden", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(FORBIDDEN, message, detail)


def egress_blocked(message: str = "Host is not on the egress allowlist", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(EGRESS_BLOCKED, message, detail)


def not_ready(message: str = "Service is not ready", detail: dict[str, Any] | None = None) -> ArachneError:
    return ArachneError(NOT_READY, message, detail)
