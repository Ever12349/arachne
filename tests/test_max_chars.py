"""main_text truncation: hard cap, max_chars clamp, truncated flag."""

from __future__ import annotations

from app.config import MAIN_TEXT_MAX_CHARS
from app.extract import apply_max_chars
from app.models import ExtractResponse, PageMetadata
from tests.conftest import set_high_qps


def _response(text: str) -> ExtractResponse:
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


def test_omit_max_chars_hard_cap_sets_truncated():
    out = apply_max_chars(_response("x" * (MAIN_TEXT_MAX_CHARS + 10)), None)
    assert len(out.main_text) == MAIN_TEXT_MAX_CHARS
    assert out.truncated is True


def test_omit_max_chars_under_cap_not_truncated():
    out = apply_max_chars(_response("short"), None)
    assert out.main_text == "short"
    assert out.truncated is False


def test_max_chars_truncates_and_sets_flag():
    out = apply_max_chars(_response("abcdefghij"), 4)
    assert out.main_text == "abcd"
    assert out.truncated is True


def test_max_chars_not_truncated_when_shorter():
    out = apply_max_chars(_response("abc"), 50)
    assert out.main_text == "abc"
    assert out.truncated is False


def test_max_chars_clamped_to_at_least_one():
    out = apply_max_chars(_response("hello"), 0)
    assert out.main_text == "h"
    assert out.truncated is True
    out_neg = apply_max_chars(_response("hello"), -20)
    assert out_neg.main_text == "h"


def test_max_chars_clamped_to_hard_cap():
    out = apply_max_chars(_response("x" * (MAIN_TEXT_MAX_CHARS + 5)), MAIN_TEXT_MAX_CHARS + 100)
    assert len(out.main_text) == MAIN_TEXT_MAX_CHARS
    assert out.truncated is True


def test_get_max_chars_truncates(api_client):
    response = api_client.get(
        "/extract",
        params={"url": "https://www.example.com/article", "max_chars": 8},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["main_text"]) <= 8
    assert data["truncated"] is True
    assert "cached" not in data


def test_post_max_chars_truncates(api_client):
    set_high_qps(api_client)
    response = api_client.post(
        "/extract",
        json={"url": "https://www.example.com/article", "max_chars": 12},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["main_text"]) <= 12
    assert data["truncated"] is True


def test_omit_max_chars_api_not_truncated(api_client):
    response = api_client.get("/extract", params={"url": "https://www.example.com/article"})
    assert response.status_code == 200
    assert response.json()["truncated"] is False


def test_max_chars_zero_clamped_via_api(api_client):
    response = api_client.get(
        "/extract",
        params={"url": "https://www.example.com/article", "max_chars": 0},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["main_text"]) == 1
    assert data["truncated"] is True
