"""QPS queue: wait for the next 1s window, time out, or reject immediately."""

from __future__ import annotations

import asyncio

import pytest

from app.cache import ResultCache
from app.errors import ArachneError
from app.limits import FixedWindowRateLimiter, Stats, normalize_qps_mode
from app.metrics import QPS_WAIT_SECONDS, QPS_WAITS_TOTAL
from app.models import ExtractResponse, PageMetadata
from app.service import run_extract
from tests.conftest import SAMPLE_HTML


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _limiter(
    *,
    qps: int = 1,
    mode: str = "queue",
    wait_seconds: float = 30.0,
    clock: _Clock | None = None,
    sleep,
) -> FixedWindowRateLimiter:
    return FixedWindowRateLimiter(
        qps=qps,
        window_seconds=1.0,
        clock=clock or _Clock(),
        mode=mode,
        wait_seconds=wait_seconds,
        sleep=sleep,
    )


def _sample(metric, name: str) -> float:
    for family in metric.collect():
        for sample in family.samples:
            if sample.name == name and not sample.labels:
                return float(sample.value)
    raise AssertionError(name)


async def test_queue_sleeps_to_window_boundary_then_acquires():
    clock = _Clock()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.value += seconds

    limiter = _limiter(clock=clock, sleep=sleep)
    assert await limiter.acquire() == 0
    waited = await limiter.acquire()
    assert slept == [pytest.approx(1.0)]
    assert waited == pytest.approx(1000.0)


async def test_queue_timeout_does_not_sleep_past_budget():
    clock = _Clock()

    async def sleep(seconds: float) -> None:
        raise AssertionError(f"slept {seconds}")

    limiter = _limiter(clock=clock, wait_seconds=0.4, sleep=sleep)
    assert await limiter.acquire() == 0
    with pytest.raises(ArachneError) as exc:
        await limiter.acquire()
    assert exc.value.code == "rate_limited"
    assert getattr(exc.value, "wait_ms") == 0
    assert exc.value.detail == {}
    assert clock.value == 0


async def test_queue_timeout_keeps_partial_wait():
    clock = _Clock()
    steals = {"n": 0}

    limiter_box: dict[str, FixedWindowRateLimiter] = {}

    async def sleep(seconds: float) -> None:
        clock.value += seconds
        if steals["n"] < 1:
            steals["n"] += 1
            assert await limiter_box["limiter"].try_acquire() is True

    limiter = _limiter(clock=clock, wait_seconds=1.2, sleep=sleep)
    limiter_box["limiter"] = limiter
    assert await limiter.acquire() == 0
    with pytest.raises(ArachneError) as exc:
        await limiter.acquire()
    assert exc.value.code == "rate_limited"
    assert getattr(exc.value, "wait_ms") == pytest.approx(1000.0)
    assert steals["n"] == 1


async def test_reject_does_not_wait():
    clock = _Clock()

    async def sleep(seconds: float) -> None:
        raise AssertionError("reject must not sleep")

    limiter = _limiter(mode="reject", wait_seconds=30, clock=clock, sleep=sleep)
    assert limiter.mode == "reject"
    assert await limiter.acquire() == 0
    with pytest.raises(ArachneError) as exc:
        await limiter.acquire()
    assert exc.value.code == "rate_limited"
    assert clock.value == 0


@pytest.mark.parametrize("mode", ["nope", "", "QUEUE", "Queue"])
def test_invalid_mode_is_queue(mode: str):
    assert normalize_qps_mode(mode) == "queue"


@pytest.mark.parametrize("wait_seconds", [0, -1])
async def test_non_positive_wait_is_immediate(wait_seconds: float):
    async def sleep(seconds: float) -> None:
        raise AssertionError("wait<=0 must not sleep")

    limiter = _limiter(mode="queue", wait_seconds=wait_seconds, sleep=sleep)
    assert await limiter.acquire() == 0
    with pytest.raises(ArachneError) as exc:
        await limiter.acquire()
    assert exc.value.code == "rate_limited"


def _dummy_response() -> ExtractResponse:
    return ExtractResponse(
        url="https://example.com/",
        requested_url="https://example.com/",
        status_code=200,
        title="T",
        main_text="hello",
        metadata=PageMetadata(),
        links=[],
        truncated=False,
    )


async def test_run_extract_records_qps_wait(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()

    async def sleep(seconds: float) -> None:
        clock.value += seconds

    limiter = _limiter(clock=clock, wait_seconds=5, sleep=sleep)
    stats = Stats()

    async def fake_fetch(*_args, **_kwargs):
        return _page(), 0, None

    monkeypatch.setattr("app.service.fetch_ready_page", fake_fetch)
    kwargs = dict(
        url="https://example.com/",
        client=None,  # type: ignore[arg-type]
        cache=ResultCache(),
        limiter=limiter,
        semaphore=asyncio.Semaphore(2),
        stats=stats,
    )
    await run_extract(**kwargs)
    await run_extract(**kwargs)
    assert stats.qps_waits == 1
    assert stats.qps_wait_ms_sum == pytest.approx(1000.0)
    assert stats.snapshot()["qps_waits"] == 1


def _page():
    from app.fetch import FetchedPage

    return FetchedPage(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        content_type="text/html",
        body=SAMPLE_HTML,
    )


async def test_rate_limited_log_includes_wait_ms(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    clock = _Clock()
    steals = {"n": 0}
    box: dict[str, FixedWindowRateLimiter] = {}

    async def sleep(seconds: float) -> None:
        clock.value += seconds
        if steals["n"] < 1:
            steals["n"] += 1
            assert await box["limiter"].try_acquire() is True

    limiter = _limiter(clock=clock, wait_seconds=1.2, sleep=sleep)
    box["limiter"] = limiter

    async def fake_fetch(*_args, **_kwargs):
        return _page(), 0, None

    monkeypatch.setattr("app.service.fetch_ready_page", fake_fetch)
    stats = Stats()
    kwargs = dict(
        url="https://example.com/",
        client=None,  # type: ignore[arg-type]
        cache=ResultCache(),
        limiter=limiter,
        semaphore=asyncio.Semaphore(2),
        stats=stats,
    )
    with caplog.at_level("INFO", logger="arachne"):
        await run_extract(**kwargs)
        with pytest.raises(ArachneError) as exc:
            await run_extract(**kwargs)
    assert exc.value.code == "rate_limited"
    assert stats.qps_waits == 1
    assert stats.qps_wait_ms_sum == pytest.approx(1000.0)
    assert "qps_wait_ms=1000.00" in caplog.text
    assert stats.errors_by_code["rate_limited"] == 1


def test_qps_wait_dual_writes_prometheus():
    before_count = _sample(QPS_WAITS_TOTAL, "arachne_qps_waits_total")
    before_sum = _sample(QPS_WAIT_SECONDS, "arachne_qps_wait_seconds_sum")
    stats = Stats()
    stats.record_qps_wait(0)
    stats.record_qps_wait(1500)
    assert stats.qps_waits == 1
    assert stats.qps_wait_ms_sum == pytest.approx(1500.0)
    assert _sample(QPS_WAITS_TOTAL, "arachne_qps_waits_total") == before_count + 1
    assert _sample(QPS_WAIT_SECONDS, "arachne_qps_wait_seconds_sum") == pytest.approx(before_sum + 1.5)
