"""Egress allowlist for user crawl URLs; LLM base URL is unconstrained."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.egress import host_allowed
from app.errors import ArachneError
from app.profiles.llm import complete_chat
from app.render import render_url
from app.ssrf import assert_public_http_url


def test_host_allowed_disabled_when_empty():
    assert host_allowed("evil-example.com", allowlist=()) is True
    assert host_allowed("anything.test", allowlist=()) is True


def test_host_allowed_suffix_not_substring():
    allow = ("example.com",)
    assert host_allowed("example.com", allowlist=allow) is True
    assert host_allowed("www.example.com", allowlist=allow) is True
    assert host_allowed("api.example.com", allowlist=allow) is True
    assert host_allowed("evil-example.com", allowlist=allow) is False
    assert host_allowed("example.com.evil", allowlist=allow) is False
    assert host_allowed("notexample.com", allowlist=allow) is False


@pytest.mark.asyncio
async def test_assert_public_http_url_blocks_off_allowlist(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.EGRESS_ALLOWLIST", ("example.com",))
    await assert_public_http_url("https://example.com/x")
    with pytest.raises(ArachneError) as exc:
        await assert_public_http_url("https://evil-example.com/")
    assert exc.value.code == "egress_blocked"
    assert exc.value.detail["host"] == "evil-example.com"


def test_extract_allowlist_is_403(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.EGRESS_ALLOWLIST", ("example.com",))
    blocked = api_client.post("/extract", json={"url": "https://evil-example.com/"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "egress_blocked"
    ok = api_client.post("/extract", json={"url": "https://example.com/"})
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_render_uses_allowlist_without_pinning(public_dns, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.EGRESS_ALLOWLIST", ("example.com",))
    with pytest.raises(ArachneError) as exc:
        await render_url("https://other.test/")
    assert exc.value.code == "egress_blocked"


@pytest.mark.asyncio
async def test_llm_base_url_not_on_allowlist(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.config.EGRESS_ALLOWLIST", ("example.com",))
    monkeypatch.setattr("app.config.LLM_API_KEY", "sk-test")
    monkeypatch.setattr("app.config.LLM_BASE_URL", "https://llm.internal/v1")

    captured: dict[str, str] = {}

    class _FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": '{"title_selector":"h1","main_selector":"p"}'}}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url: str, **_kwargs):
            captured["url"] = url
            return _FakeResponse()

    monkeypatch.setattr("app.profiles.llm.httpx.AsyncClient", lambda **_kw: _FakeClient())
    content = await complete_chat([{"role": "user", "content": "hi"}])
    assert captured["url"] == "https://llm.internal/v1/chat/completions"
    assert "title_selector" in content
