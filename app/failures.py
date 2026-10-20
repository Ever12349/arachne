"""In-process ring of soft-wall failures for the localhost admin UI.

Not durable. Cookie and Authorization values are never stored here.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse

from app import config
from app.auto_session import auto_session_id
from app.errors import ArachneError
from app.ssrf import normalize_host, redact_url_userinfo

SOFT_WALL_CODES = frozenset({"challenge_detected", "unauthorized_upstream"})

ADMIN_HINT = (
    "On localhost open /admin, click 开始 to open a headed browser on this computer, "
    "pass the wall, then click 完成. The API does not return cookie plaintext."
)


class FailureRing:
    """Newest-first view of the last N soft-wall failures."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def append(self, item: dict[str, Any]) -> None:
        with self._lock:
            self._items.append(item)

    def list_recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._items))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_RING = FailureRing(config.FAILURE_RING_SIZE)


def get_failure_ring() -> FailureRing:
    return _RING


def new_session_path(url: str) -> str:
    return "/admin/sessions/new?" + urlencode({"url": url})


def soft_wall_detail(url: str) -> dict[str, Any]:
    """Fields added to challenge_detected and unauthorized_upstream detail."""
    host_raw = urlparse((url or "").strip()).hostname or ""
    return {
        "needs_session": True,
        "host": normalize_host(host_raw),
        "auto_session_id": auto_session_id(url),
        "admin_hint": ADMIN_HINT,
    }


def note_soft_wall(exc: ArachneError, url: str) -> None:
    """Attach admin detail and remember the failure. Other codes are unchanged."""
    if exc.code not in SOFT_WALL_CODES:
        return
    safe_url = redact_url_userinfo(url)
    detail = soft_wall_detail(safe_url)
    exc.detail.update(detail)
    status = exc.detail.get("status_code")
    get_failure_ring().append(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "url": safe_url,
            "code": exc.code,
            "message": exc.message,
            "status_code": status if isinstance(status, int) else None,
            "host": detail["host"],
            "auto_session_id": detail["auto_session_id"],
            "needs_session": True,
            "new_session_path": new_session_path(safe_url),
        }
    )
