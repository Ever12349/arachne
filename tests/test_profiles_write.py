"""POST /profiles writes P3 JSON files; 409 unless overwrite."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.fetch import ssrf_request_hook
from app.main import app
from app.profiles.schema import SiteProfile
from tests.conftest import ARTICLE_PROFILE_HTML, set_high_qps
from tests.test_profiles import _example_payload

EXAMPLE = Path(__file__).resolve().parents[1] / "profiles" / "examples" / "example-com.json"


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


def test_write_profile_creates_json_file(api_client: TestClient):
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    response = api_client.post("/profiles", json={"profile": payload, "overwrite": False})
    assert response.status_code == 200, response.text
    written = response.json()["profile"]
    SiteProfile.model_validate(written)
    assert written["id"] == "example-com"
    registry_dir = Path(api_client.app.state.profile_registry._dir)
    path = registry_dir / "example-com.json"
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["id"] == "example-com"
    assert on_disk["title_selector"] == "h1.article-title"
    assert path.name == f"{on_disk['id']}.json"


def test_write_without_overwrite_is_409(api_client: TestClient):
    payload = _example_payload()
    first = api_client.post("/profiles", json={"profile": payload})
    assert first.status_code == 200, first.text
    second = api_client.post("/profiles", json={"profile": payload})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "profile_exists"
    assert second.json()["error"]["detail"]["id"] == "example-com"


def test_overwrite_replaces_file(api_client: TestClient):
    payload = _example_payload()
    first = api_client.post("/profiles", json={"profile": payload})
    assert first.status_code == 200
    updated = _example_payload(version="2", title_selector="h1.other")
    replaced = api_client.post("/profiles", json={"profile": updated, "overwrite": True})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["profile"]["version"] == "2"
    assert replaced.json()["profile"]["title_selector"] == "h1.other"
    path = Path(api_client.app.state.profile_registry._dir) / "example-com.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["version"] == "2"
    assert on_disk["title_selector"] == "h1.other"


def test_hand_authored_profile_does_not_need_suggest(api_client: TestClient):
    payload = {
        "id": "hand-made",
        "version": "1",
        "hosts": ["handmade.test"],
        "title_selector": "h1",
        "main_selector": "p",
        "remove_selectors": [],
        "meta": {},
        "strict": False,
        "disable_links": False,
    }
    response = api_client.post("/profiles", json={"profile": payload})
    assert response.status_code == 200, response.text
    assert response.json()["profile"]["id"] == "hand-made"
    path = Path(api_client.app.state.profile_registry._dir) / "hand-made.json"
    assert path.is_file()


def test_invalid_profile_schema_is_400(api_client: TestClient):
    response = api_client.post(
        "/profiles",
        json={"profile": {"id": "bad id!!", "version": "1", "hosts": ["example.com"]}},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_write_hot_reloads_for_extract(public_dns, monkeypatch: pytest.MonkeyPatch):
    payload = _example_payload()
    with _client_for_html(monkeypatch) as client:
        set_high_qps(client)
        missing = client.post("/extract", json={"url": "https://example.com/article"})
        assert missing.status_code == 200
        assert missing.json()["profile_id"] == ""
        written = client.post("/profiles", json={"profile": payload})
        assert written.status_code == 200, written.text
        auto = client.post("/extract", json={"url": "https://www.example.com/article"})
        forced = client.post(
            "/extract",
            json={"url": "https://other.test/page", "site_profile": "example-com"},
        )
    assert auto.status_code == 200
    assert auto.json()["profile_id"] == "example-com"
    assert auto.json()["title"] == "Profile Title"
    assert auto.json()["profile_fallback"] is False
    assert forced.status_code == 200
    assert forced.json()["title"] == "Profile Title"
