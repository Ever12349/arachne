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


def _redact_userinfo_fallback(url: str) -> str:
    """Hide userinfo when urlsplit cannot parse the authority (for example a bad port)."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return "***"
    authority, slash, remainder = rest.partition("/")
    if "@" not in authority:
        return url
    hostport = authority.rsplit("@", 1)[-1]
    return f"{scheme}://***:***@{hostport}{slash}{remainder}"


def redact_url_userinfo(url: str) -> str:
    """Drop userinfo so logs and error details can name a page URL.

    Path, query, and fragment are kept. Unlike ``redact_proxy_url``, this
    does not strip the path. An invalid port still hides userinfo when
    the authority contains ``@``.
    """
    try:
        parsed = urlsplit(url)
        username = parsed.username
        password = parsed.password
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return _redact_userinfo_fallback(url)
    if username is None and password is None:
        return url
    if ":" in host:
        host = f"[{host}]"
    netloc = f"***:***@{host}" if host else "***:***"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


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
