"""Allowlist for caller-injected request headers (POST /extract only)."""

from __future__ import annotations

# Canonical names forwarded to httpx when present on the request.
ALLOWED_HEADERS: dict[str, str] = {
    "authorization": "Authorization",
    "accept": "Accept",
    "accept-language": "Accept-Language",
    "user-agent": "User-Agent",
    "referer": "Referer",
    "cache-control": "Cache-Control",
}

# Never forwarded. Cookie must be passed via the `cookies` field, not headers.
FORBIDDEN_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "cookie",
    }
)


def filter_request_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Keep allowlisted headers; strip/reject hop-by-hop and Cookie headers."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for raw_name, value in headers.items():
        key = raw_name.strip().lower()
        if key in FORBIDDEN_HEADERS:
            continue
        canonical = ALLOWED_HEADERS.get(key)
        if canonical is None:
            continue
        out[canonical] = value
    return out
