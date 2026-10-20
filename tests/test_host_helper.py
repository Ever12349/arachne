"""Host helper auth, capture job status, and one-time install. No real browser."""

from __future__ import annotations

import json
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from app.capture_broker import CaptureBroker, CaptureBusy, CaptureFailed, wall_cleared
from app.dotenv_file import parse_env_text
from app.hostcfg import HELPER_SERVICE
from scripts.capture_session import main as capture_main
from scripts.host_helper import create_server, dispatch, main as helper_main
from scripts.install_host_helper import install, render_plist

SECRET_COOKIE = "super-secret-cookie-value"
TOKEN = "helper-token-not-a-cookie"


def _runner(cookies: dict[str, str] | None = None):
    def runner(job):
        job.mark("awaiting_user")
        if not job.done_event.wait(3):
            raise CaptureFailed("timed out", code="timed_out")
        if job.abort_event.is_set():
            raise CaptureFailed("采集已取消。", code="cancelled")
        return dict(cookies or {"sid": SECRET_COOKIE})

    return runner


def _enable_sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.config.SESSION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))


def test_wall_cleared_only_after_a_challenge_goes_away():
    long_page = "<html><body>" + ("article text " * 8) + "</body></html>"
    challenge = "<html><body>Just a moment, checking your browser before accessing</body></html>"
    assert wall_cleared(challenge, {"a": "b"}, saw_challenge=True) is False
    assert wall_cleared(long_page, {"a": "b"}, saw_challenge=False) is False
    assert wall_cleared(long_page, {}, saw_challenge=True) is False
    assert wall_cleared("short", {"a": "b"}, saw_challenge=True) is False
    assert wall_cleared(long_page, {"a": "b"}, saw_challenge=True) is True


def test_broker_done_writes_session_without_exposing_cookies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    job = broker.start("https://www.example.com/story")
    assert job.public()["session_id"] == "auto-example-com"
    assert job.public()["status"] in {"starting", "awaiting_user"}
    same = broker.start("https://example.com/other-path")
    assert same.id == job.id
    with pytest.raises(CaptureBusy):
        broker.start("https://other.test/page")
    assert broker.done(job.id) is job
    assert job.finished.wait(3)
    state = job.public()
    assert state["status"] == "ready"
    assert state["ready"] is True
    assert SECRET_COOKIE not in json.dumps(state)
    assert (tmp_path / "auto-example-com.bin").is_file()


def test_broker_refuses_to_open_a_browser_without_a_session_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SESSION_KEY", "")
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    calls = {"n": 0}

    def runner(job):
        calls["n"] += 1
        return {"sid": SECRET_COOKIE}

    broker = CaptureBroker(runner=runner)
    with pytest.raises(CaptureFailed) as caught:
        broker.start("https://example.com/a")
    assert caught.value.code == "session_key_missing"
    assert calls["n"] == 0
    assert list(tmp_path.iterdir()) == []


def test_dispatch_rejects_non_loopback_before_starting_a_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    status, body, _headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        body=b'{"url":"https://example.com/a"}',
        peer="203.0.113.8",
        token=TOKEN,
    )
    assert status == 403
    assert body["error"] == "localhost_only"
    assert broker._jobs == {}


def test_dispatch_rejects_foreign_origin_and_bad_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    foreign, foreign_body, foreign_headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Origin": "https://evil.test",
            "Content-Type": "application/json",
        },
        body=b'{"url":"https://example.com/a"}',
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert foreign == 403
    assert foreign_body["error"] == "localhost_only"
    assert "Access-Control-Allow-Origin" not in foreign_headers
    assert broker._jobs == {}

    missing, missing_body, _headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={"Content-Type": "application/json"},
        body=b'{"url":"https://example.com/a"}',
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert missing == 401
    assert missing_body == {"error": "unauthorized"}
    assert TOKEN not in json.dumps(missing_body)
    assert broker._jobs == {}

    wrong, wrong_body, _headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={"X-Arachne-Helper-Token": "nope"},
        body=b'{"url":"https://example.com/a"}',
        peer="::1",
        token=TOKEN,
    )
    assert wrong == 401
    assert wrong_body["error"] == "unauthorized"
    assert broker._jobs == {}


def test_dispatch_health_is_open_on_loopback_and_job_flow_hides_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    health, health_body, _headers = dispatch(
        broker,
        method="GET",
        path="/health",
        headers={},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert health == 200
    assert health_body["service"] == HELPER_SERVICE

    preflight, _body, cors = dispatch(
        broker,
        method="OPTIONS",
        path="/capture",
        headers={"Origin": "http://127.0.0.1:8000"},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert preflight == 204
    assert cors["Access-Control-Allow-Origin"] == "http://127.0.0.1:8000"

    started, payload, _headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={"Authorization": f"Bearer {TOKEN}", "Origin": "http://localhost:8000"},
        body=json.dumps({"url": "https://example.com/a"}).encode(),
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert started == 200
    assert payload["status"] in {"starting", "awaiting_user"}
    assert SECRET_COOKIE not in json.dumps(payload)
    job_id = payload["job_id"]

    polling, polled, _headers = dispatch(
        broker,
        method="GET",
        path=f"/capture/{job_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert polling == 200
    assert polled["job_id"] == job_id
    assert SECRET_COOKIE not in json.dumps(polled)

    finished, after_done, _headers = dispatch(
        broker,
        method="POST",
        path=f"/capture/{job_id}/done",
        headers={"Authorization": f"Bearer {TOKEN}"},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert finished == 200
    assert after_done["job_id"] == job_id
    broker.get(job_id).finished.wait(3)
    ready, ready_body, _headers = dispatch(
        broker,
        method="GET",
        path=f"/capture/{job_id}",
        headers={"X-Arachne-Helper-Token": TOKEN},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert ready == 200
    assert ready_body["status"] == "ready"
    assert ready_body["ready"] is True
    assert SECRET_COOKIE not in json.dumps(ready_body)
    assert (tmp_path / "auto-example-com.bin").is_file()

    missing, missing_body, _headers = dispatch(
        broker,
        method="GET",
        path="/capture/does-not-exist",
        headers={"Authorization": f"Bearer {TOKEN}"},
        body=b"",
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert missing == 404
    assert missing_body["error"] == "not_found"


def test_dispatch_bad_url_does_not_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    status, body, _headers = dispatch(
        broker,
        method="POST",
        path="/capture",
        headers={"Authorization": f"Bearer {TOKEN}"},
        body=b'{"url":"file:///etc/passwd"}',
        peer="127.0.0.1",
        token=TOKEN,
    )
    assert status == 400
    assert body["error"] == "bad_url"
    assert broker._jobs == {}


def test_server_binds_loopback_and_serves_job_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _enable_sessions(monkeypatch, tmp_path)
    broker = CaptureBroker(runner=_runner())
    server = create_server("127.0.0.1", 0, TOKEN, broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        assert host == "127.0.0.1"
        base = f"http://127.0.0.1:{port}"
        health = httpx.get(base + "/health", trust_env=False, timeout=2)
        assert health.status_code == 200
        assert health.json()["service"] == HELPER_SERVICE
        denied = httpx.post(base + "/capture", json={"url": "https://example.com/a"}, trust_env=False, timeout=2)
        assert denied.status_code == 401
        assert broker._jobs == {}
        started = httpx.post(
            base + "/capture",
            json={"url": "https://example.com/a"},
            headers={"Authorization": f"Bearer {TOKEN}"},
            trust_env=False,
            timeout=2,
        )
        assert started.status_code == 200
        job_id = started.json()["job_id"]
        assert SECRET_COOKIE not in started.text
        done = httpx.post(
            f"{base}/capture/{job_id}/done",
            headers={"Authorization": f"Bearer {TOKEN}"},
            trust_env=False,
            timeout=2,
        )
        assert done.status_code == 200
        final = done.json()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and final.get("status") not in {"ready", "failed"}:
            final = httpx.get(
                f"{base}/capture/{job_id}",
                headers={"Authorization": f"Bearer {TOKEN}"},
                trust_env=False,
                timeout=2,
            ).json()
            time.sleep(0.05)
        assert final["status"] == "ready"
        assert SECRET_COOKIE not in json.dumps(final)
        evil = httpx.request(
            "OPTIONS",
            base + "/capture",
            headers={"Origin": "https://evil.test"},
            trust_env=False,
            timeout=2,
        )
        assert evil.status_code == 403
        assert "access-control-allow-origin" not in {key.lower() for key in evil.headers}
    finally:
        server.shutdown()
        server.server_close()


def test_create_server_refuses_wildcard_and_empty_token():
    broker = CaptureBroker(runner=_runner())
    with pytest.raises(ValueError):
        create_server("0.0.0.0", 0, TOKEN, broker)
    with pytest.raises(ValueError):
        create_server("127.0.0.1", 0, "", broker)


def test_main_refuses_non_loopback_host(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARACHNE_HOST_HELPER_HOST", "0.0.0.0")
    monkeypatch.setenv("ARACHNE_HOST_HELPER_TOKEN", TOKEN)
    assert helper_main([]) == 1


def test_capture_script_missing_key_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SESSION_KEY", "")
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    assert capture_main(["--url", "https://example.com/a"]) == 1
    assert list(tmp_path.iterdir()) == []


def test_install_writes_private_files_and_does_not_print_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("OTHER=keep\nARACHNE_HOST_HELPER_TOKEN=already-set-token\n", encoding="utf-8")
    home = tmp_path / "home"
    assert install(repo=repo, home=home, launch=False, environ={}) == 0
    env_text = (repo / ".env").read_text(encoding="utf-8")
    parsed = parse_env_text(env_text)
    assert parsed["OTHER"] == "keep"
    assert parsed["ARACHNE_HOST_HELPER_TOKEN"] == "already-set-token"
    assert parsed["ARACHNE_HOST_HELPER_HOST"] == "127.0.0.1"
    Fernet(parsed["ARACHNE_SESSION_KEY"].encode("ascii"))
    plist_path = home / "Library" / "LaunchAgents" / "com.arachne.host-helper.plist"
    plist = plist_path.read_text(encoding="utf-8")
    assert "127.0.0.1" in plist
    assert "0.0.0.0" not in plist
    assert "already-set-token" in plist
    assert parsed["ARACHNE_SESSION_KEY"] in plist
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600
    printed = capsys.readouterr()
    combined = printed.out + printed.err
    assert "already-set-token" not in combined
    assert parsed["ARACHNE_SESSION_KEY"] not in combined


def test_render_plist_escapes_and_forces_loopback():
    plist = render_plist(
        python="/usr/bin/python3",
        script="/repo/scripts/host_helper.py",
        working_directory="/repo",
        updates={
            "ARACHNE_HOST_HELPER_TOKEN": "a&b",
            "ARACHNE_SESSION_KEY": "key",
            "ARACHNE_SESSIONS_DIR": "/repo/data/sessions",
            "ARACHNE_HOST_HELPER_PORT": "18765",
            "ARACHNE_HOST_HELPER_HOST": "0.0.0.0",
        },
        log_path="/home/me/Library/Logs/arachne-host-helper.log",
    )
    assert "a&amp;b" in plist
    assert "0.0.0.0" not in plist
    assert ">127.0.0.1<" in plist
