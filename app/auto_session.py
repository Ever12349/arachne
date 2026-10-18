"""Fernet `auto-<host>` sessions: load when the caller omits `session_id`, persist after a browser accept.

Suggest may read. Only extract (including jobs, which call `run_extract`) may write.
No key is generated. Cookie and Authorization values are never logged.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from app import config
from app.errors import SESSION_INVALID, ArachneError
from app.profiles.suggest import profile_id_from_host
from app.sessions import (
    SESSION_ID_RE,
    SessionMaterial,
    load_session,
    merge_session_material,
    session_file_path,
    session_key_usable,
    write_session_file,
)

logger = logging.getLogger("arachne")

PERSIST_NO = "0"
PERSIST_YES = "1"
PERSIST_EMPTY = "skipped_empty"
PERSIST_DISABLED = "skipped_disabled"
PERSIST_NO_KEY = "skipped_no_key"


def auto_session_id(url: str) -> str:
    """`auto-` + requested hostname, cleaned like `profile_id_from_host`.

    The result is truncated to 64 characters and stripped of a trailing `-`
    so it matches `SESSION_ID_RE`.
    """
    host = urlparse((url or "").strip()).hostname
    cleaned = profile_id_from_host(host)
    session_id = ("auto-" + cleaned)[:64].rstrip("-")
    if not SESSION_ID_RE.fullmatch(session_id):
        session_id = "auto-site"
    return session_id


def try_load_auto_session(url: str) -> tuple[SessionMaterial, str | None]:
    """Load `auto-<host>` when the file exists.

    A missing file is an empty session. Decrypt or parse failure is an empty
    session plus the note `ignored_invalid` (not `session_invalid`).
    `ARACHNE_SESSION_PERSIST=false` still allows this read.
    """
    session_id = auto_session_id(url)
    empty = SessionMaterial(cookies={}, headers={})
    try:
        path = session_file_path(session_id)
    except ArachneError as exc:
        if exc.code != SESSION_INVALID:
            raise
        logger.info("auto_session=ignored_invalid id=%s", session_id)
        return empty, "ignored_invalid"
    if not path.is_file():
        return empty, None
    try:
        return load_session(session_id), None
    except ArachneError as exc:
        if exc.code != SESSION_INVALID:
            raise
        logger.info("auto_session=ignored_invalid id=%s", session_id)
        return empty, "ignored_invalid"


def resolve_session_material(
    url: str,
    session_id: str | None,
    cookies: dict[str, str] | None,
    headers: dict[str, str] | None,
) -> SessionMaterial:
    """Explicit `session_id` loads only that file. Otherwise try the auto file.

    Body cookies and headers override the file. A missing explicit file is
    `session_invalid`. A bad auto file is ignored.
    """
    if session_id is not None:
        base = load_session(session_id)
    else:
        base, _note = try_load_auto_session(url)
    return merge_session_material(base, cookies, headers)


def persist_auto_session(
    url: str,
    cookies: dict[str, str] | None,
    *,
    session_id_provided: bool,
) -> str:
    """Write non-empty browser cookies to `auto-<host>.bin`. Return the log token.

    `cookies is None` is the httpx-only path (no write). An explicit `session_id`
    never writes the auto file. An empty jar leaves any previous file in place.
    A missing or invalid `ARACHNE_SESSION_KEY` skips the write and does not raise.
    Headers are stored as `{}`.
    """
    if cookies is None or session_id_provided:
        return PERSIST_NO
    if not cookies:
        return PERSIST_EMPTY
    if not config.SESSION_PERSIST:
        return PERSIST_DISABLED
    if not session_key_usable():
        return PERSIST_NO_KEY
    write_session_file(auto_session_id(url), dict(cookies), {})
    return PERSIST_YES
