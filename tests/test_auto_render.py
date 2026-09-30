"""Auto-render: one Playwright fallback after challenge_detected, shared by extract and suggest."""

from __future__ import annotations

import asyncio

import pytest

from app.cache import ResultCache, cache_key
from app.errors import ArachneError, render_unavailable
from app.fetch import FetchedPage
from app.render import RenderedPage
from app.limits import FixedWindowRateLimiter, Stats
from app.metrics import AUTO_RENDERS_TOTAL
from app.profiles.models import SuggestEvidence, SuggestResponse
from app.profiles.schema import SiteProfile
from app.service import run_extract, run_suggest
from tests.conftest import CHALLENGE_CF_HTML, SAMPLE_HTML


def _page(
    body: str,
    url: str = "https://example.com/article",
    *,
    status_code: int = 200,
) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=status_code,
        content_type="text/html; charset=utf-8",
        body=body,
    )


def _rendered(page: FetchedPage, cookies: dict[str, str] | None = None) -> RenderedPage:
    return RenderedPage(page=page, cookies=dict(cookies or {}))


def _suggest() -> SuggestResponse:
    return SuggestResponse(
        profile=SiteProfile(id="example-com", version="1", hosts=["example.com"]),
        evidence=SuggestEvidence(
            strategy_used="heuristic",
            title_preview="Sample",
            main_preview="text",
        ),
    )


class _Limiter(FixedWindowRateLimiter):
    def __init__(self) -> None:
        super().__init__(qps=10, mode="reject", wait_seconds=0)
        self.calls = 0

    async def acquire(self) -> float:
        self.calls += 1
        return await super().acquire()


def _sample_total() -> float:
    for family in AUTO_RENDERS_TOTAL.collect():
        for sample in family.samples:
            if sample.name == "arachne_auto_renders_total":
                return float(sample.value)
    raise AssertionError("arachne_auto_renders_total")


async def test_auto_render_success_caches_render_bit_and_one_qps_slot(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    calls = {"fetch": 0, "render": 0}
    sem = asyncio.Semaphore(1)

    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        assert sem.locked()
        calls["fetch"] += 1
        return _page(CHALLENGE_CF_HTML)

    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        assert sem.locked()
        calls["render"] += 1
        return _rendered(_page(SAMPLE_HTML, url))

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)

    cache = ResultCache()
    limiter = _Limiter()
    stats = Stats()
    before = _sample_total()
    url = "https://example.com/article"
    with caplog.at_level("INFO", logger="arachne"):
        result = await run_extract(
            url=url,
            client=None,  # type: ignore[arg-type]
            render=False,
            cache=cache,
            limiter=limiter,
            semaphore=sem,
            stats=stats,
        )
    assert result.title
    assert calls == {"fetch": 1, "render": 1}
    assert limiter.calls == 1
    assert stats.auto_renders == 1
    assert _sample_total() == before + 1
    assert await cache.get(cache_key(url, {}, {})) is None
    assert await cache.get(cache_key(url, {}, {}, render=True)) is not None
    assert "auto_render=1" in caplog.text
    assert "qps_wait_ms=" in caplog.text
    assert "render_used" not in result.model_dump()

    await run_extract(
        url=url,
        client=None,  # type: ignore[arg-type]
        render=True,
        cache=cache,
        limiter=limiter,
        semaphore=sem,
        stats=stats,
    )
    assert calls["render"] == 1
    assert limiter.calls == 2


async def test_auto_render_skipped_without_playwright(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        return _page(CHALLENGE_CF_HTML)

    async def fake_render(*_args, **_kwargs) -> RenderedPage:
        raise AssertionError("render must not run")

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)
    stats = Stats()
    with pytest.raises(ArachneError) as exc:
        await run_extract(
            url="https://example.com/article",
            client=None,  # type: ignore[arg-type]
            render=False,
            cache=ResultCache(),
            limiter=_Limiter(),
            semaphore=asyncio.Semaphore(1),
            stats=stats,
        )
    assert exc.value.code == "challenge_detected"
    assert stats.auto_renders == 0
    assert stats.errors_by_code["challenge_detected"] == 1


async def test_auto_render_unavailable_keeps_original_challenge(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        return _page(CHALLENGE_CF_HTML)

    async def fake_render(*_args, **_kwargs) -> RenderedPage:
        raise render_unavailable()

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    with pytest.raises(ArachneError) as exc:
        await run_extract(
            url="https://example.com/article",
            client=None,  # type: ignore[arg-type]
            render=False,
            cache=ResultCache(),
            limiter=_Limiter(),
            semaphore=asyncio.Semaphore(1),
            stats=Stats(),
        )
    assert exc.value.code == "challenge_detected"


async def test_explicit_render_does_not_fetch_first(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        raise AssertionError("explicit render must not use httpx first")

    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        return _rendered(_page(SAMPLE_HTML, url))

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    stats = Stats()
    result = await run_extract(
        url="https://example.com/article",
        client=None,  # type: ignore[arg-type]
        render=True,
        cache=ResultCache(),
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=stats,
    )
    assert result.title
    assert stats.auto_renders == 0


async def test_explicit_render_cache_bit_stays_one(monkeypatch: pytest.MonkeyPatch):
    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        return _rendered(_page(SAMPLE_HTML, url))

    monkeypatch.setattr("app.service.render_url", fake_render)
    cache = ResultCache()
    await run_extract(
        url="https://example.com/article",
        client=None,  # type: ignore[arg-type]
        render=True,
        cache=cache,
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=Stats(),
    )
    url = "https://example.com/article"
    assert await cache.get(cache_key(url, {}, {}, render=True)) is not None
    assert await cache.get(cache_key(url, {}, {})) is None


async def test_suggest_auto_renders_on_challenge(monkeypatch: pytest.MonkeyPatch):
    calls = {"fetch": 0, "render": 0}
    limiter = _Limiter()

    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        calls["fetch"] += 1
        return _page(CHALLENGE_CF_HTML)

    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        calls["render"] += 1
        return _rendered(_page(SAMPLE_HTML, url))

    async def fake_suggest(_html: str, *, url: str, strategy: str) -> SuggestResponse:
        assert url == "https://example.com/article"
        assert strategy == "heuristic"
        return _suggest()

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.service.suggest_from_html", fake_suggest)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)
    stats = Stats()
    result = await run_suggest(
        url="https://example.com/article",
        client=None,  # type: ignore[arg-type]
        render=False,
        limiter=limiter,
        semaphore=asyncio.Semaphore(1),
        stats=stats,
    )
    assert result.profile.id == "example-com"
    assert calls == {"fetch": 1, "render": 1}
    assert limiter.calls == 1
    assert stats.auto_renders == 1


async def test_suggest_explicit_render_skips_httpx(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        raise AssertionError("explicit render must not use httpx first")

    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        return _rendered(_page(SAMPLE_HTML, url))

    async def fake_suggest(_html: str, *, url: str, strategy: str) -> SuggestResponse:
        return _suggest()

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.service.suggest_from_html", fake_suggest)
    stats = Stats()
    result = await run_suggest(
        url="https://example.com/article",
        client=None,  # type: ignore[arg-type]
        render=True,
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=stats,
    )
    assert result.profile.id == "example-com"
    assert stats.auto_renders == 0


def _both_codes() -> frozenset[str]:
    return frozenset({"challenge_detected", "unauthorized_upstream"})


async def _extract(monkeypatch: pytest.MonkeyPatch, stats: Stats, **kwargs: object):
    return await run_extract(
        url="https://example.com/article",
        client=None,  # type: ignore[arg-type]
        cache=ResultCache(),
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=stats,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_unauthorized_auto_renders_when_enabled(monkeypatch: pytest.MonkeyPatch):
    calls = {"fetch": 0, "render": 0}

    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        calls["fetch"] += 1
        return _page(SAMPLE_HTML, status_code=401)

    async def fake_render(url: str, **_kwargs) -> RenderedPage:
        calls["render"] += 1
        return _rendered(_page(SAMPLE_HTML, url))

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)
    monkeypatch.setattr("app.config.AUTO_RENDER_ON", _both_codes())
    stats = Stats()
    result = await _extract(monkeypatch, stats, render=False)
    assert result.title
    assert calls == {"fetch": 1, "render": 1}
    assert stats.auto_renders == 1


async def test_unauthorized_not_rendered_when_only_challenge(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        return _page(SAMPLE_HTML, status_code=403)

    async def fake_render(*_args, **_kwargs) -> RenderedPage:
        raise AssertionError("render must not run")

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)
    monkeypatch.setattr("app.config.AUTO_RENDER_ON", frozenset({"challenge_detected"}))
    stats = Stats()
    with pytest.raises(ArachneError) as exc:
        await _extract(monkeypatch, stats, render=False)
    assert exc.value.code == "unauthorized_upstream"
    assert stats.auto_renders == 0


async def test_auto_render_off_keeps_challenge(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        return _page(CHALLENGE_CF_HTML)

    async def fake_render(*_args, **_kwargs) -> RenderedPage:
        raise AssertionError("render must not run")

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    monkeypatch.setattr("app.config.AUTO_RENDER", False)
    monkeypatch.setattr("app.config.AUTO_RENDER_ON", _both_codes())
    stats = Stats()
    with pytest.raises(ArachneError) as exc:
        await _extract(monkeypatch, stats, render=False)
    assert exc.value.code == "challenge_detected"
    assert stats.auto_renders == 0


async def test_unauthorized_without_playwright_keeps_original(monkeypatch: pytest.MonkeyPatch):
    async def fake_fetch(*_args, **_kwargs) -> FetchedPage:
        return _page("<html><body>login</body></html>", status_code=401)

    async def fake_render(*_args, **_kwargs) -> RenderedPage:
        raise AssertionError("render must not run")

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: False)
    monkeypatch.setattr("app.config.AUTO_RENDER", True)
    monkeypatch.setattr("app.config.AUTO_RENDER_ON", _both_codes())
    stats = Stats()
    with pytest.raises(ArachneError) as exc:
        await _extract(monkeypatch, stats, render=False)
    assert exc.value.code == "unauthorized_upstream"
    assert exc.value.code != "render_unavailable"
    assert stats.auto_renders == 0


def test_auto_render_on_csv(monkeypatch: pytest.MonkeyPatch):
    from app.config import auto_render_on_codes

    monkeypatch.delenv("ARACHNE_AUTO_RENDER_ON", raising=False)
    assert auto_render_on_codes() == _both_codes()

    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "")
    assert auto_render_on_codes() == frozenset()

    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "   ")
    assert auto_render_on_codes() == frozenset()

    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "challenge,nope,UNAUTHORIZED")
    assert auto_render_on_codes() == _both_codes()

    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "challenge")
    assert auto_render_on_codes() == frozenset({"challenge_detected"})

    monkeypatch.setenv("ARACHNE_AUTO_RENDER_ON", "challenge_detected")
    assert auto_render_on_codes() == frozenset()
