"""Encrypted session files: load, merge, POST-only, session_invalid, no secret logs."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import USER_AGENT
from app.errors import ArachneError
from app.fetch import ssrf_request_hook
from app.main import app
from app.sessions import load_session, write_session_file
from tests.conftest import SAMPLE_HTML, set_high_qps


def _configure_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr("app.config.SESSION_KEY", key)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    return key


def _capturing_client(monkeypatch: pytest.MonkeyPatch, captured: dict[str, str | None]):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured["cookie"] = request.headers.get("cookie")
            captured["authorization"] = request.headers.get("authorization")
            captured["user-agent"] = request.headers.get("user-agent")
            captured["accept"] = request.headers.get("accept")
            captured["referer"] = request.headers.get("referer")
            return httpx.Response(
                200,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    return TestClient(app)


def test_session_base_merged_and_body_overrides(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    _configure_sessions(tmp_path, monkeypatch)
    write_session_file(
        "demo",
        {"sid": "from-session", "keep": "session"},
        {"Authorization": "Bearer session-token", "Accept": "text/plain", "User-Agent": "SessionUA/1"},
    )
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/",
                "session_id": "demo",
                "cookies": {"sid": "from-body"},
                "headers": {"Accept": "text/html"},
            },
        )
    assert response.status_code == 200
    cookie = captured.get("cookie") or ""
    assert "sid=from-body" in cookie
    assert "keep=session" in cookie
    assert captured["authorization"] == "Bearer session-token"
    assert captured["accept"] == "text/html"
    assert captured["user-agent"] == "SessionUA/1"


@pytest.mark.parametrize(
    "session_id",
    ["", "bad id", "../etc", "x" * 65, "id.with.dot"],
)
def test_bad_session_id_is_session_invalid(
    tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch, session_id: str
):
    _configure_sessions(tmp_path, monkeypatch)
    with _capturing_client(monkeypatch, {}) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": session_id},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"


def test_missing_key_is_session_invalid(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SESSION_KEY", "")
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    write_session_file(
        "demo",
        {"sid": "abc"},
        {},
        key=Fernet.generate_key(),
        directory=tmp_path,
    )
    with _capturing_client(monkeypatch, {}) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": "demo"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"


def test_unknown_file_is_session_invalid(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    _configure_sessions(tmp_path, monkeypatch)
    with _capturing_client(monkeypatch, {}) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": "missing"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"


def test_decrypt_fail_is_session_invalid(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    _configure_sessions(tmp_path, monkeypatch)
    write_session_file("demo", {"sid": "abc"}, {})
    monkeypatch.setattr("app.config.SESSION_KEY", Fernet.generate_key().decode("ascii"))
    with _capturing_client(monkeypatch, {}) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": "demo"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"


def test_garbage_file_is_session_invalid(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    _configure_sessions(tmp_path, monkeypatch)
    (tmp_path / "demo.bin").write_bytes(b"not-fernet")
    with _capturing_client(monkeypatch, {}) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": "demo"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "session_invalid"


def test_session_cookie_header_stripped(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch):
    _configure_sessions(tmp_path, monkeypatch)
    write_session_file(
        "demo",
        {"sid": "ok"},
        {"Cookie": "from=headers", "Host": "evil.example"},
    )
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "session_id": "demo"},
        )
    assert response.status_code == 200
    cookie = captured.get("cookie") or ""
    assert "sid=ok" in cookie
    assert "from=headers" not in cookie


def test_get_ignores_session_id_query(api_client: TestClient):
    response = api_client.get(
        "/extract",
        params={"url": "https://example.com/", "session_id": "whatever"},
    )
    assert response.status_code == 200


def test_omit_session_id_does_not_need_key(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SESSION_KEY", "")
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 200


def test_session_secrets_not_logged(tmp_path: Path, public_dns, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _configure_sessions(tmp_path, monkeypatch)
    write_session_file(
        "demo",
        {"sid": "SUPERSECRETCOOKIE"},
        {"Authorization": "Bearer SECRETTOKEN"},
    )
    caplog.set_level(logging.INFO, logger="arachne")
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/private", "session_id": "demo"},
        )
    assert response.status_code == 200
    text = caplog.text
    assert "SUPERSECRETCOOKIE" not in text
    assert "SECRETTOKEN" not in text
    assert "Bearer " not in text


def test_write_session_script_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = _configure_sessions(tmp_path, monkeypatch)
    from scripts.write_session import main

    code = main(
        [
            "--id",
            "cli-demo",
            "--cookies",
            '{"sid":"from-cli"}',
            "--headers",
            '{"Authorization":"Bearer cli"}',
        ]
    )
    assert code == 0
    loaded = load_session("cli-demo")
    assert loaded.cookies == {"sid": "from-cli"}
    assert loaded.headers == {"Authorization": "Bearer cli"}
    assert key  # used via config; not printed


def test_write_session_script_does_not_print_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    _configure_sessions(tmp_path, monkeypatch)
    from scripts.write_session import main

    code = main(["--id", "quiet", "--cookies", '{"sid":"HIDDENCOOKIE"}'])
    assert code == 0
    out = capsys.readouterr().out
    assert "HIDDENCOOKIE" not in out
    assert "quiet.bin" in out


def test_load_session_none_is_empty():
    material = load_session(None)
    assert material.cookies == {}
    assert material.headers == {}


def test_load_session_bad_id_raises():
    with pytest.raises(ArachneError) as exc:
        load_session("no spaces allowed")
    assert exc.value.code == "session_invalid"
