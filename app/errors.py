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
INTERNAL = "internal"

HTTP_STATUS: dict[str, int] = {
    BAD_URL: 400,
    UNSUPPORTED_CONTENT: 422,
    EXTRACT_EMPTY: 422,
    TOO_LARGE: 422,
    TIMEOUT: 504,
    FETCH_FAILED: 502,
    UNAUTHORIZED_UPSTREAM: 502,
    INTERNAL: 500,
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
