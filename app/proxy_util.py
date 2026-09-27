"""Single reader for ARACHNE_HTTP_PROXY.

The environment variable is loaded in app.config. This module validates it,
redacts userinfo, and shapes the value for httpx and Playwright.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

from app import config
from app.errors import fetch_failed

_PROXY_SCHEMES = frozenset({"http", "https"})


def redact_proxy_url(url: str) -> str:
    """Drop userinfo so logs and error details can name the proxy."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
        username = parsed.username
        password = parsed.password
        scheme = parsed.scheme
    except ValueError:
        return "***"
    if username is None and password is None:
        return url
    if ":" in host:
        host = f"[{host}]"
    netloc = f"***:***@{host}" if host else "***:***"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((scheme, netloc, "", "", ""))


def configured_proxy_url() -> str | None:
    """Return the proxy URL, or None when proxying is off.

    Empty / whitespace is off. A non-empty value that is not an http(s) URL
    with a host raises `fetch_failed`.
    """
    raw = str(getattr(config, "HTTP_PROXY", "") or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    try:
        host = parsed.hostname
        parsed.port
    except ValueError:
        host = None
    if parsed.scheme.lower() not in _PROXY_SCHEMES or not host:
        raise fetch_failed(
            "Invalid ARACHNE_HTTP_PROXY",
            {"proxy": redact_proxy_url(raw)},
        )
    return raw


def playwright_proxy() -> dict[str, str] | None:
    """Playwright `launch(proxy=…)` mapping, or None when proxying is off."""
    raw = configured_proxy_url()
    if raw is None:
        return None
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    server = f"{parsed.scheme.lower()}://{host}"
    if parsed.port:
        server = f"{server}:{parsed.port}"
    settings: dict[str, str] = {"server": server}
    if parsed.username:
        settings["username"] = unquote(parsed.username)
    if parsed.password:
        settings["password"] = unquote(parsed.password)
    return settings
