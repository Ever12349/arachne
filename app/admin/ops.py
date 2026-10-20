"""Session files and capture-command status for the admin UI.

List and status responses carry ids and mtimes only. They do not decrypt files
and they do not include cookie values, header values, or the Fernet key.
"""

from __future__ import annotations

import shlex
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app import config
from app.auto_session import auto_session_id
from app.errors import bad_url, session_invalid
from app.sessions import SESSION_ID_RE, session_file_path, session_key_usable, write_session_file
from app.ssrf import normalize_host, parse_http_url, redact_url_userinfo


def capture_command(url: str) -> str:
    """Host-side command. The Fernet key stays in the operator's environment."""
    return "python scripts/capture_session.py --url " + shlex.quote(url)


def _sessions_root() -> Path:
    return Path(config.SESSIONS_DIR).expanduser()


def list_sessions() -> list[dict[str, object]]:
    """Metadata for `*.bin` files. Skips names that are not session ids."""
    base = _sessions_root()
    if not base.is_dir():
        return []
    try:
        base_resolved = base.resolve()
    except OSError:
        return []
    rows: list[dict[str, object]] = []
    for path in base.glob("*.bin"):
        session_id = path.name[: -len(".bin")]
        if not SESSION_ID_RE.fullmatch(session_id):
            continue
        try:
            resolved = path.resolve()
            if resolved.parent != base_resolved or not resolved.is_file():
                continue
            mtime = resolved.stat().st_mtime
        except OSError:
            continue
        rows.append(
            {
                "id": session_id,
                "auto_host": session_id.startswith("auto-"),
                "mtime": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "mtime_epoch": mtime,
            }
        )
    rows.sort(key=lambda row: float(row["mtime_epoch"]), reverse=True)
    for row in rows:
        del row["mtime_epoch"]
    return rows


def session_count() -> int:
    return len(list_sessions())


def delete_session(session_id: str) -> None:
    """Remove one session file. Missing file or a bad id does not delete anything else."""
    path = session_file_path(session_id)
    if not path.is_file():
        raise session_invalid("Invalid session")
    path.unlink()


def _string_map(value: object, field: str, *, allow_empty: bool) -> dict[str, str]:
    if not isinstance(value, dict):
        raise bad_url(f"{field} must be an object of strings")
    if not value and not allow_empty:
        raise bad_url(f"{field} must be a non-empty object of strings")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise bad_url(f"{field} must be an object of strings")
        out[key] = item
    return out


def save_pasted_cookies(
    url: str,
    cookies: object,
    headers: object = None,
    session_id: str | None = None,
) -> str:
    """Encrypt a flat name→value cookie map into `auto-<host>.bin` (or `session_id`)."""
    parse_http_url(url)
    cookie_map = _string_map(cookies, "cookies", allow_empty=False)
    header_map = _string_map(headers, "headers", allow_empty=True) if headers is not None else {}
    target = (session_id or "").strip() or auto_session_id(url)
    write_session_file(target, cookie_map, header_map)
    return target


def capture_status(url: str) -> dict[str, object]:
    """Whether `auto-<host>.bin` is on disk, plus the host command to create it."""
    parse_http_url(url)
    safe_url = redact_url_userinfo(url)
    host = normalize_host(urlparse(safe_url).hostname)
    session_id = auto_session_id(safe_url)
    path = session_file_path(session_id)
    ready = path.is_file()
    mtime: str | None = None
    if ready:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    return {
        "url": safe_url,
        "host": host,
        "session_id": session_id,
        "filename": f"{session_id}.bin",
        "command": capture_command(safe_url),
        "ready": ready,
        "mtime": mtime,
        "session_key_configured": session_key_usable(),
    }
