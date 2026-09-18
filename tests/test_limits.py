"""Rate limit, concurrency exemption, and /stats counters."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.cache import ResultCache, cache_key
from app.errors import ArachneError
from app.limits import FixedWindowRateLimiter, Stats
from app.models import ExtractResponse, PageMetadata
from app.service import run_extract


def test_sixth_extract_is_rate_limited(api_client: TestClient):
    statuses = []
    last = None
    for _ in range(6):
        last = api_client.post("/extract", json={"url": "https://example.com/"})
        statuses.append(last.status_code)
    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert statuses[5] == 429
    assert last is not None
    body = last.json()
    assert body["error"]["code"] == "rate_limited"
    assert "message" in body["error"]


def test_health_and_stats_exempt_from_rate_limit(api_client: TestClient):
    for _ in range(5):
        assert api_client.post("/extract", json={"url": "https://example.com/"}).status_code == 200
    assert api_client.get("/health").status_code == 200
    stats = api_client.get("/stats")
    assert stats.status_code == 200
    payload = stats.json()
    assert payload["requests_total"] == 5
    sixth = api_client.post("/extract", json={"url": "https://example.com/"})
    assert sixth.status_code == 429
    after = api_client.get("/stats").json()
    assert after["requests_total"] == 6
    assert after["errors_by_code"]["rate_limited"] == 1
    assert after["in_flight"] == 0
    assert after["latency_ms_count"] == 6
    assert after["latency_ms_sum"] >= 0
    # health/stats must not increment extract counters
    assert api_client.get("/health").status_code == 200
    assert api_client.get("/stats").json()["requests_total"] == 6


def test_stats_counts_cache_hit_and_miss(api_client: TestClient):
    url = "https://www.example.com/article"
    first = api_client.get("/extract", params={"url": url})
    second = api_client.get("/extract", params={"url": url})
    assert first.status_code == 200
    assert second.status_code == 200
    stats = api_client.get("/stats").json()
    assert stats["requests_total"] == 2
    assert stats["cache_misses"] == 1
    assert stats["cache_hits"] == 1
    assert stats["errors_by_code"] == {}


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_fixed_window_resets_after_one_second():
    clock = _Clock()
    limiter = FixedWindowRateLimiter(qps=2, window_seconds=1.0, clock=clock)
    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is True
    assert await limiter.try_acquire() is False
    clock.value += 0.99
    assert await limiter.try_acquire() is False
    clock.value += 0.02
    assert await limiter.try_acquire() is True


def _dummy_response(text: str = "hello") -> ExtractResponse:
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
async def test_cache_hit_skips_semaphore():
    cache = ResultCache()
    key = cache_key("https://example.com/", {}, {})
    await cache.set(key, _dummy_response("cached-text"))
    held = asyncio.Semaphore(0)
    result = await asyncio.wait_for(
        run_extract(
            url="https://example.com/",
            client=None,  # type: ignore[arg-type]
            cache=cache,
            limiter=FixedWindowRateLimiter(qps=100),
            semaphore=held,
            stats=Stats(),
        ),
        timeout=0.5,
    )
    assert result.main_text == "cached-text"


@pytest.mark.asyncio
async def test_cache_miss_waits_on_semaphore():
    cache = ResultCache()
    held = asyncio.Semaphore(0)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            run_extract(
                url="https://example.com/",
                client=None,  # type: ignore[arg-type]
                cache=cache,
                limiter=FixedWindowRateLimiter(qps=100),
                semaphore=held,
                stats=Stats(),
            ),
            timeout=0.1,
        )


@pytest.mark.asyncio
async def test_errors_are_counted(monkeypatch: pytest.MonkeyPatch):
    stats = Stats()
    cache = ResultCache()
    limiter = FixedWindowRateLimiter(qps=100)
    sem = asyncio.Semaphore(10)

    async def boom(*_args, **_kwargs):
        raise ArachneError("fetch_failed", "nope")

    monkeypatch.setattr("app.service.extract_page", boom)
    with pytest.raises(ArachneError):
        await run_extract(
            url="https://example.com/",
            client=None,  # type: ignore[arg-type]
            cache=cache,
            limiter=limiter,
            semaphore=sem,
            stats=stats,
        )
    assert stats.errors_by_code["fetch_failed"] == 1
    assert stats.cache_misses == 1
    assert len(cache) == 0
