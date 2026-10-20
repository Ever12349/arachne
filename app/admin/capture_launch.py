"""Start a headed capture from /admin without putting a shell command in the page.

Docker cannot show the Mac UI. When the helper is down and this process is
inside a container, the response asks the browser (which is on the host) to
call the helper. The shared secret is included only in that relay response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from app.auto_session import auto_session_id
from app.capture_broker import JOB_ID_RE_TEXT, CaptureBusy, CaptureFailed, get_local_broker
from app.hostcfg import HELPER_SERVICE, capture_mode, helper_base_url, host_helper_token, running_in_docker
from app.sessions import session_key_usable
from app.ssrf import parse_http_url, redact_url_userinfo

_JOB_ID_RE = re.compile(JOB_ID_RE_TEXT)

_MESSAGES = {
    "session_key_missing": "先配置 ARACHNE_SESSION_KEY，再点开始。页面不会显示密钥。",
    "playwright_missing": "宿主机没有 Playwright。安装 requirements-playwright.txt，或先安装采集助手。",
    "browser_failed": "没能在宿主机打开有头浏览器。",
    "helper_unavailable": "本机采集助手没有响应。在 Mac 上一次性执行：python scripts/install_host_helper.py",
    "helper_unauthorized": "采集助手在运行，但 ARACHNE_HOST_HELPER_TOKEN 不一致。",
    "helper_unreachable": "连不上本机采集助手。确认它只监听 127.0.0.1。",
    "capture_busy": "已经有一个采集在进行。",
    "unauthorized": "采集助手拒绝了这个请求。",
    "not_found": "没有这个采集任务。",
}


@dataclass(frozen=True)
class LaunchResult:
    status_code: int
    body: dict[str, object]


def probe_helper() -> bool:
    """True when the host helper's /health answers on loopback."""
    try:
        response = httpx.get(f"{helper_base_url()}/health", timeout=0.4, trust_env=False)
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except Exception:
        return False
    return isinstance(body, dict) and body.get("service") == HELPER_SERVICE


def helper_request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    """Call the helper. The token is a header, never part of the URL or the error text."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = httpx.request(
            method,
            helper_base_url() + path,
            headers=headers,
            json=payload,
            timeout=3.0,
            trust_env=False,
        )
    except httpx.HTTPError:
        return 502, {"ok": False, "error": "helper_unreachable", "message": _MESSAGES["helper_unreachable"]}
    try:
        data = response.json()
    except Exception:
        return 502, {"ok": False, "error": "helper_unreachable", "message": _MESSAGES["helper_unreachable"]}
    if not isinstance(data, dict):
        return 502, {"ok": False, "error": "helper_unreachable", "message": _MESSAGES["helper_unreachable"]}
    return response.status_code, data


def start_capture(url: str) -> LaunchResult:
    """Open a headed browser for `url`. Does not return cookie values or the Fernet key."""
    parse_http_url(url)
    safe = redact_url_userinfo(url.strip())
    if not session_key_usable():
        return _error("session_key_missing", 400)
    if probe_helper():
        token = host_helper_token()
        if not token:
            return _error("helper_unauthorized", 503)
        status, data = helper_request("POST", "/capture", token, {"url": safe})
        return _from_helper(status, data, relay="admin")
    if _must_use_helper():
        token = host_helper_token()
        if not token:
            return _error("helper_unavailable", 503)
        session_id = auto_session_id(safe)
        return LaunchResult(
            200,
            {
                "ok": True,
                "relay": "browser",
                "helper_origin": helper_base_url(),
                "token": token,
                "url": safe,
                "session_id": session_id,
                "filename": f"{session_id}.bin",
                "message": "正在从这台电脑的浏览器连接宿主机助手。",
            },
        )
    try:
        job = get_local_broker().start(safe)
    except CaptureBusy as exc:
        body = exc.job.public()
        body["ok"] = False
        body["relay"] = "local"
        body["error"] = "capture_busy"
        body["message"] = _MESSAGES["capture_busy"]
        return LaunchResult(409, body)
    except CaptureFailed as exc:
        code = exc.code if exc.code in _MESSAGES else "playwright_missing"
        status = 400 if code == "session_key_missing" else 501
        return _error(code, status)
    body = job.public()
    body["ok"] = True
    body["relay"] = "local"
    body["message"] = "正在这台电脑上打开浏览器。"
    return LaunchResult(200, body)


def capture_job(job_id: str) -> LaunchResult:
    local = _local_job(job_id)
    if local is not None:
        body = local.public()
        body["ok"] = True
        body["relay"] = "local"
        return LaunchResult(200, body)
    return _proxy_job("GET", job_id, None)


def finish_capture(job_id: str) -> LaunchResult:
    local = _local_job(job_id)
    if local is not None:
        local.request_done()
        body = local.public()
        body["ok"] = True
        body["relay"] = "local"
        body["message"] = "正在保存会话。"
        return LaunchResult(200, body)
    return _proxy_job("POST", job_id, {})


def _must_use_helper() -> bool:
    """Containers cannot open a Mac window. `helper` mode never spawns in-process."""
    if running_in_docker():
        return True
    return capture_mode() == "helper"


def _local_job(job_id: str):
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        return None
    return get_local_broker().get(job_id)


def _proxy_job(method: str, job_id: str, payload: dict[str, object] | None) -> LaunchResult:
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        return LaunchResult(404, {"ok": False, "error": "not_found", "message": _MESSAGES["not_found"]})
    if not probe_helper():
        return LaunchResult(404, {"ok": False, "error": "not_found", "message": _MESSAGES["not_found"]})
    token = host_helper_token()
    if not token:
        return _error("helper_unauthorized", 503)
    path = f"/capture/{job_id}" if method == "GET" else f"/capture/{job_id}/done"
    status, data = helper_request(method, path, token, payload)
    return _from_helper(status, data, relay="admin")


def _from_helper(status: int, data: dict[str, object], *, relay: str) -> LaunchResult:
    body = {key: value for key, value in data.items() if key != "token"}
    body["relay"] = relay
    body["ok"] = status < 400 and body.get("error") in {None, ""}
    if status >= 400 and "message" not in body:
        code = body.get("error")
        if isinstance(code, str) and code in _MESSAGES:
            body["message"] = _MESSAGES[code]
    if status >= 500:
        status = 502
    return LaunchResult(status, body)


def _error(code: str, status: int) -> LaunchResult:
    return LaunchResult(
        status,
        {"ok": False, "error": code, "message": _MESSAGES.get(code, "采集失败。")},
    )
