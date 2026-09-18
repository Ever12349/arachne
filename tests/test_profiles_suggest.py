"""POST /profiles/suggest: heuristic DOM scoring, LLM mock, no disk writes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import ArachneError
from app.fetch import ssrf_request_hook
from app.main import app
from app.profiles.apply import extract_with_profile
from app.profiles.llm import normalize_selector_payload, parse_selector_json
from app.profiles.schema import SiteProfile
from app.profiles.suggest import (
    dom_skeleton,
    profile_id_from_host,
    self_test,
    suggest_from_html,
    suggest_heuristic,
)
from tests.conftest import set_high_qps

FIXTURES = Path(__file__).parent / "fixtures"
SUGGEST_ARTICLE_HTML = (FIXTURES / "suggest_article.html").read_text(encoding="utf-8")
SUGGEST_SHORT_HTML = (FIXTURES / "suggest_too_short.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_llm_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.LLM_API_KEY", "")


def _client_for_html(monkeypatch: pytest.MonkeyPatch, html: str) -> TestClient:
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


def test_profile_id_from_normalized_host():
    assert profile_id_from_host("www.example.com") == "example-com"
    assert profile_id_from_host("news.example.co.uk") == "news-example-co-uk"
    assert profile_id_from_host("EXAMPLE.COM") == "example-com"


def test_heuristic_selectors_self_test_on_fixture():
    sets = suggest_heuristic(SUGGEST_ARTICLE_HTML, url="https://www.example.com/story")
    assert sets
    best = sets[0]
    verified = self_test(
        SUGGEST_ARTICLE_HTML,
        best.title_selector,
        best.main_selector,
        list(best.remove_selectors),
    )
    assert verified is not None
    title, main = verified
    assert title == "Suggested Article Headline"
    assert "unique main article body" in main
    assert len(title) >= 2
    assert len(main) >= 80


@pytest.mark.asyncio
async def test_suggest_from_html_builds_p3_profile():
    result = await suggest_from_html(
        SUGGEST_ARTICLE_HTML,
        url="https://www.example.com/story",
        strategy="heuristic",
    )
    profile = result.profile
    assert profile.id == "example-com"
    assert profile.hosts == ["example.com"]
    assert profile.version == "1"
    assert profile.strict is False
    extracted = extract_with_profile(
        SUGGEST_ARTICLE_HTML,
        profile=profile,
        requested_url="https://www.example.com/story",
        final_url="https://www.example.com/story",
        status_code=200,
        content_type="text/html",
    )
    assert extracted.title == "Suggested Article Headline"
    assert "unique main article body" in extracted.main_text
    assert extracted.profile_fallback is False
    assert result.evidence.strategy_used == "heuristic"
    assert "Suggested Article Headline" in result.evidence.title_preview
    assert "unique main article body" in result.evidence.main_preview
    if result.evidence.alternatives is not None:
        assert len(result.evidence.alternatives) <= 3


@pytest.mark.asyncio
async def test_heuristic_fails_when_text_too_short():
    with pytest.raises(ArachneError) as exc:
        await suggest_from_html(SUGGEST_SHORT_HTML, url="https://example.com/x", strategy="heuristic")
    assert exc.value.code == "extract_empty"


def test_skeleton_respects_node_and_char_caps():
    skeleton = dom_skeleton(SUGGEST_ARTICLE_HTML, max_nodes=5, max_chars=80)
    assert skeleton.count("<") <= 5
    assert len(skeleton) <= 80
    full = dom_skeleton(SUGGEST_ARTICLE_HTML)
    assert "<article" in full or "article" in full
    assert "headline" in full or "entry-content" in full
    assert len(full) <= 30_000


def test_parse_selector_json_fenced_and_raw():
    raw = parse_selector_json('{"title_selector":"h1","main_selector":"article"}')
    assert raw["title_selector"] == "h1"
    fenced = parse_selector_json(
        '```json\n{"title_selector": "h1.headline", "main_selector": "div.entry-content"}\n```'
    )
    assert fenced["main_selector"] == "div.entry-content"
    wrapped = parse_selector_json('Here you go:\n{"title_selector":"h1","main_selector":"p"}\nThanks')
    payload = normalize_selector_payload(wrapped)
    assert payload["title_selector"] == "h1"
    assert payload["remove_selectors"] == []


def test_post_suggest_heuristic_does_not_write_disk(public_dns, monkeypatch: pytest.MonkeyPatch):
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        profiles_dir = Path(client.app.state.profile_registry._dir)
        before = list(profiles_dir.glob("*.json"))
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://www.example.com/story", "strategy": "heuristic"},
        )
        after = list(profiles_dir.glob("*.json"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profile"]["id"] == "example-com"
    assert body["profile"]["hosts"] == ["example.com"]
    assert body["evidence"]["strategy_used"] == "heuristic"
    assert "llm_skipped" not in body["evidence"] or body["evidence"]["llm_skipped"] is None
    SiteProfile.model_validate(body["profile"])
    extracted = extract_with_profile(
        SUGGEST_ARTICLE_HTML,
        profile=SiteProfile.model_validate(body["profile"]),
        requested_url="https://www.example.com/story",
        final_url="https://www.example.com/story",
        status_code=200,
        content_type="text/html",
    )
    assert extracted.title == "Suggested Article Headline"
    assert "unique main article body" in extracted.main_text
    assert after == before
    if body["evidence"].get("alternatives"):
        assert len(body["evidence"]["alternatives"]) <= 3


def test_strategy_llm_without_key_is_501(public_dns, monkeypatch: pytest.MonkeyPatch):
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://example.com/story", "strategy": "llm"},
        )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "llm_unavailable"


def test_strategy_auto_without_key_skips_llm(public_dns, monkeypatch: pytest.MonkeyPatch):
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://example.com/story", "strategy": "auto"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence"]["strategy_used"] == "heuristic"
    assert body["evidence"]["llm_skipped"] == "no_key"
    assert body["profile"]["id"] == "example-com"


def test_strategy_llm_mocked_success_verified(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.LLM_API_KEY", "sk-test")

    async def fake_propose(**_kwargs):
        return {
            "title_selector": "h1.headline",
            "main_selector": "div.entry-content",
            "remove_selectors": ["nav", ".ads", "aside"],
            "meta": {"description": "meta[name='description']"},
        }

    monkeypatch.setattr("app.profiles.llm.propose_selectors", fake_propose)
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://example.com/story", "strategy": "llm"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence"]["strategy_used"] == "llm"
    assert body["profile"]["title_selector"] == "h1.headline"
    assert body["profile"]["main_selector"] == "div.entry-content"
    assert "Suggested Article Headline" in body["evidence"]["title_preview"]
    assert "unique main article body" in body["evidence"]["main_preview"]


def test_strategy_llm_bad_selectors_are_502(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.LLM_API_KEY", "sk-test")

    async def fake_propose(**_kwargs):
        return {
            "title_selector": "h1.does-not-exist",
            "main_selector": "div.nope",
            "remove_selectors": [],
            "meta": {},
        }

    monkeypatch.setattr("app.profiles.llm.propose_selectors", fake_propose)
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://example.com/story", "strategy": "llm"},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "llm_failed"


def test_strategy_auto_falls_back_when_llm_fails(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.LLM_API_KEY", "sk-test")

    async def fake_propose(**_kwargs):
        raise ArachneError("llm_failed", "boom")

    monkeypatch.setattr("app.profiles.llm.propose_selectors", fake_propose)
    with _client_for_html(monkeypatch, SUGGEST_ARTICLE_HTML) as client:
        set_high_qps(client)
        response = client.post(
            "/profiles/suggest",
            json={"url": "https://example.com/story", "strategy": "auto"},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence"]["strategy_used"] == "heuristic"
    assert body["evidence"]["llm_skipped"] == "llm_failed"
    assert body["profile"]["title_selector"]
    assert body["profile"]["main_selector"]


def test_suggest_shares_global_qps(api_client: TestClient):
    statuses = []
    last = None
    for _ in range(5):
        last = api_client.post("/extract", json={"url": "https://example.com/"})
        statuses.append(last.status_code)
    assert statuses == [200, 200, 200, 200, 200]
    sixth = api_client.post("/profiles/suggest", json={"url": "https://example.com/"})
    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "rate_limited"


def test_suggest_rejects_private_url(api_client: TestClient):
    response = api_client.post("/profiles/suggest", json={"url": "http://127.0.0.1/"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_configurable_min_main_length(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.SUGGEST_MIN_MAIN_CHARS", 10_000)
    sets = suggest_heuristic(SUGGEST_ARTICLE_HTML, url="https://example.com/")
    assert sets == []
