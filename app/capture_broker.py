"""One headed-browser capture at a time. Jobs never expose cookie values.

The runner waits until the person clicks Done or a challenge page clears on
its own. It does not solve CAPTCHAs, submit logins, or hide the browser.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from urllib.parse import urlparse

from app.antibot import looks_like_challenge
from app.auto_session import auto_session_id
from app.errors import ArachneError
from app.render import cookies_for_host
from app.sessions import session_key_usable, write_session_file
from app.ssrf import parse_http_url, redact_url_userinfo

logger = logging.getLogger("arachne")

JOB_ID_RE_TEXT = r"[A-Za-z0-9_-]{8,80}"
_ACTIVE = frozenset({"starting", "awaiting_user", "saving"})
_MAX_JOBS = 32
_CAPTURE_TIMEOUT_SECONDS = 15 * 60


class CaptureFailed(Exception):
    """Expected start or run failure. `code` is a stable token, not a secret."""

    def __init__(self, message: str, *, code: str = "capture_failed") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class CaptureBusy(Exception):
    """Another capture is still using the headed browser."""

    def __init__(self, job: CaptureJob) -> None:
        super().__init__(job.id)
        self.job = job


class CaptureJob:
    """In-memory capture. Cookie maps are never stored on the job."""

    def __init__(self, url: str, session_id: str) -> None:
        self.id = secrets.token_urlsafe(12)
        self.url = url
        self.session_id = session_id
        self.filename = f"{session_id}.bin"
        self.status = "starting"
        self.error: str | None = None
        self.created = time.monotonic()
        self.done_event = threading.Event()
        self.abort_event = threading.Event()
        self.finished = threading.Event()
        self._lock = threading.Lock()

    def mark(self, status: str) -> None:
        with self._lock:
            self.status = status
        logger.info("capture_job status=%s id=%s", status, self.id)

    def fail(self, message: str) -> None:
        with self._lock:
            self.status = "failed"
            self.error = message
        logger.info("capture_job status=failed id=%s", self.id)

    def request_done(self) -> None:
        """Ask the runner to save cookies now. Does not cancel the job."""
        self.done_event.set()

    def abort(self) -> None:
        """Close the browser without writing a session file."""
        self.abort_event.set()
        self.done_event.set()

    def public(self) -> dict[str, object]:
        with self._lock:
            return {
                "job_id": self.id,
                "url": self.url,
                "session_id": self.session_id,
                "filename": self.filename,
                "status": self.status,
                "ready": self.status == "ready",
                "error": self.error,
            }


def wall_cleared(html: str, cookies: dict[str, str], *, saw_challenge: bool) -> bool:
    """True when a challenge was seen and the page no longer looks like one.

    A normal login form is not auto-saved: the person clicks Done for that.
    Empty or tiny documents are ignored so a navigation gap does not write.
    """
    if not saw_challenge or not cookies:
        return False
    if not html or len(html.strip()) < 40:
        return False
    return not looks_like_challenge(html)


def playwright_importable() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def playwright_runner(job: CaptureJob) -> object:
    """Open a visible Chromium window and return Playwright's cookie list.

    Returns when Done is requested, the challenge markers disappear, the job
    is aborted, or the wait expires. Cookie values stay in the return value
    only long enough for the broker to encrypt them.
    """
    if not playwright_importable():
        raise CaptureFailed(
            "宿主机没有 Playwright。",
            code="playwright_missing",
        )
    from playwright.sync_api import sync_playwright

    saw_challenge = False
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=False)
        except Exception as exc:
            raise CaptureFailed(
                "没能在宿主机打开有头浏览器。",
                code="browser_failed",
            ) from exc
        try:
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(job.url, wait_until="domcontentloaded", timeout=60_000)
            except Exception:
                logger.info("capture_job navigation_incomplete id=%s", job.id)
            job.mark("awaiting_user")
            deadline = time.monotonic() + _CAPTURE_TIMEOUT_SECONDS
            while True:
                if job.abort_event.is_set():
                    raise CaptureFailed("采集已取消。", code="cancelled")
                html = _safe_content(page)
                if looks_like_challenge(html):
                    saw_challenge = True
                host = urlparse(job.url).hostname or ""
                filtered = cookies_for_host(_safe_cookies(context), host)
                if job.done_event.is_set() or wall_cleared(html, filtered, saw_challenge=saw_challenge):
                    break
                if time.monotonic() >= deadline:
                    raise CaptureFailed("等待通过验证超时。", code="timed_out")
                job.done_event.wait(timeout=1.0)
            if job.abort_event.is_set():
                raise CaptureFailed("采集已取消。", code="cancelled")
            return context.cookies()
        finally:
            browser.close()


def _safe_content(page: object) -> str:
    try:
        content = page.content()  # type: ignore[attr-defined]
    except Exception:
        return ""
    return content if isinstance(content, str) else ""


def _safe_cookies(context: object) -> list[object]:
    try:
        raw = context.cookies()  # type: ignore[attr-defined]
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


class CaptureBroker:
    """Runs `runner` on a daemon thread and writes `auto-<host>.bin`."""

    def __init__(self, runner: object | None = None) -> None:
        self._runner = playwright_runner if runner is None else runner
        self._jobs: dict[str, CaptureJob] = {}
        self._lock = threading.Lock()

    def start(self, url: str) -> CaptureJob:
        safe = redact_url_userinfo(url.strip())
        parse_http_url(safe)
        if not session_key_usable():
            raise CaptureFailed(
                "先配置 ARACHNE_SESSION_KEY，再开始采集。",
                code="session_key_missing",
            )
        if self._runner is playwright_runner and not playwright_importable():
            raise CaptureFailed(
                "宿主机没有 Playwright。",
                code="playwright_missing",
            )
        session_id = auto_session_id(safe)
        with self._lock:
            for existing in self._jobs.values():
                if existing.status not in _ACTIVE:
                    continue
                if existing.session_id == session_id:
                    return existing
                raise CaptureBusy(existing)
            job = CaptureJob(safe, session_id)
            self._jobs[job.id] = job
            self._prune_locked()
        thread = threading.Thread(target=self._run, args=(job,), name=f"capture-{job.id}", daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> CaptureJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def done(self, job_id: str) -> CaptureJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.request_done()
        return job

    def _prune_locked(self) -> None:
        if len(self._jobs) <= _MAX_JOBS:
            return
        finished = [job for job in self._jobs.values() if job.status not in _ACTIVE]
        finished.sort(key=lambda job: job.created)
        while len(self._jobs) > _MAX_JOBS and finished:
            old = finished.pop(0)
            self._jobs.pop(old.id, None)

    def _run(self, job: CaptureJob) -> None:
        try:
            raw = self._runner(job)  # type: ignore[operator]
            host = urlparse(job.url).hostname or ""
            cookies = _cookie_map(raw, host)
            if not cookies:
                job.fail("这个主机没有 Cookie，没有写入文件。")
                return
            job.mark("saving")
            write_session_file(job.session_id, cookies, {})
            job.mark("ready")
        except CaptureFailed as exc:
            job.fail(exc.message)
        except ArachneError:
            job.fail("写入会话失败。检查 ARACHNE_SESSION_KEY。")
        except Exception as exc:
            logger.info("capture_job crashed id=%s error_type=%s", job.id, type(exc).__name__)
            job.fail("采集失败。")
        finally:
            job.finished.set()


def _cookie_map(raw: object, host: str) -> dict[str, str]:
    if isinstance(raw, dict):
        out: dict[str, str] = {}
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                out[key] = value
        return out
    return cookies_for_host(raw, host)


_LOCAL: CaptureBroker | None = None
_LOCAL_LOCK = threading.Lock()


def get_local_broker() -> CaptureBroker:
    """Broker used when admin itself is running on the host, not in Docker."""
    global _LOCAL
    with _LOCAL_LOCK:
        if _LOCAL is None:
            _LOCAL = CaptureBroker()
        return _LOCAL


def set_local_broker(broker: CaptureBroker | None) -> None:
    """Tests replace the process-wide broker. `None` drops it."""
    global _LOCAL
    with _LOCAL_LOCK:
        _LOCAL = broker
