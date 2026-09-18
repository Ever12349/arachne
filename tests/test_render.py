"""Optional Playwright render: 501 when missing, mocked success/failure, cache key, SSRF."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import cache_key
from app.errors import ArachneError, render_failed
from app.fetch import FetchedPage, ssrf_request_hook
from app.main import app
from tests.conftest import SAMPLE_HTML, set_high_qps


def _html_page(url: str = "https://example.com/") -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="text/html; charset=utf-8",
        body=SAMPLE_HTML,
    )


def _client_sample(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    return TestClient(app)


def test_render_unavailable_when_playwright_missing(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    with _client_sample(monkeypatch) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "render": True},
        )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "render_unavailable"


def test_render_true_uses_playwright_path(public_dns, monkeypatch: pytest.MonkeyPatch):
    calls = {"n": 0}

    async def fake_render(url: str, **_kwargs) -> FetchedPage:
        calls["n"] += 1
        return _html_page(url)

    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.service.render_url", fake_render)
    with _client_sample(monkeypatch) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/article", "render": True},
        )
    assert response.status_code == 200
    assert response.json()["title"]
    assert calls["n"] == 1


def test_render_failed_is_502(public_dns, monkeypatch: pytest.MonkeyPatch):
    async def boom(url: str, **_kwargs) -> FetchedPage:
        raise render_failed()

    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.service.render_url", boom)
    with _client_sample(monkeypatch) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "https://example.com/", "render": True},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "render_failed"


def test_render_private_url_is_bad_url_even_without_playwright(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    with _client_sample(monkeypatch) as client:
        set_high_qps(client)
        response = client.post(
            "/extract",
            json={"url": "http://127.0.0.1/", "render": True},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_get_extract_has_no_render(api_client: TestClient):
    response = api_client.get("/extract", params={"url": "https://example.com/", "render": True})
    assert response.status_code == 200


def test_cache_key_includes_render_and_ua_strategy():
    base = cache_key("https://example.com/a", {}, {})
    rendered = cache_key("https://example.com/a", {}, {}, render=True)
    rotated = cache_key("https://example.com/a", {}, {}, ua_strategy="rotate")
    assert base != rendered
    assert base != rotated
    assert rendered != rotated
    assert cache_key("https://example.com/a", {}, {}, render=False, ua_strategy="default") == base


def test_render_flag_busts_cache(public_dns, monkeypatch: pytest.MonkeyPatch):
    http_counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        http_counts["n"] += 1
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    def fake_create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    render_counts = {"n": 0}

    async def fake_render(url: str, **_kwargs) -> FetchedPage:
        render_counts["n"] += 1
        return _html_page(url)

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.service.render_url", fake_render)
    with TestClient(app) as client:
        set_high_qps(client)
        r1 = client.post("/extract", json={"url": "https://example.com/article", "render": False})
        r2 = client.post("/extract", json={"url": "https://example.com/article", "render": True})
        r3 = client.post("/extract", json={"url": "https://example.com/article", "render": True})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert http_counts["n"] == 1
    assert render_counts["n"] == 1


def test_ua_strategy_busts_cache(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        return httpx.Response(
            200,
            content=SAMPLE_HTML.encode("utf-8"),
            headers={"content-type": "text/html"},
            request=request,
        )

    def fake_create() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        set_high_qps(client)
        r1 = client.post("/extract", json={"url": "https://example.com/article", "ua_strategy": "default"})
        r2 = client.post("/extract", json={"url": "https://example.com/article", "ua_strategy": "rotate"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert counts["n"] == 2


@pytest.mark.asyncio
async def test_render_url_raises_unavailable(monkeypatch: pytest.MonkeyPatch, public_dns):
    from app.render import render_url

    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    with pytest.raises(ArachneError) as exc:
        await render_url("https://example.com/")
    assert exc.value.code == "render_unavailable"


@pytest.mark.asyncio
async def test_render_uses_same_semaphore(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from app.cache import ResultCache
    from app.limits import FixedWindowRateLimiter, Stats
    from app.service import run_extract

    async def fake_render(url: str, **_kwargs) -> FetchedPage:
        return _html_page(url)

    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    held = asyncio.Semaphore(0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_extract(
                url="https://example.com/",
                client=None,  # type: ignore[arg-type]
                render=True,
                cache=ResultCache(),
                limiter=FixedWindowRateLimiter(qps=100),
                semaphore=held,
                stats=Stats(),
            ),
            timeout=0.1,
        )
