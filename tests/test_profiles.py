"""Site profiles: host match, conflicts, profile_invalid, strict, fallback, cache key."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache import cache_key, profile_cache_token
from app.errors import ArachneError
from app.fetch import ssrf_request_hook
from app.main import app
from app.profiles.apply import extract_with_profile
from app.profiles.loader import ProfileRegistry, resolve_profile
from app.profiles.schema import SiteProfile
from tests.conftest import ARTICLE_PROFILE_HTML, SAMPLE_HTML, set_high_qps

EXAMPLE_PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "examples" / "example-com.json"


def _write_profile(directory: Path, payload: dict, filename: str | None = None) -> Path:
    profile_id = payload["id"]
    path = directory / f"{filename or profile_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _example_payload(**overrides) -> dict:
    data = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def _registry(directory: Path, **kwargs) -> ProfileRegistry:
    return ProfileRegistry(directory, debounce_seconds=0, **kwargs)


def _client_for_html(monkeypatch: pytest.MonkeyPatch, html: str = ARTICLE_PROFILE_HTML) -> TestClient:
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=html.encode("utf-8"),
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


def test_example_profile_file_matches_locked_schema():
    data = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))
    profile = SiteProfile.model_validate(data)
    assert profile.id == "example-com"
    assert profile.version == "1"
    assert profile.hosts == ["example.com"]
    assert profile.title_selector == "h1.article-title"
    assert profile.main_selector == "article .content"
    assert profile.remove_selectors == [".ads", "nav"]
    assert profile.meta == {"description": "meta[name='description']"}
    assert profile.strict is False
    assert profile.disable_links is False


def test_match_normalizes_www(tmp_path: Path):
    _write_profile(tmp_path, _example_payload())
    registry = _registry(tmp_path)
    matched = registry.match_url("https://www.example.com/article")
    assert matched is not None
    assert matched.id == "example-com"
    assert registry.match_host("EXAMPLE.COM") is matched
    assert registry.match_url("https://other.test/") is None


def test_conflict_highest_profile_id_wins(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write_profile(
        tmp_path,
        _example_payload(id="aaa-profile", hosts=["example.com"], title_selector="h1"),
    )
    _write_profile(
        tmp_path,
        _example_payload(id="zzz-profile", hosts=["www.example.com"], title_selector="h1.article-title"),
    )
    with caplog.at_level(logging.WARNING, logger="arachne"):
        registry = _registry(tmp_path)
        winner = registry.match_host("example.com")
    assert winner is not None
    assert winner.id == "zzz-profile"
    assert "zzz-profile" in caplog.text
    assert "aaa-profile" in caplog.text
    assert "example.com" in caplog.text


def test_bad_json_skipped_falls_back_to_generic(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="arachne"):
        registry = _registry(tmp_path)
        assert registry.match_host("example.com") is None
    assert "broken.json" in caplog.text


def test_filename_id_mismatch_skipped(tmp_path: Path):
    _write_profile(tmp_path, _example_payload(id="example-com"), filename="other-id")
    registry = _registry(tmp_path)
    with pytest.raises(ArachneError) as exc:
        registry.get("other-id")
    assert exc.value.code == "profile_invalid"
    assert registry.match_host("example.com") is None


def test_resolve_explicit_missing_is_profile_invalid(tmp_path: Path):
    registry = _registry(tmp_path)
    with pytest.raises(ArachneError) as exc:
        resolve_profile(registry, site_profile="missing-id", url="https://example.com/")
    assert exc.value.code == "profile_invalid"
    assert exc.value.detail.get("site_profile") == "missing-id"


def test_resolve_explicit_malformed_id_is_profile_invalid(tmp_path: Path):
    registry = _registry(tmp_path)
    with pytest.raises(ArachneError) as exc:
        registry.get("bad id!!")
    assert exc.value.code == "profile_invalid"


def test_apply_profile_success_selectors_and_remove():
    profile = SiteProfile.model_validate(_example_payload())
    result = extract_with_profile(
        ARTICLE_PROFILE_HTML,
        profile=profile,
        requested_url="https://example.com/article",
        final_url="https://example.com/article",
        status_code=200,
        content_type="text/html; charset=utf-8",
    )
    assert result.title == "Profile Title"
    assert "Profile main text" in result.main_text
    assert "SPAM" not in result.main_text
    assert result.profile_id == "example-com"
    assert result.profile_version == "1"
    assert result.profile_fallback is False
    assert result.metadata.description == "Profile description from meta."
    hrefs = [link.href for link in result.links]
    assert "https://example.com/article-link" in hrefs
    assert "https://example.com/ad" not in hrefs
    assert "https://example.com/nav-only" not in hrefs


def test_disable_links_clears_links():
    profile = SiteProfile.model_validate(_example_payload(disable_links=True))
    result = extract_with_profile(
        ARTICLE_PROFILE_HTML,
        profile=profile,
        requested_url="https://example.com/a",
        final_url="https://example.com/a",
        status_code=200,
        content_type="text/html",
    )
    assert result.links == []
    assert result.profile_fallback is False


def test_invalid_css_is_failed_field_not_500():
    profile = SiteProfile.model_validate(
        _example_payload(title_selector="///not a selector", main_selector="article .content", strict=False)
    )
    result = extract_with_profile(
        ARTICLE_PROFILE_HTML,
        profile=profile,
        requested_url="https://example.com/a",
        final_url="https://example.com/a",
        status_code=200,
        content_type="text/html",
    )
    assert result.title == ""
    assert "Profile main text" in result.main_text
    assert result.profile_fallback is False


def test_strict_extract_empty_when_selectors_miss():
    profile = SiteProfile.model_validate(
        _example_payload(title_selector="h1.nope", main_selector="div.nope", strict=True)
    )
    with pytest.raises(ArachneError) as exc:
        extract_with_profile(
            ARTICLE_PROFILE_HTML,
            profile=profile,
            requested_url="https://example.com/a",
            final_url="https://example.com/a",
            status_code=200,
            content_type="text/html",
        )
    assert exc.value.code == "extract_empty"


def test_non_strict_fallback_flag_uses_generic_extract():
    profile = SiteProfile.model_validate(
        _example_payload(title_selector="h1.nope", main_selector="div.nope", strict=False)
    )
    result = extract_with_profile(
        ARTICLE_PROFILE_HTML,
        profile=profile,
        requested_url="https://example.com/a",
        final_url="https://example.com/a",
        status_code=200,
        content_type="text/html",
    )
    assert result.profile_id == "example-com"
    assert result.profile_version == "1"
    assert result.profile_fallback is True
    assert result.title
    assert result.main_text
    assert "SPAM" not in result.main_text


def test_no_profile_generic_has_empty_profile_fields():
    result = extract_with_profile(
        SAMPLE_HTML,
        profile=None,
        requested_url="https://www.example.com/article",
        final_url="https://www.example.com/article",
        status_code=200,
        content_type="text/html; charset=utf-8",
    )
    assert result.profile_id == ""
    assert result.profile_version == ""
    assert result.profile_fallback is False
    assert result.title


def test_cache_key_includes_profile_id_and_version():
    base = cache_key("https://example.com/a", {}, {})
    assert base[4] == ""
    same = cache_key(
        "https://example.com/a",
        {},
        {},
        profile_id="example-com",
        profile_version="1",
    )
    other_ver = cache_key(
        "https://example.com/a",
        {},
        {},
        profile_id="example-com",
        profile_version="2",
    )
    other_id = cache_key(
        "https://example.com/a",
        {},
        {},
        profile_id="other",
        profile_version="1",
    )
    assert same != base
    assert same != other_ver
    assert same != other_id
    assert same[4] == "example-com@1"
    assert profile_cache_token("", "1") == ""
    assert profile_cache_token("example-com", "1") == "example-com@1"


def test_post_site_profile_invalid_is_400(public_dns, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        missing = client.post(
            "/extract",
            json={"url": "https://example.com/article", "site_profile": "no-such"},
        )
        malformed = client.post(
            "/extract",
            json={"url": "https://example.com/article", "site_profile": "bad id"},
        )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "profile_invalid"
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "profile_invalid"


def test_get_ignores_site_profile_query(public_dns, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        response = client.get(
            "/extract",
            params={"url": "https://example.com/article", "site_profile": "no-such"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["profile_id"] == ""
    assert data["profile_fallback"] is False


def test_auto_match_and_explicit_profile_via_api(public_dns, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _write_profile(tmp_path, _example_payload())
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        auto = client.post("/extract", json={"url": "https://www.example.com/article"})
        forced = client.post(
            "/extract",
            json={"url": "https://other.test/page", "site_profile": "example-com"},
        )
        unmatched = client.get("/extract", params={"url": "https://other.test/page"})
    assert auto.status_code == 200
    assert auto.json()["profile_id"] == "example-com"
    assert auto.json()["profile_version"] == "1"
    assert auto.json()["profile_fallback"] is False
    assert auto.json()["title"] == "Profile Title"
    assert forced.status_code == 200
    assert forced.json()["title"] == "Profile Title"
    assert forced.json()["profile_id"] == "example-com"
    assert unmatched.status_code == 200
    assert unmatched.json()["profile_id"] == ""
    unmatched_hrefs = [link["href"] for link in unmatched.json()["links"]]
    assert any("/ad" in href or "nav-only" in href for href in unmatched_hrefs)


def test_strict_and_fallback_via_api(public_dns, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _write_profile(
        tmp_path,
        _example_payload(id="strict-miss", hosts=["example.com"], title_selector="h1.nope", main_selector=".nope", strict=True),
    )
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        strict = client.post("/extract", json={"url": "https://example.com/article", "site_profile": "strict-miss"})
    assert strict.status_code == 422
    assert strict.json()["error"]["code"] == "extract_empty"

    _write_profile(
        tmp_path,
        _example_payload(
            id="loose-miss",
            hosts=["example.com"],
            title_selector="h1.nope",
            main_selector=".nope",
            strict=False,
        ),
    )
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        loose = client.post("/extract", json={"url": "https://example.com/article", "site_profile": "loose-miss"})
    assert loose.status_code == 200
    body = loose.json()
    assert body["profile_id"] == "loose-miss"
    assert body["profile_fallback"] is True
    assert body["title"]
    assert body["main_text"]


def test_profile_token_splits_cache(public_dns, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _write_profile(tmp_path, _example_payload())
    _write_profile(
        tmp_path,
        _example_payload(id="v2", version="2", hosts=["other.test"], title_selector="h1.article-title", main_selector="article .content"),
    )
    monkeypatch.setattr("app.config.PROFILES_DIR", str(tmp_path))
    counts: dict[str, int] = {"n": 0}

    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            counts["n"] += 1
            return httpx.Response(
                200,
                content=ARTICLE_PROFILE_HTML.encode("utf-8"),
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    url = "https://example.com/article"
    with TestClient(app) as client:
        set_high_qps(client)
        r1 = client.post("/extract", json={"url": url})
        r2 = client.post("/extract", json={"url": url})
        r3 = client.post("/extract", json={"url": url, "site_profile": "v2"})
        r4 = client.post("/extract", json={"url": url, "site_profile": "v2"})
        r5 = client.get("/extract", params={"url": url})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
    assert r4.status_code == 200
    assert r5.status_code == 200
    assert r1.json()["profile_id"] == "example-com"
    assert r3.json()["profile_id"] == "v2"
    assert r3.json()["profile_version"] == "2"
    assert r5.json()["profile_id"] == "example-com"
    # auto-match twice + explicit v2 twice (second of each is a cache hit)
    assert counts["n"] == 2


def test_hot_reload_after_debounce(tmp_path: Path):
    clock = {"now": 0.0}

    def monotonic() -> float:
        return clock["now"]

    registry = ProfileRegistry(tmp_path, debounce_seconds=1.0, monotonic=monotonic)
    assert registry.match_host("example.com") is None
    _write_profile(tmp_path, _example_payload())
    clock["now"] = 0.5
    assert registry.match_host("example.com") is None
    clock["now"] = 1.5
    matched = registry.match_host("example.com")
    assert matched is not None
    assert matched.id == "example-com"
