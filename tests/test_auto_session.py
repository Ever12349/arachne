"""Auto session ids, load/merge, and persist after an accepted browser page."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.auto_session import auto_session_id
from app.cache import ResultCache
from app.errors import ArachneError, render_failed
from app.fetch import FetchedPage
from app.limits import FixedWindowRateLimiter, Stats
from app.profiles.models import SuggestEvidence, SuggestResponse
from app.profiles.schema import SiteProfile
from app.profiles.suggest import profile_id_from_host
from app.render import RenderedPage, flatten_playwright_cookies
from app.service import run_extract, run_suggest
from app.sessions import SESSION_ID_RE, load_session, write_session_file
from app.ssrf import normalize_host
from tests.conftest import CHALLENGE_CF_HTML, EMPTY_HTML, SAMPLE_HTML

URL = "https://www.example.com/article"
BARE = "https://example.com/article"


def _page(body: str = SAMPLE_HTML, *, status: int = 200, url: str = URL) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type="text/html; charset=utf-8",
        body=body,
    )


def _rendered(
    body: str = SAMPLE_HTML,
    cookies: dict[str, str] | None = None,
    *,
    url: str = URL,
) -> RenderedPage:
    return RenderedPage(page=_page(body, url=url), cookies=dict(cookies or {}))


class _Limiter(FixedWindowRateLimiter):
    def __init__(self) -> None:
        super().__init__(qps=10, mode="reject", wait_seconds=0)

    async def acquire(self) -> float:
        return await super().acquire()


def _sessions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, key: str | None = None, persist: bool = True) -> str:
    raw = Fernet.generate_key().decode("ascii") if key is None else key
    monkeypatch.setattr("app.config.SESSION_KEY", raw)
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr("app.config.SESSION_PERSIST", persist)
    return raw


def _bind(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fetch: FetchedPage | None = None,
    rendered: RenderedPage | BaseException | None = None,
    seen: dict[str, dict[str, str]] | None = None,
) -> None:
    async def fake_fetch(*_args, **kwargs):
        if seen is not None:
            seen["fetch_cookies"] = dict(kwargs.get("cookies") or {})
            seen["fetch_headers"] = dict(kwargs.get("headers") or {})
        if fetch is None:
            raise AssertionError("fetch must not run")
        return fetch

    async def fake_render(*_args, **kwargs):
        if seen is not None:
            seen["render_cookies"] = dict(kwargs.get("cookies") or {})
        if rendered is None:
            raise AssertionError("render must not run")
        if isinstance(rendered, BaseException):
            raise rendered
        return rendered

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)


async def _extract(**kwargs):
    stats = kwargs.pop("stats", None) or Stats()
    cache = kwargs.pop("cache", None) or ResultCache()
    return await run_extract(
        url=kwargs.pop("url", URL),
        client=None,  # type: ignore[arg-type]
        cache=cache,
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=stats,
        **kwargs,
    )


def _suggest_body() -> SuggestResponse:
    return SuggestResponse(
        profile=SiteProfile(id="example-com", version="1", hosts=["example.com"]),
        evidence=SuggestEvidence(strategy_used="heuristic", title_preview="Sample", main_preview="text"),
    )


def test_auto_session_id_uses_profile_host_cleaning():
    assert auto_session_id("https://www.Example.com/a") == "auto-example-com"
    assert auto_session_id("https://example.com/b") == "auto-example-com"
    assert auto_session_id("https://news.example.co.uk/x") == "auto-news-example-co-uk"
    long_host = ("ab." * 40) + "example.com"
    cleaned = profile_id_from_host(normalize_host(long_host))
    sid = auto_session_id(f"https://{long_host}/path")
    assert sid == ("auto-" + cleaned)[:64].rstrip("-")
    assert SESSION_ID_RE.fullmatch(sid)
    assert not sid.endswith("-")
    assert len(sid) <= 64


def test_flatten_playwright_cookies_later_name_wins():
    raw = [
        {"name": "sid", "value": "first", "domain": "a.example"},
        {"name": "other", "value": "ok", "domain": ".example.com"},
        {"name": "sid", "value": "second", "domain": "b.example"},
        {"name": 1, "value": "no"},
        "bad",
    ]
    assert flatten_playwright_cookies(raw) == {"sid": "second", "other": "ok"}
    assert flatten_playwright_cookies(None) == {}


async def test_load_auto_when_no_session_id_body_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    write_session_file(
        auto_session_id(URL),
        {"sid": "from-auto", "keep": "auto"},
        {"Authorization": "Bearer from-file"},
    )
    seen: dict[str, dict[str, str]] = {}
    _bind(monkeypatch, fetch=_page(url=BARE), seen=seen)
    result = await _extract(
        url=BARE,
        cookies={"sid": "from-body"},
        headers={"Accept": "text/html"},
    )
    assert result.title
    assert seen["fetch_cookies"]["sid"] == "from-body"
    assert seen["fetch_cookies"]["keep"] == "auto"
    assert seen["fetch_headers"]["Authorization"] == "Bearer from-file"
    assert seen["fetch_headers"]["Accept"] == "text/html"


async def test_explicit_session_id_does_not_load_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"auto": "1"}, {})
    write_session_file("demo", {"hand": "1"}, {})
    seen: dict[str, dict[str, str]] = {}
    _bind(monkeypatch, fetch=_page(), seen=seen)
    await _extract(session_id="demo")
    assert seen["fetch_cookies"] == {"hand": "1"}


async def test_missing_explicit_session_does_not_fall_back_to_auto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"auto": "1"}, {})
    _bind(monkeypatch, fetch=_page())
    with pytest.raises(ArachneError) as exc:
        await _extract(session_id="missing")
    assert exc.value.code == "session_invalid"


async def test_explicit_session_does_not_persist_auto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"old": "1"}, {})
    write_session_file("demo", {"hand": "1"}, {})
    before = (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes()
    _bind(monkeypatch, rendered=_rendered(cookies={"fresh": "SECRETCOOKIE"}))
    with caplog.at_level("INFO", logger="arachne"):
        result = await _extract(render=True, session_id="demo")
    assert result.title
    assert (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes() == before
    assert "session_persist=0" in caplog.text
    assert "SECRETCOOKIE" not in caplog.text


async def test_missing_key_skips_persist_and_extract_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _sessions(monkeypatch, tmp_path, key="")
    _bind(monkeypatch, rendered=_rendered(cookies={"sid": "SECRETCOOKIE"}))
    with caplog.at_level("INFO", logger="arachne"):
        result = await _extract(render=True)
    assert result.title
    assert list(tmp_path.glob("*.bin")) == []
    assert "session_persist=skipped_no_key" in caplog.text
    assert "SECRETCOOKIE" not in caplog.text


async def test_invalid_key_skips_persist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _sessions(monkeypatch, tmp_path, key="not-a-valid-fernet-key")
    _bind(monkeypatch, rendered=_rendered(cookies={"sid": "SECRETCOOKIE"}))
    with caplog.at_level("INFO", logger="arachne"):
        result = await _extract(render=True)
    assert result.title
    assert list(tmp_path.glob("*.bin")) == []
    assert "session_persist=skipped_no_key" in caplog.text
    assert "SECRETCOOKIE" not in caplog.text


async def test_empty_browser_cookies_leave_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"old": "keep"}, {})
    before = (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes()
    _bind(monkeypatch, rendered=_rendered(cookies={}))
    with caplog.at_level("INFO", logger="arachne"):
        await _extract(render=True)
    assert (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes() == before
    assert "session_persist=skipped_empty" in caplog.text


async def test_corrupt_auto_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _sessions(monkeypatch, tmp_path)
    (tmp_path / f"{auto_session_id(URL)}.bin").write_bytes(b"not-fernet")
    seen: dict[str, dict[str, str]] = {}
    _bind(monkeypatch, fetch=_page(), seen=seen)
    with caplog.at_level("INFO", logger="arachne"):
        result = await _extract()
    assert result.title
    assert seen["fetch_cookies"] == {}
    assert "ignored_invalid" in caplog.text
    assert "session_invalid" not in caplog.text


async def test_suggest_reads_auto_and_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"old": "1"}, {})
    before = (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes()
    seen: dict[str, dict[str, str]] = {}
    _bind(monkeypatch, rendered=_rendered(cookies={"fresh": "nope"}), seen=seen)

    async def fake_suggest(_html: str, *, url: str, strategy: str) -> SuggestResponse:
        assert url == URL
        return _suggest_body()

    monkeypatch.setattr("app.service.suggest_from_html", fake_suggest)
    result = await run_suggest(
        url=URL,
        client=None,  # type: ignore[arg-type]
        render=True,
        limiter=_Limiter(),
        semaphore=asyncio.Semaphore(1),
        stats=Stats(),
    )
    assert result.profile.id == "example-com"
    assert seen["render_cookies"] == {"old": "1"}
    assert (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes() == before
    assert list(tmp_path.glob("*.bin")) == [tmp_path / f"{auto_session_id(URL)}.bin"]


async def test_extract_auto_render_writes_and_reuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    _sessions(monkeypatch, tmp_path)
    seen: dict[str, dict[str, str]] = {}
    calls = {"fetch": 0, "render": 0}

    async def fake_fetch(*_args, **kwargs):
        calls["fetch"] += 1
        if seen is not None:
            seen["fetch_cookies"] = dict(kwargs.get("cookies") or {})
        if calls["fetch"] == 1:
            return _page(CHALLENGE_CF_HTML)
        return _page(SAMPLE_HTML)

    async def fake_render(*_args, **_kwargs):
        calls["render"] += 1
        return _rendered(SAMPLE_HTML, {"sid": "EXTRACTCOOKIE"})

    monkeypatch.setattr("app.service.fetch_with_retries", fake_fetch)
    monkeypatch.setattr("app.service.render_url", fake_render)
    monkeypatch.setattr("app.render.playwright_available", lambda: True)
    stats = Stats()
    cache = ResultCache()
    with caplog.at_level("INFO", logger="arachne"):
        first = await _extract(stats=stats, cache=cache, cookies={"body": "1"})
    assert first.title
    assert calls == {"fetch": 1, "render": 1}
    assert stats.auto_renders == 1
    loaded = load_session(auto_session_id(URL))
    assert loaded.cookies == {"sid": "EXTRACTCOOKIE"}
    assert loaded.headers == {}
    assert "session_persist=1" in caplog.text
    assert "EXTRACTCOOKIE" not in caplog.text

    await _extract(stats=stats, cache=cache, render=False)
    assert calls == {"fetch": 2, "render": 1}
    assert seen["fetch_cookies"]["sid"] == "EXTRACTCOOKIE"


async def test_explicit_render_persists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _sessions(monkeypatch, tmp_path)
    _bind(monkeypatch, rendered=_rendered(cookies={"sid": "browser"}))
    stats = Stats()
    with caplog.at_level("INFO", logger="arachne"):
        result = await _extract(render=True, stats=stats)
    assert result.title
    assert stats.auto_renders == 0
    loaded = load_session(auto_session_id(URL))
    assert loaded.cookies == {"sid": "browser"}
    assert loaded.headers == {}
    assert "session_persist=1" in caplog.text


async def test_persist_happens_before_extract_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    _bind(monkeypatch, rendered=_rendered(EMPTY_HTML, {"sid": "kept"}))
    with pytest.raises(ArachneError) as exc:
        await _extract(render=True)
    assert exc.value.code == "extract_empty"
    loaded = load_session(auto_session_id(URL))
    assert loaded.cookies == {"sid": "kept"}
    assert loaded.headers == {}


async def test_session_persist_false_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _sessions(monkeypatch, tmp_path, persist=False)
    write_session_file(auto_session_id(URL), {"old": "1"}, {})
    seen: dict[str, dict[str, str]] = {}
    _bind(monkeypatch, fetch=_page(), seen=seen)
    await _extract()
    assert seen["fetch_cookies"] == {"old": "1"}

    _bind(monkeypatch, rendered=_rendered(cookies={"sid": "new"}))
    with caplog.at_level("INFO", logger="arachne"):
        await _extract(render=True)
    loaded = load_session(auto_session_id(URL))
    assert loaded.cookies == {"old": "1"}
    assert "session_persist=skipped_disabled" in caplog.text


async def test_httpx_success_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    _sessions(monkeypatch, tmp_path)
    _bind(monkeypatch, fetch=_page())
    with caplog.at_level("INFO", logger="arachne"):
        await _extract()
    assert list(tmp_path.glob("*.bin")) == []
    assert "session_persist=0" in caplog.text


async def test_render_failure_keeps_auto_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sessions(monkeypatch, tmp_path)
    write_session_file(auto_session_id(URL), {"old": "1"}, {})
    before = (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes()
    _bind(monkeypatch, fetch=_page(CHALLENGE_CF_HTML), rendered=render_failed())
    with pytest.raises(ArachneError) as exc:
        await _extract(render=False)
    assert exc.value.code == "render_failed"
    assert (tmp_path / f"{auto_session_id(URL)}.bin").read_bytes() == before


def test_write_session_file_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key()
    monkeypatch.setattr("app.config.SESSION_KEY", key.decode("ascii"))
    monkeypatch.setattr("app.config.SESSIONS_DIR", str(tmp_path))
    path = write_session_file("demo", {"sid": "old"}, {}, directory=tmp_path, key=key)
    original = path.read_bytes()
    replaced: list[tuple[str, str]] = []
    real_replace = os.replace

    def boom(src, dst):
        replaced.append((os.fspath(src), os.fspath(dst)))
        assert Path(src).parent == Path(dst).parent
        raise OSError("replace failed")

    monkeypatch.setattr("app.sessions.os.replace", boom)
    with pytest.raises(OSError):
        write_session_file("demo", {"sid": "new"}, {}, directory=tmp_path, key=key)
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
    assert replaced

    def spy(src, dst):
        replaced.append((os.fspath(src), os.fspath(dst)))
        assert Path(src).parent == Path(dst).parent
        return real_replace(src, dst)

    monkeypatch.setattr("app.sessions.os.replace", spy)
    write_session_file("demo", {"sid": "new"}, {"Authorization": "Bearer kept"}, directory=tmp_path, key=key)
    loaded = load_session("demo")
    assert loaded.cookies == {"sid": "new"}
    assert loaded.headers == {"Authorization": "Bearer kept"}
    assert list(tmp_path.glob("*.tmp")) == []
    assert Path(replaced[-1][1]) == path
