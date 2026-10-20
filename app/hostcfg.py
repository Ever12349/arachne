"""Host-helper settings read at call time.

The helper process and the admin process do not share an import. Both read the
environment (and, for the token only, `./.env`) when a request arrives.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.dotenv_file import load_env_file

HELPER_DEFAULT_PORT = 18765
HELPER_SERVICE = "arachne-host-helper"


def helper_port() -> int:
    """TCP port for the loopback helper. Invalid values fall back to 18765."""
    raw = os.environ.get("ARACHNE_HOST_HELPER_PORT", "").strip()
    if not raw:
        raw = _dotenv().get("ARACHNE_HOST_HELPER_PORT", "").strip()
    if not raw:
        return HELPER_DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return HELPER_DEFAULT_PORT
    if not 1 <= port <= 65535:
        return HELPER_DEFAULT_PORT
    return port


def helper_base_url() -> str:
    """Browser and admin both use IPv4 loopback, never a wildcard bind."""
    return f"http://127.0.0.1:{helper_port()}"


def host_helper_token() -> str:
    """Shared secret. Environment wins over `.env`. Empty means unset."""
    raw = os.environ.get("ARACHNE_HOST_HELPER_TOKEN", "").strip()
    if raw:
        return raw
    return _dotenv().get("ARACHNE_HOST_HELPER_TOKEN", "").strip()


def running_in_docker() -> bool:
    """True when this process must not open a Mac window.

    `ARACHNE_IN_DOCKER` wins when it is set. Otherwise a `/.dockerenv` file
    means the process is inside a container.
    """
    raw = os.environ.get("ARACHNE_IN_DOCKER")
    if raw is not None and str(raw).strip():
        return str(raw).strip().lower() in {"1", "true", "yes"}
    return Path("/.dockerenv").is_file()


def capture_mode() -> str:
    """`auto` (default), `host` (spawn in this process), or `helper` (never spawn)."""
    raw = os.environ.get("ARACHNE_CAPTURE_MODE", "").strip().lower()
    if raw in {"host", "helper", "auto"}:
        return raw
    dotted = _dotenv().get("ARACHNE_CAPTURE_MODE", "").strip().lower()
    if dotted in {"host", "helper", "auto"}:
        return dotted
    return "auto"


def _dotenv() -> dict[str, str]:
    configured = os.environ.get("ARACHNE_DOTENV", "").strip()
    path = Path(configured) if configured else Path(".env")
    return load_env_file(path)
