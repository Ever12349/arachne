"""POST session cookies/headers: allowlist, stripping, User-Agent override, no secret logs."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import USER_AGENT
from app.fetch import ssrf_request_hook
from app.headers_policy import FORBIDDEN_HEADERS, filter_request_headers
from app.main import app
from tests.conftest import SAMPLE_HTML, set_high_qps


def test_filter_allowlist_and_forbidden():
    out = filter_request_headers(
        {
            "authorization": "Bearer secret",
            "Accept": "text/html",
            "X-Custom": "nope",
            "Host": "evil.example",
            "Content-Length": "999",
            "Transfer-Encoding": "chunked",
            "Connection": "close",
            "Cookie": "from=header",
        }
    )
    assert out == {"Authorization": "Bearer secret", "Accept": "text/html"}
    assert "Host" not in out
    assert "Cookie" not in out
    for name in FORBIDDEN_HEADERS:
        assert name not in {k.lower() for k in out}


def test_empty_headers():
    assert filter_request_headers(None) == {}
    assert filter_request_headers({}) == {}


def _capturing_client(monkeypatch: pytest.MonkeyPatch, captured: dict[str, str | None]):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            captured["cookie"] = request.headers.get("cookie")
            captured["authorization"] = request.headers.get("authorization")
            captured["user-agent"] = request.headers.get("user-agent")
            captured["accept"] = request.headers.get("accept")
            captured["host"] = request.headers.get("host")
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


def test_post_cookies_forwarded(public_dns, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/",
                "cookies": {"session": "fromfield", "other": "v"},
            },
        )
    assert response.status_code == 200
    cookie = captured.get("cookie") or ""
    assert "session=fromfield" in cookie
    assert "other=v" in cookie


def test_cookie_header_not_forwarded_only_cookies_field(public_dns, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/",
                "headers": {"Cookie": "fromheader=1", "Host": "evil.example"},
                "cookies": {"session": "fromfield"},
            },
        )
    assert response.status_code == 200
    cookie = captured.get("cookie") or ""
    assert "fromheader" not in cookie
    assert "session=fromfield" in cookie
    assert captured.get("host") != "evil.example"


def test_allowlisted_headers_and_ua_override(public_dns, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/",
                "headers": {
                    "Authorization": "Bearer tok",
                    "Accept": "text/html",
                    "User-Agent": "CallerAgent/9",
                    "Referer": "https://caller.example/",
                    "X-Forwarded-For": "1.2.3.4",
                },
            },
        )
    assert response.status_code == 200
    assert captured["authorization"] == "Bearer tok"
    assert captured["accept"] == "text/html"
    assert captured["user-agent"] == "CallerAgent/9"
    assert captured["referer"] == "https://caller.example/"


def test_default_user_agent_when_not_overridden(public_dns, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str | None] = {}
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 200
    assert captured["user-agent"] == USER_AGENT


def test_get_extract_has_no_session_fields(api_client: TestClient):
    response = api_client.get(
        "/extract",
        params={"url": "https://example.com/", "cookies": "sid=abc", "headers": "Authorization: x"},
    )
    assert response.status_code == 200


def test_cookies_must_be_string_map(api_client: TestClient):
    response = api_client.post(
        "/extract",
        json={"url": "https://example.com/", "cookies": {"sid": ["not", "a", "string"]}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_secrets_not_logged(public_dns, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    captured: dict[str, str | None] = {}
    caplog.set_level(logging.INFO, logger="arachne")
    with _capturing_client(monkeypatch, captured) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/private",
                "cookies": {"session": "SUPERSECRETCOOKIE"},
                "headers": {"Authorization": "Bearer SECRETTOKEN"},
            },
        )
    assert response.status_code == 200
    text = caplog.text
    assert "SUPERSECRETCOOKIE" not in text
    assert "SECRETTOKEN" not in text
    assert "Bearer " not in text
    assert "cache=miss" in text or "cache=hit" in text
