"""Retries, UA rotation, and high-precision challenge detection."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.antibot import (
    UA_POOL,
    backoff_delay,
    headers_for_upstream,
    is_retryable,
    looks_like_challenge,
)
from app.errors import ArachneError, fetch_failed, timeout_error
from app.fetch import FetchedPage, ssrf_request_hook
from app.main import app
from tests.conftest import (
    CHALLENGE_ATTENTION_HTML,
    CHALLENGE_CF_HTML,
    SAMPLE_HTML,
    set_high_qps,
)


def test_challenge_markers_match_fixtures():
    assert looks_like_challenge(CHALLENGE_CF_HTML) is True
    assert looks_like_challenge(CHALLENGE_ATTENTION_HTML) is True
    assert looks_like_challenge(SAMPLE_HTML) is False
    assert looks_like_challenge("") is False
    assert looks_like_challenge("Please wait while the article loads.") is False


def test_retryable_only_timeout_and_connect():
    assert is_retryable(timeout_error()) is True
    assert is_retryable(fetch_failed("x", {"kind": "connect"})) is True
    assert is_retryable(fetch_failed("x")) is False
    assert is_retryable(ArachneError("challenge_detected", "nope")) is False
    assert is_retryable(ArachneError("too_large", "nope")) is False


def test_backoff_uses_configured_delays(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.RETRY_BACKOFF_SECONDS", [0.5, 1.0])
    assert backoff_delay(0) == 0.5
    assert backoff_delay(1) == 1.0
    assert backoff_delay(2) == 1.0


def test_explicit_ua_wins_over_rotate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.antibot.UA_POOL", ("RotateAgent/1.0",))
    headers = headers_for_upstream({"User-Agent": "Caller/9"}, "rotate")
    assert headers["User-Agent"] == "Caller/9"


def test_rotate_picks_from_pool_when_no_ua(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.antibot.UA_POOL", ("RotateAgent/1.0",))
    headers = headers_for_upstream({}, "rotate")
    assert headers["User-Agent"] == "RotateAgent/1.0"


def test_default_strategy_uses_configured_ua():
    from app.config import USER_AGENT

    headers = headers_for_upstream({}, "default")
    assert headers["User-Agent"] == USER_AGENT


def _client_with_handler(monkeypatch: pytest.MonkeyPatch, handler):
    def fake_create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    return TestClient(app)


def test_timeout_is_retried_then_succeeds(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        if counts["n"] < 3:
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("app.antibot.asyncio.sleep", record)
    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 200
    assert counts["n"] == 3
    assert sleeps == [0.5, 1.0]


def test_connect_error_is_retried(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "fetch_failed"
    assert counts["n"] == 3


def test_http_404_is_not_retried(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(
            404,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/missing"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "fetch_failed"
    assert counts["n"] == 1


def test_unauthorized_is_not_retried(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(
            403,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "unauthorized_upstream"
    assert counts["n"] == 1


def test_challenge_200_html_is_403(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(
            200,
            content=CHALLENGE_CF_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "challenge_detected"
    assert response.json()["error"]["detail"]["status_code"] == 200
    assert counts["n"] == 1


def test_challenge_403_is_challenge_detected_not_unauthorized(public_dns, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=CHALLENGE_ATTENTION_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "challenge_detected"
    assert response.json()["error"]["detail"]["status_code"] == 403


def test_401_without_challenge_stays_unauthorized(public_dns, monkeypatch: pytest.MonkeyPatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "unauthorized_upstream"


def test_rotate_sends_pool_ua(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.antibot.UA_POOL", ("RotateAgent/1.0",))
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["user-agent"] = request.headers.get("user-agent")
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "ua_strategy": "rotate"},
        )
    assert response.status_code == 200
    assert captured["user-agent"] == "RotateAgent/1.0"
    assert captured["user-agent"] in UA_POOL or captured["user-agent"] == "RotateAgent/1.0"


def test_caller_ua_wins_over_rotate_on_post(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.antibot.UA_POOL", ("RotateAgent/1.0",))
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["user-agent"] = request.headers.get("user-agent")
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    with _client_with_handler(monkeypatch, handler) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={
                "url": "https://example.com/",
                "ua_strategy": "rotate",
                "headers": {"User-Agent": "CallerAgent/9"},
            },
        )
    assert response.status_code == 200
    assert captured["user-agent"] == "CallerAgent/9"


def test_invalid_ua_strategy_is_bad_url(api_client: TestClient):
    response = api_client.post(
        "/extract",
        json={"url": "https://example.com/", "ua_strategy": "stealth"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


@pytest.mark.asyncio
async def test_raise_if_challenge_skips_non_html_2xx():
    from app.antibot import raise_if_challenge

    page = FetchedPage(
        requested_url="https://example.com/a.png",
        final_url="https://example.com/a.png",
        status_code=200,
        content_type="image/png",
        body="just a moment cf-challenge",
    )
    raise_if_challenge(page)
