"""Cache key normalization, TTL, success-only storage, and hit counting."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import ResultCache, cache_key, normalize_cache_url, session_fingerprint
from app.extract import apply_max_chars
from app.fetch import ssrf_request_hook
from app.main import app
from app.models import ExtractResponse, PageMetadata
from tests.conftest import SAMPLE_HTML, set_high_qps


def test_normalize_cache_url_lowers_and_drops_default_port_and_fragment():
    assert normalize_cache_url("HTTPS://Example.COM:443/Path?b=1&a=2#frag") == (
        "https://example.com/Path?b=1&a=2"
    )
    assert normalize_cache_url("http://EXAMPLE.com:80/") == "http://example.com/"
    assert normalize_cache_url("https://example.com:8443/a") == "https://example.com:8443/a"


def test_normalize_keeps_query_order():
    left = normalize_cache_url("https://example.com/p?b=1&a=2")
    right = normalize_cache_url("https://example.com/p?a=2&b=1")
    assert left != right


def test_session_fingerprint_is_stable_and_order_independent():
    a = session_fingerprint({"Authorization": "Bearer x", "Accept": "text/html"}, {"b": "2", "a": "1"})
    b = session_fingerprint({"Accept": "text/html", "Authorization": "Bearer x"}, {"a": "1", "b": "2"})
    assert a == b
    assert len(a) == 64
    empty = session_fingerprint({}, {})
    assert empty != a


def test_cache_key_includes_url_and_session():
    k1 = cache_key("https://EXAMPLE.com:443/a#x", {}, {})
    k2 = cache_key("https://example.com/a", {}, {})
    assert k1 == k2
    k3 = cache_key("https://example.com/a", {}, {"sid": "1"})
    assert k1 != k3


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _dummy(text: str = "body") -> ExtractResponse:
    return ExtractResponse(
        url="https://example.com/",
        requested_url="https://example.com/",
        status_code=200,
        title="T",
        main_text=text,
        metadata=PageMetadata(),
        links=[],
        truncated=False,
    )


@pytest.mark.asyncio
async def test_ttl_expiry_and_maxsize():
    clock = _Clock()
    cache = ResultCache(maxsize=1, ttl=60, timer=clock)
    await cache.set(("u1", "s"), _dummy("one"))
    assert (await cache.get(("u1", "s"))) is not None
    await cache.set(("u2", "s"), _dummy("two"))
    assert (await cache.get(("u1", "s"))) is None
    assert (await cache.get(("u2", "s"))).main_text == "two"  # type: ignore[union-attr]
    clock.value += 61
    assert (await cache.get(("u2", "s"))) is None


@pytest.mark.asyncio
async def test_cached_copy_is_not_mutated_by_truncation():
    cache = ResultCache()
    original = _dummy("abcdefghij")
    await cache.set(("u", "s"), original)
    stored = await cache.get(("u", "s"))
    assert stored is not None
    truncated = apply_max_chars(stored, 3)
    assert truncated.main_text == "abc"
    again = await cache.get(("u", "s"))
    assert again is not None
    assert again.main_text == "abcdefghij"


def _client_with_counter(public_dns, monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            counts["n"] = counts.get("n", 0) + 1
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


def test_success_is_cached_normalized_url(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {"n": 0}
    with _client_with_counter(public_dns, monkeypatch, counts) as client:
        set_high_qps(client)
        r1 = client.get("/extract", params={"url": "https://EXAMPLE.com:443/article#top"})
        r2 = client.get("/extract", params={"url": "https://example.com/article"})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert counts["n"] == 1
        stats = client.get("/stats").json()
        assert stats["cache_hits"] == 1
        assert stats["cache_misses"] == 1
        assert "cached" not in r2.json()


def test_query_order_is_part_of_cache_key(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {"n": 0}
    with _client_with_counter(public_dns, monkeypatch, counts) as client:
        set_high_qps(client)
        client.get("/extract", params={"url": "https://example.com/p?b=1&a=2"})
        client.get("/extract", params={"url": "https://example.com/p?a=2&b=1"})
        assert counts["n"] == 2


def test_session_changes_cache_key(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {"n": 0}
    with _client_with_counter(public_dns, monkeypatch, counts) as client:
        set_high_qps(client)
        url = "https://example.com/article"
        client.post("/extract", json={"url": url})
        client.post("/extract", json={"url": url, "cookies": {"sid": "abc"}})
        assert counts["n"] == 2


def test_errors_are_not_cached(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {"n": 0}

    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            counts["n"] += 1
            return httpx.Response(
                404,
                content=SAMPLE_HTML.encode("utf-8"),
                headers={"content-type": "text/html"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        set_high_qps(client)
        r1 = client.post("/extract", json={"url": "https://example.com/missing"})
        r2 = client.post("/extract", json={"url": "https://example.com/missing"})
    assert r1.status_code == 502
    assert r2.status_code == 502
    assert counts["n"] == 2


def test_max_chars_does_not_bust_cache(public_dns, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {"n": 0}
    with _client_with_counter(public_dns, monkeypatch, counts) as client:
        set_high_qps(client)
        url = "https://example.com/article"
        r1 = client.get("/extract", params={"url": url, "max_chars": 20})
        r2 = client.get("/extract", params={"url": url, "max_chars": 8})
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert counts["n"] == 1
        assert r2.json()["truncated"] is True
        assert len(r2.json()["main_text"]) <= 8
