"""HTTP API contract: success envelope and mapped error codes."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.fetch import ssrf_request_hook
from app.main import app
from tests.conftest import EMPTY_HTML, SAMPLE_HTML


def test_health(api_client: TestClient):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_version(api_client: TestClient):
    spec = api_client.get("/openapi.json").json()
    assert spec["info"]["version"] == "0.8.1"


def test_request_id_generated_and_echoed(api_client: TestClient):
    response = api_client.get("/health")
    assert response.headers.get("X-Request-Id")
    echoed = api_client.get("/health", headers={"X-Request-Id": "client-rid-1"})
    assert echoed.headers.get("X-Request-Id") == "client-rid-1"


def test_metrics_lists_expected_series(api_client: TestClient):
    extract = api_client.get("/extract", params={"url": "https://example.com/"})
    assert extract.status_code == 200
    assert api_client.post("/extract", json={"url": "http://127.0.0.1/"}).status_code == 400
    body = api_client.get("/metrics").text
    for name in (
        "arachne_requests_total",
        "arachne_errors_total",
        "arachne_in_flight",
        "arachne_cache_hits_total",
        "arachne_cache_misses_total",
        "arachne_request_latency_seconds",
    ):
        assert name in body


def test_stats_shape_before_extracts(api_client: TestClient):
    response = api_client.get("/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["requests_total"] == 0
    assert data["errors_by_code"] == {}
    assert data["cache_hits"] == 0
    assert data["cache_misses"] == 0
    assert data["in_flight"] == 0
    assert data["latency_ms_sum"] == 0 or data["latency_ms_sum"] == 0.0
    assert data["latency_ms_count"] == 0


def test_get_extract_success(api_client: TestClient):
    response = api_client.get("/extract", params={"url": "https://www.example.com/article"})
    assert response.status_code == 200
    data = response.json()
    assert data["requested_url"] == "https://www.example.com/article"
    assert data["url"]
    assert data["status_code"] == 200
    assert data["title"]
    assert data["main_text"]
    assert data["truncated"] is False
    assert "cached" not in data
    assert data["profile_id"] == ""
    assert data["profile_version"] == ""
    assert data["profile_fallback"] is False
    assert "description" in data["metadata"]
    assert "og" in data["metadata"]
    assert data["metadata"]["og"]["title"] == "OG Title"
    assert isinstance(data["links"], list)
    if data["links"]:
        assert "href" in data["links"][0]
        assert "text" in data["links"][0]
    assert data["images"] == []
    assert data["metadata"]["og"]["image"] == "https://www.example.com/images/og.png"


def test_post_extract_success(api_client: TestClient):
    response = api_client.post("/extract", json={"url": "https://www.example.com/article"})
    assert response.status_code == 200
    assert response.json()["title"]


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "file:///etc/passwd", "ftp://example.com/", "not-a-url"],
)
def test_bad_url(api_client: TestClient, url: str):
    response = api_client.post("/extract", json={"url": url})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "bad_url"
    assert "message" in body["error"]
    assert isinstance(body["error"]["detail"], dict)


def test_missing_url_is_bad_url(api_client: TestClient):
    response = api_client.get("/extract")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_extract_empty_is_422(public_dns, monkeypatch: pytest.MonkeyPatch):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=EMPTY_HTML.encode("utf-8"),
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
        response = client.post("/extract", json={"url": "https://example.com/empty"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "extract_empty"


@pytest.mark.parametrize(
    "status,http_status,code",
    [
        (401, 502, "unauthorized_upstream"),
        (403, 502, "unauthorized_upstream"),
        (404, 502, "fetch_failed"),
    ],
)
def test_upstream_http_errors(public_dns, monkeypatch: pytest.MonkeyPatch, status: int, http_status: int, code: str):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
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
        response = client.post("/extract", json={"url": "https://example.com/"})
    assert response.status_code == http_status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["detail"]["status_code"] == status


def test_timeout_is_504(public_dns, monkeypatch: pytest.MonkeyPatch):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out", request=request)

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        response = client.get("/extract", params={"url": "https://example.com/"})
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"


def test_unsupported_content_is_422(public_dns, monkeypatch: pytest.MonkeyPatch):
    def fake_create() -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"PNG",
                headers={"content-type": "image/png"},
                request=request,
            )

        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"request": [ssrf_request_hook]},
        )

    monkeypatch.setattr("app.main.create_http_client", fake_create)
    with TestClient(app) as client:
        response = client.post("/extract", json={"url": "https://example.com/a.png"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_content"
