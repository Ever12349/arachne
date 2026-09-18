"""Load Fernet-encrypted caller sessions from disk. Never log plaintext material."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app import config
from app.errors import session_invalid

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class SessionMaterial:
    cookies: dict[str, str]
    headers: dict[str, str]


def _sessions_dir() -> Path:
    return Path(config.SESSIONS_DIR).expanduser()


def _session_key_bytes() -> bytes:
    raw = config.SESSION_KEY
    if raw is None or not str(raw).strip():
        raise session_invalid("Invalid session")
    text = str(raw).strip()
    try:
        return text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise session_invalid("Invalid session") from exc


def _fernet() -> Fernet:
    try:
        return Fernet(_session_key_bytes())
    except (ValueError, TypeError) as exc:
        raise session_invalid("Invalid session") from exc


def _require_str_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise session_invalid("Invalid session")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise session_invalid("Invalid session")
        out[key] = item
    return out


def parse_session_payload(data: Any) -> SessionMaterial:
    if not isinstance(data, dict):
        raise session_invalid("Invalid session")
    cookies = _require_str_map(data.get("cookies", {}))
    headers = _require_str_map(data.get("headers", {}))
    return SessionMaterial(cookies=cookies, headers=headers)


def session_file_path(session_id: str, *, directory: Path | None = None) -> Path:
    if not SESSION_ID_RE.fullmatch(session_id or ""):
        raise session_invalid("Invalid session")
    base = (directory if directory is not None else _sessions_dir()).expanduser()
    try:
        base_resolved = base.resolve()
        path = (base_resolved / f"{session_id}.bin").resolve()
    except OSError as exc:
        raise session_invalid("Invalid session") from exc
    if path.parent != base_resolved:
        raise session_invalid("Invalid session")
    return path


def load_session(session_id: str | None) -> SessionMaterial:
    """Return empty material when `session_id` is omitted; otherwise decrypt `{id}.bin`."""
    if session_id is None:
        return SessionMaterial(cookies={}, headers={})

    path = session_file_path(session_id)
    if not path.is_file():
        raise session_invalid("Invalid session")

    try:
        blob = path.read_bytes()
        plain = _fernet().decrypt(blob)
        data = json.loads(plain.decode("utf-8"))
    except (OSError, InvalidToken, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise session_invalid("Invalid session") from exc

    return parse_session_payload(data)


def merge_session_material(
    session: SessionMaterial,
    cookies: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> SessionMaterial:
    """Session cookies/headers are the base; request body values override."""
    merged_cookies = {**session.cookies, **dict(cookies or {})}
    merged_headers = {**session.headers, **dict(headers or {})}
    return SessionMaterial(cookies=merged_cookies, headers=merged_headers)


def encrypt_session_payload(cookies: dict[str, str], headers: dict[str, str], *, key: bytes | None = None) -> bytes:
    material = SessionMaterial(cookies=dict(cookies), headers=dict(headers))
    payload = json.dumps(
        {"cookies": material.cookies, "headers": material.headers},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    fernet = Fernet(key) if key is not None else _fernet()
    return fernet.encrypt(payload)


def write_session_file(
    session_id: str,
    cookies: dict[str, str],
    headers: dict[str, str],
    *,
    directory: Path | str | None = None,
    key: bytes | None = None,
) -> Path:
    """Encrypt `{cookies, headers}` and write `{id}.bin`. Used by scripts/write_session.py."""
    parse_session_payload({"cookies": cookies, "headers": headers})
    base = Path(directory) if directory is not None else _sessions_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = session_file_path(session_id, directory=base)
    path.write_bytes(encrypt_session_payload(cookies, headers, key=key))
    return path
