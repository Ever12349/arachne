"""API-key auth, PUBLIC flags, and fail-closed startup."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import any_key_matches, key_matches, parse_api_keys
from app.config import _env_bool
from app.logging_setup import JsonFormatter, request_id_var
from app.main import app
from tests.conftest import set_high_qps

KEY = "test-secret-key"


def _enable_auth(
    monkeypatch: pytest.MonkeyPatch,
    keys: tuple[str, ...] = (KEY,),
    *,
    metrics_public: bool = False,
    stats_public: bool = False,
) -> None:
    monkeypatch.setattr("app.config.REQUIRE_AUTH", True)
    monkeypatch.setattr("app.config.API_KEYS", keys)
    monkeypatch.setattr("app.config.METRICS_PUBLIC", metrics_public)
    monkeypatch.setattr("app.config.STATS_PUBLIC", stats_public)


def test_parse_api_keys_trims_and_skips_empty():
    assert parse_api_keys(" a, ,b,c ") == ("a", "b", "c")
    assert parse_api_keys("") == ()
    assert parse_api_keys("   ") == ()
    assert parse_api_keys(None) == ()


def test_key_matches_rejects_different_lengths():
    assert key_matches("ab", "abc") is False
    assert key_matches("abc", "abc") is True
    assert any_key_matches("nope", ("abc", "secret")) is False
    assert any_key_matches("secret", ("abc", "secret")) is True


def test_env_bool(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ARACHNE_TEST_FLAG", "TRUE")
    assert _env_bool("ARACHNE_TEST_FLAG", False) is True
    monkeypatch.setenv("ARACHNE_TEST_FLAG", "Yes")
    assert _env_bool("ARACHNE_TEST_FLAG", False) is True
    monkeypatch.setenv("ARACHNE_TEST_FLAG", "1")
    assert _env_bool("ARACHNE_TEST_FLAG", False) is True
    monkeypatch.setenv("ARACHNE_TEST_FLAG", "no")
    assert _env_bool("ARACHNE_TEST_FLAG", True) is False
    monkeypatch.delenv("ARACHNE_TEST_FLAG", raising=False)
    assert _env_bool("ARACHNE_TEST_FLAG", True) is True


def test_auth_off_ignores_wrong_key(api_client: TestClient):
    set_high_qps(api_client)
    response = api_client.get(
        "/extract",
        params={"url": "https://example.com/"},
        headers={"Authorization": "Bearer wrong", "X-Arachne-Key": "also-wrong"},
    )
    assert response.status_code == 200


def test_auth_off_anonymous_ok(api_client: TestClient):
    response = api_client.get("/extract", params={"url": "https://example.com/"})
    assert response.status_code == 200


def test_auth_on_accepts_bearer(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch)
    set_high_qps(api_client)
    denied = api_client.get("/extract", params={"url": "https://example.com/"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "unauthorized"
    ok = api_client.get(
        "/extract",
        params={"url": "https://example.com/"},
        headers={"Authorization": f"Bearer {KEY}"},
    )
    assert ok.status_code == 200


def test_auth_on_accepts_x_arachne_key(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch)
    ok = api_client.get(
        "/extract",
        params={"url": "https://example.com/"},
        headers={"X-Arachne-Key": KEY},
    )
    assert ok.status_code == 200


def test_auth_on_invalid_key_is_401(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch)
    response = api_client.get(
        "/extract",
        params={"url": "https://example.com/"},
        headers={"Authorization": "Bearer not-the-key"},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "message" in body["error"]
    assert isinstance(body["error"]["detail"], dict)


def test_auth_on_health_and_ready_exempt(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch)
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/ready").json() == {"status": "ok"}


def test_stats_requires_auth_unless_public(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch, stats_public=False)
    assert api_client.get("/stats").status_code == 401
    _enable_auth(monkeypatch, stats_public=True)
    assert api_client.get("/stats").status_code == 200
    keyed = api_client.get("/stats", headers={"Authorization": f"Bearer {KEY}"})
    assert keyed.status_code == 200


def test_metrics_requires_auth_unless_public(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch, metrics_public=False)
    assert api_client.get("/metrics").status_code == 401
    _enable_auth(monkeypatch, metrics_public=True)
    public = api_client.get("/metrics")
    assert public.status_code == 200
    assert "arachne_requests_total" in public.text


def test_metrics_with_key_when_private(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    _enable_auth(monkeypatch, metrics_public=False)
    response = api_client.get("/metrics", headers={"X-Arachne-Key": KEY})
    assert response.status_code == 200
    assert "arachne_" in response.text


def test_require_auth_without_keys_refuses_startup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.REQUIRE_AUTH", True)
    monkeypatch.setattr("app.config.API_KEYS", ())
    with pytest.raises(RuntimeError, match="ARACHNE_REQUIRE_AUTH"):
        with TestClient(app):
            pass


def test_json_formatter_includes_request_id_not_secrets():
    token = request_id_var.set("req-123")
    try:
        record = logging.LogRecord("arachne", logging.INFO, __file__, 0, "extract url=https://example.com", (), None)
        line = JsonFormatter().format(record)
        data = json.loads(line)
    finally:
        request_id_var.reset(token)
    assert data["message"] == "extract url=https://example.com"
    assert data["request_id"] == "req-123"
    assert data["level"] == "INFO"
    assert "Authorization" not in line
    assert "cookie" not in line.lower()
