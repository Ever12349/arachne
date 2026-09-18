"""Batch jobs API: create, poll, cancel, and item-level extract errors."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app.errors import ArachneError
from app.jobs.models import CreateJobRequest, JobItemInput
from app.jobs.store import JobStore
from app.models import ExtractResponse, PageMetadata


def _poll_job(client: TestClient, job_id: str, *, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["status"] in {"completed", "cancelled"}:
            return last
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {last}")


def _dummy_extract(**_kwargs) -> ExtractResponse:
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


def test_create_job_returns_202(api_client: TestClient):
    response = api_client.post("/jobs", json={"items": [{"url": "https://example.com"}]})
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["total"] == 1
    uuid.UUID(body["job_id"])

    job = _poll_job(api_client, body["job_id"])
    assert job["status"] == "completed"
    assert job["total"] == 1
    assert job["succeeded_count"] == 1
    assert job["failed_count"] == 0
    assert job["cancelled_count"] == 0
    item = job["items"][0]
    assert item["index"] == 0
    assert item["url"] == "https://example.com"
    assert item["status"] == "succeeded"
    assert item["result"]["title"]
    assert "error" not in item["result"]


def test_per_item_failure_job_still_completed(api_client: TestClient):
    response = api_client.post(
        "/jobs",
        json={
            "defaults": {"ua_strategy": "default", "render": False, "site_profile": None},
            "items": [
                {"url": "https://example.com"},
                {"url": "http://127.0.0.1/"},
            ],
        },
    )
    assert response.status_code == 202
    job = _poll_job(api_client, response.json()["job_id"])
    assert job["status"] == "completed"
    assert job["succeeded_count"] == 1
    assert job["failed_count"] == 1
    assert job["cancelled_count"] == 0
    assert job["items"][0]["status"] == "succeeded"
    failed = job["items"][1]
    assert failed["status"] == "failed"
    assert failed["result"]["error"]["code"] == "bad_url"
    assert "message" in failed["result"]["error"]
    assert isinstance(failed["result"]["error"]["detail"], dict)


def test_cancel_pending_running_finishes(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    entered = threading.Event()
    release = threading.Event()

    async def gated_extract(**kwargs):
        loop = asyncio.get_running_loop()
        entered.set()
        await loop.run_in_executor(None, lambda: release.wait(timeout=5))
        result = _dummy_extract()
        result.requested_url = kwargs["url"]
        return result

    monkeypatch.setattr("app.config.JOB_CONCURRENCY", 1)
    monkeypatch.setattr("app.jobs.worker.run_extract", gated_extract)

    response = api_client.post(
        "/jobs",
        json={
            "items": [
                {"url": "https://example.com/a"},
                {"url": "https://example.com/b"},
                {"url": "https://example.com/c"},
            ]
        },
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert entered.wait(timeout=3), "first item never started"

    try:
        cancel = api_client.post(f"/jobs/{job_id}/cancel")
        assert cancel.status_code == 200
        cancelled_view = cancel.json()
        assert any(item["status"] == "cancelled" for item in cancelled_view["items"])
        assert any(item["status"] == "running" for item in cancelled_view["items"])

        release.set()
        final = _poll_job(api_client, job_id)
        assert final["status"] == "cancelled"
        statuses = [item["status"] for item in final["items"]]
        assert "pending" not in statuses
        assert "cancelled" in statuses
        assert "succeeded" in statuses
        assert final["cancelled_count"] >= 1
        assert final["succeeded_count"] >= 1

        again = api_client.post(f"/jobs/{job_id}/cancel")
        assert again.status_code == 200
        assert again.json()["status"] == "cancelled"
    finally:
        release.set()


def test_cancel_queued_job(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    async def drop(_job_id: str) -> None:
        return None

    monkeypatch.setattr(api_client.app.state.job_worker, "enqueue", drop)
    response = api_client.post(
        "/jobs",
        json={"items": [{"url": "https://example.com/a"}, {"url": "https://example.com/b"}]},
    )
    job_id = response.json()["job_id"]
    queued = api_client.get(f"/jobs/{job_id}").json()
    assert queued["status"] == "queued"
    assert all(item["status"] == "pending" for item in queued["items"])

    cancel = api_client.post(f"/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    body = cancel.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_count"] == 2
    assert all(item["status"] == "cancelled" for item in body["items"])


def test_cancel_idempotent_on_completed(api_client: TestClient):
    response = api_client.post("/jobs", json={"items": [{"url": "https://example.com"}]})
    job_id = response.json()["job_id"]
    job = _poll_job(api_client, job_id)
    assert job["status"] == "completed"
    cancel = api_client.post(f"/jobs/{job_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["status"] == "completed"
    assert cancel.json()["succeeded_count"] == 1


def test_job_not_found(api_client: TestClient):
    missing = str(uuid.uuid4())
    get_resp = api_client.get(f"/jobs/{missing}")
    assert get_resp.status_code == 404
    assert get_resp.json()["error"]["code"] == "job_not_found"
    cancel = api_client.post(f"/jobs/{missing}/cancel")
    assert cancel.status_code == 404
    assert cancel.json()["error"]["code"] == "job_not_found"


def test_max_urls_rejected(api_client: TestClient):
    items = [{"url": "https://example.com/"} for _ in range(51)]
    response = api_client.post("/jobs", json={"items": items})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "bad_url"
    assert body["error"]["detail"]["max_urls"] == 50
    assert body["error"]["detail"]["count"] == 51


def test_empty_items_rejected(api_client: TestClient):
    response = api_client.post("/jobs", json={"items": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_url"


def test_create_job_does_not_consume_qps(api_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    async def drop(_job_id: str) -> None:
        return None

    monkeypatch.setattr(api_client.app.state.job_worker, "enqueue", drop)
    for _ in range(5):
        assert api_client.post("/extract", json={"url": "https://example.com/"}).status_code == 200
    created = api_client.post("/jobs", json={"items": [{"url": "https://example.com/"}]})
    assert created.status_code == 202
    sixth = api_client.post("/extract", json={"url": "https://example.com/"})
    assert sixth.status_code == 429
    assert sixth.json()["error"]["code"] == "rate_limited"


def test_job_items_do_consume_qps(api_client: TestClient):
    created = api_client.post("/jobs", json={"items": [{"url": "https://example.com/"}]})
    assert created.status_code == 202
    job = _poll_job(api_client, created.json()["job_id"])
    assert job["status"] == "completed"
    stats = api_client.get("/stats").json()
    assert stats["requests_total"] >= 1


@pytest.mark.asyncio
async def test_store_expired_job_is_missing():
    clock = {"now": 0.0}

    def timer() -> float:
        return clock["now"]

    store = JobStore(maxsize=10, ttl=10.0, timer=timer)
    job = await store.create(CreateJobRequest(items=[JobItemInput(url="https://example.com")]))
    assert await store.get(job.job_id) is not None
    clock["now"] = 11.0
    assert await store.get(job.job_id) is None
    with pytest.raises(ArachneError) as exc:
        await store.snapshot(job.job_id)
    assert exc.value.code == "job_not_found"
