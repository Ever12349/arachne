"""Localhost admin UI, soft-wall detail, and capture polling. No cookie plaintext."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.admin.guard import is_loopback_peer
from app.failures import FailureRing
from app.fetch import ssrf_request_hook
from app.main import app
from app.render import cookies_for_host
from app.sessions import write_session_file
from scripts.capture_session import main as capture_main
from tests.conftest import CHALLENGE_CF_HTML, SAMPLE_HTML, set_high_qps

SECRET_COOKIE = "super-secret-cookie-value"
SECRET_KEY_MARK = "fernet-key-should-not-leak"


def _client(monkeypatch: pytest.MonkeyPatch, peer: str, handler=None) -> TestClient:
    def fake_create() -> httpx.AsyncClient:
        def default_handler(request: httpx.Request) -> httpx.Response:
            if handler is not None:
                return handler(request)
            return httpx.Response(
                200,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(default_handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    return TestClient(app, client=(peer, 50000))


@pytest.fixture
def loopback(public_dns, monkeypatch: pytest.MonkeyPatch):
    with _client(monkeypatch, "127.0.0.1") as client:
        set_high_qps(client)
        yield client


def test_loopback_peer_helper():
    assert is_loopback_peer("127.0.0.1") is True
    assert is_loopback_peer("127.0.0.8") is True
    assert is_loopback_peer("::1") is True
    assert is_loopback_peer("::ffff:127.0.0.1") is True
    assert is_loopback_peer("localhost") is True
    assert is_loopback_peer("203.0.113.5") is False
    assert is_loopback_peer("10.0.0.1") is False
    assert is_loopback_peer("testclient") is False
    assert is_loopback_peer(None) is False


@pytest.mark.parametrize(
    "path",
    ["/admin", "/admin/api/sessions", "/admin/api/failures", "/admin/sessions"],
)
def test_admin_rejects_non_loopback(public_dns, monkeypatch: pytest.MonkeyPatch, path: str):
    with _client(monkeypatch, "203.0.113.8") as client:
        response = client.get(path, headers={"Host": "127.0.0.1"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_admin_disabled_is_404(loopback: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.ADMIN_ENABLED", False)
    response = loopback.get("/admin")
    assert response.status_code == 404


def test_admin_reuses_api_key(loopback: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.REQUIRE_AUTH", True)
    monkeypatch.setattr("app.config.API_KEYS", ("admin-shared-key",))
    denied = loopback.get("/admin/api/status")
    assert denied.status_code == 401
    allowed = loopback.get("/admin/api/status", headers={"X-Arachne-Key": "admin-shared-key"})
    assert allowed.status_code == 200
    assert allowed.json()["version"] == "0.11.0"


def test_openapi_omits_admin_routes(loopback: TestClient):
    spec = loopback.get("/openapi.json").json()
    assert spec["info"]["version"] == "0.11.0"
    assert all(not path.startswith("/admin") for path in spec["paths"])


def test_dashboard_shows_version_and_flags(loopback: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.HTTP_PROXY", "http://user:super-proxy-secret@10.1.1.1:8080")
    monkeypatch.setattr("app.config.SESSION_KEY", SECRET_KEY_MARK)
    page = loopback.get("/admin")
    assert page.status_code == 200
    assert "0.11.0" in page.text
    assert "仪表盘" in page.text
    body = loopback.get("/admin/api/status")
    payload = body.json()
    assert payload["proxy_enabled"] is True
    assert payload["auto_render"] is True
    assert payload["health"]["ready"] is True
    assert "super-proxy-secret" not in body.text
    assert "10.1.1.1" not in body.text
    assert SECRET_KEY_MARK not in body.text
    assert SECRET_KEY_MARK not in page.text


def _enable_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr("app.config.SESSION_KEY", key)
    return key


def test_sessions_list_and_delete_hide_secrets(loopback: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    key = _enable_key(monkeypatch)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    write_session_file("demo", {SECRET_COOKIE: "v"}, {"Authorization": "Bearer " + SECRET_COOKIE})
    write_session_file("auto-example-com", {"sid": SECRET_COOKIE}, {})
    blob = (tmp_path / "demo.bin").read_text(encoding="utf-8")

    listed = loopback.get("/admin/api/sessions")
    assert listed.status_code == 200
    payload = listed.json()
    ids = {row["id"]: row for row in payload["sessions"]}
    assert set(ids) == {"demo", "auto-example-com"}
    assert ids["auto-example-com"]["auto_host"] is True
    assert ids["demo"]["auto_host"] is False
    assert "mtime" in ids["demo"]
    text = listed.text + loopback.get("/admin/sessions").text
    assert SECRET_COOKIE not in text
    assert key not in text
    assert blob not in text
    assert "cookies" not in listed.json()["sessions"][0]

    monkeypatch.setattr("app.config.SESSION_KEY", "not-a-valid-fernet-key")
    still = loopback.get("/admin/api/sessions")
    assert still.status_code == 200
    assert SECRET_COOKIE not in still.text

    removed = loopback.delete("/admin/api/sessions/demo")
    assert removed.status_code == 204
    assert not (tmp_path / "demo.bin").exists()
    assert (tmp_path / "auto-example-com.bin").exists()

    page = loopback.post("/admin/sessions/auto-example-com/delete", follow_redirects=True)
    assert page.status_code == 200
    assert not (tmp_path / "auto-example-com.bin").exists()


def test_bad_session_id_is_rejected(loopback: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _enable_key(monkeypatch)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    response = loopback.delete("/admin/api/sessions/id.with.dot")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"
    assert list(tmp_path.glob("*.bin")) == []


def test_paste_cookies_writes_auto_file_without_echo(
    loopback: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key = _enable_key(monkeypatch)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    response = loopback.post(
        "/admin/api/sessions",
        json={"url": "https://www.example.com/wall", "cookies": {"sid": SECRET_COOKIE}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"session_id": "auto-example-com", "filename": "auto-example-com.bin"}
    assert SECRET_COOKIE not in response.text
    assert key not in response.text
    assert (tmp_path / "auto-example-com.bin").is_file()


def test_capture_poll_flips_when_file_appears(
    loopback: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    key = _enable_key(monkeypatch)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    url = "https://example.com/article?q=1"
    missing = loopback.get("/admin/api/capture", params={"url": url})
    assert missing.status_code == 200
    status = missing.json()
    assert status["ready"] is False
    assert status["mtime"] is None
    assert status["session_id"] == "auto-example-com"
    assert status["host"] == "example.com"
    assert "scripts/capture_session.py" in status["command"]
    assert url in status["command"]
    assert key not in missing.text
    assert status["session_key_configured"] is True

    write_session_file("auto-example-com", {"sid": SECRET_COOKIE}, {})
    ready = loopback.get("/admin/api/capture", params={"url": url})
    assert ready.json()["ready"] is True
    assert ready.json()["mtime"]
    assert SECRET_COOKIE not in ready.text
    page = loopback.get("/admin/sessions/new", params={"url": url})
    assert page.status_code == 200
    assert "新建会话" in page.text
    assert "重试抽取" in page.text
    assert "scripts/capture_session.py" in page.text
    assert SECRET_COOKIE not in page.text
    assert key not in page.text


def test_soft_wall_detail_and_clickable_failures(public_dns, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(
                401,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html"},
                request=request,
            )
        return httpx.Response(
            403,
            content=CHALLENGE_CF_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client(monkeypatch, "127.0.0.1", handler) as client:
        set_high_qps(client)
        url = "https://www.example.com/story"
        response = client.post("/extract", json={"url": url})
        assert response.status_code == 403
        error = response.json()["error"]
        assert error["code"] == "challenge_detected"
        detail = error["detail"]
        assert detail["needs_session"] is True
        assert detail["host"] == "example.com"
        assert detail["auto_session_id"] == "auto-example-com"
        assert detail["status_code"] == 403
        assert "/admin" in detail["admin_hint"]

        denied = client.post("/extract", json={"url": "https://example.com/login"})
        assert denied.status_code == 502
        denied_detail = denied.json()["error"]["detail"]
        assert denied.json()["error"]["code"] == "unauthorized_upstream"
        assert denied_detail["needs_session"] is True
        assert denied_detail["host"] == "example.com"
        assert denied_detail["auto_session_id"] == "auto-example-com"

        failures = client.get("/admin/api/failures").json()["failures"]
        urls = [row["url"] for row in failures]
        assert urls[0] == "https://example.com/login"
        assert url in urls
        href = "/admin/sessions/new?" + urlencode({"url": url})
        dashboard = client.get("/admin")
        assert href in dashboard.text
        assert "为此建会话" in dashboard.text
        recent = client.get("/admin/failures")
        assert href in recent.text
        assert "为此建会话" in recent.text
        opened = client.get(href)
        assert opened.status_code == 200
        assert url in opened.text
        assert "新建会话" in opened.text


def test_try_extract_success_and_needs_session_cta(public_dns, monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                401,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html"},
                request=request,
            )
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    with _client(monkeypatch, "::1", handler) as client:
        set_high_qps(client)
        failed = client.post("/admin/extract", data={"url": "https://example.com/private", "session_id": ""})
        assert failed.status_code == 200
        assert "unauthorized_upstream" in failed.text
        assert "为此建会话" in failed.text
        ok = client.post("/admin/api/extract", json={"url": "https://example.com/private"})
        assert ok.status_code == 200
        body = ok.json()
        assert body["title"] == "OG Title"
        assert "main article text" in body["summary"]
        assert SECRET_COOKIE not in ok.text


def test_failure_urls_drop_userinfo(public_dns, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=CHALLENGE_CF_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client(monkeypatch, "127.0.0.1", handler) as client:
        set_high_qps(client)
        url = "https://user:s3cret-pass@www.example.com/story"
        response = client.post("/extract", json={"url": url})
        assert response.status_code == 403
        assert "s3cret-pass" not in response.text
        listed = client.get("/admin/api/failures")
        assert listed.status_code == 200
        assert "s3cret-pass" not in listed.text
        assert listed.json()["failures"][0]["url"] == "https://www.example.com/story"
        page = client.get("/admin")
        assert "s3cret-pass" not in page.text
        capture = client.get("/admin/api/capture", params={"url": url})
        assert "s3cret-pass" not in capture.text
        assert "https://www.example.com/story" in capture.json()["command"]


def test_failure_ring_keeps_last_50():
    ring = FailureRing(50)
    for index in range(60):
        ring.append({"url": f"https://example.com/{index}"})
    recent = ring.list_recent()
    assert len(recent) == 50
    assert recent[0]["url"].endswith("/59")
    assert recent[-1]["url"].endswith("/10")


def test_cookies_for_host_filters_other_domains():
    raw = [
        {"name": "a", "value": "1", "domain": ".example.com"},
        {"name": "b", "value": "2", "domain": "other.test"},
        {"name": "a", "value": "3", "domain": "www.example.com"},
        {"name": "hostonly", "value": "4"},
    ]
    assert cookies_for_host(raw, "www.example.com") == {"a": "3", "hostonly": "4"}


def test_capture_script_rejects_non_http_without_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    assert capture_main(["--url", "file:///etc/passwd"]) == 1
    assert list(tmp_path.iterdir()) == []


def test_manual_paste_fallback_is_on_empty_new_session(loopback: TestClient):
    page = loopback.get("/admin/sessions/new")
    assert "没有失败记录" in page.text
    assert "<details open>" in page.text or "手动粘贴 Cookie" in page.text
    pasted = loopback.post(
        "/admin/sessions",
        data={"url": "https://example.com/x", "cookies_json": json.dumps({"sid": SECRET_COOKIE})},
    )
    assert pasted.status_code == 400
    assert SECRET_COOKIE not in pasted.text
