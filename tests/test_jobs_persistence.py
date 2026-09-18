"""Job persistence: SQLite reopen, search, delete, client isolation, TTL."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import create_db_engine, init_db, session_factory
from app.errors import ArachneError
from app.jobs.models import CreateJobRequest, JobItemInput
from app.jobs.store import JobStore
from app.jobs.tables import JobRow
from app.models import ExtractResponse, PageMetadata
from tests.test_jobs import _poll_job


async def _open_store(database_url: str, *, ttl_seconds: int = 0) -> tuple[JobStore, AsyncEngine]:
    engine = create_db_engine(database_url)
    await init_db(engine)
    store = JobStore(session_factory(engine), ttl_seconds=ttl_seconds)
    return store, engine


@pytest.mark.asyncio
async def test_create_get_survives_reopen(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'persist.db'}"
    store, engine = await _open_store(url)
    try:
        job = await store.create(
            CreateJobRequest(items=[JobItemInput(url="https://example.com/reopen")]),
            client_id="alpha",
        )
        job_id = job.job_id
        assert (await store.get(job_id, client_id="alpha")) is not None
    finally:
        await engine.dispose()

    store2, engine2 = await _open_store(url)
    try:
        loaded = await store2.get(job_id, client_id="alpha")
        assert loaded is not None
        assert loaded.job_id == job_id
        assert loaded.client_id == "alpha"
        assert loaded.status == "queued"
        assert loaded.items[0].url == "https://example.com/reopen"
        view = await store2.snapshot(job_id, client_id="alpha")
        assert view.job_id == job_id
        assert view.client_id == "alpha"
    finally:
        await engine2.dispose()


@pytest.mark.asyncio
async def test_store_client_mismatch_is_not_found(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'client.db'}"
    store, engine = await _open_store(url)
    try:
        job = await store.create(
            CreateJobRequest(items=[JobItemInput(url="https://example.com")]),
            client_id="alpha",
        )
        assert await store.get(job.job_id, client_id="beta") is None
        with pytest.raises(ArachneError) as exc:
            await store.snapshot(job.job_id, client_id="beta")
        assert exc.value.code == "job_not_found"
        assert await store.get(job.job_id, client_id="alpha") is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_store_delete_and_search(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'search.db'}"
    store, engine = await _open_store(url)
    try:
        first = await store.create(
            CreateJobRequest(items=[JobItemInput(url="https://example.com/a")]),
            client_id="alpha",
        )
        later = await store.create(
            CreateJobRequest(items=[JobItemInput(url="https://example.com/a")]),
            client_id="alpha",
        )
        other = await store.create(
            CreateJobRequest(items=[JobItemInput(url="https://example.com/a")]),
            client_id="beta",
        )
        hits = await store.search("https://example.com/a", client_id="alpha")
        assert [job.job_id for job in hits] == [later.job_id, first.job_id]
        assert all(job.client_id == "alpha" for job in hits)
        assert other.job_id not in {job.job_id for job in hits}

        await store.delete(first.job_id, client_id="alpha")
        assert await store.get(first.job_id, client_id="alpha") is None
        remaining = await store.search("https://example.com/a", client_id="alpha")
        assert [job.job_id for job in remaining] == [later.job_id]

        assert await store.start_job(later.job_id) is not None
        assert await store.start_item(later.job_id, 0)
        await store.succeed_item(
            later.job_id,
            0,
            ExtractResponse(
                url="https://example.com/final",
                requested_url="https://example.com/a",
                status_code=200,
                title="T",
                main_text="hello",
                metadata=PageMetadata(),
                links=[],
            ),
        )
        await store.finish_job(later.job_id)
        by_final = await store.search("https://example.com/final", client_id="alpha")
        assert [job.job_id for job in by_final] == [later.job_id]
        with pytest.raises(ArachneError) as exc:
            await store.delete(other.job_id, client_id="alpha")
        assert exc.value.code == "job_not_found"
        assert await store.get(other.job_id, client_id="beta") is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_job_ttl_cleanup(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ttl.db'}"
    store, engine = await _open_store(url, ttl_seconds=10)
    try:
        job = await store.create(CreateJobRequest(items=[JobItemInput(url="https://example.com")]))
        assert await store.start_job(job.job_id) is not None
        await store.finish_job(job.job_id)
        loaded = await store.get(job.job_id)
        assert loaded is not None
        assert loaded.status == "completed"

        stale = datetime.now(timezone.utc) - timedelta(seconds=30)
        async with store._sessionmaker() as session:
            async with session.begin():
                row = await session.get(JobRow, job.job_id)
                assert row is not None
                row.updated_at = stale

        deleted = await store.cleanup_expired()
        assert deleted == 1
        assert await store.get(job.job_id) is None
        with pytest.raises(ArachneError) as exc:
            await store.snapshot(job.job_id)
        assert exc.value.code == "job_not_found"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_recover_incomplete_resets_running_items(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'recover.db'}"
    store, engine = await _open_store(url)
    try:
        job = await store.create(CreateJobRequest(items=[JobItemInput(url="https://example.com")]))
        assert await store.start_job(job.job_id) is not None
        assert await store.start_item(job.job_id, 0)
        ids = await store.recover_incomplete()
        assert job.job_id in ids
        loaded = await store.get(job.job_id)
        assert loaded is not None
        assert loaded.status == "queued"
        assert loaded.items[0].status == "pending"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ttl_zero_keeps_completed_jobs(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'keep.db'}"
    store, engine = await _open_store(url, ttl_seconds=0)
    try:
        job = await store.create(CreateJobRequest(items=[JobItemInput(url="https://example.com")]))
        await store.start_job(job.job_id)
        await store.finish_job(job.job_id)
        stale = datetime.now(timezone.utc) - timedelta(days=365)
        async with store._sessionmaker() as session:
            async with session.begin():
                row = await session.get(JobRow, job.job_id)
                assert row is not None
                row.updated_at = stale
        assert await store.cleanup_expired() == 0
        assert await store.get(job.job_id) is not None
    finally:
        await engine.dispose()


def test_api_search_delete_and_client_header(api_client: TestClient):
    headers = {"X-Arachne-Client": "alpha"}
    created = api_client.post(
        "/jobs",
        json={"items": [{"url": "https://example.com/search-me"}]},
        headers=headers,
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    deadline_job = _poll_job(api_client, job_id, headers=headers)
    assert deadline_job["status"] == "completed"
    assert deadline_job["client_id"] == "alpha"
    final_url = deadline_job["items"][0]["result"]["url"]

    other = api_client.get(f"/jobs/{job_id}", headers={"X-Arachne-Client": "beta"})
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "job_not_found"

    missing_header = api_client.get(f"/jobs/{job_id}")
    assert missing_header.status_code == 404

    by_requested = api_client.get(
        "/jobs/search",
        params={"url": "https://example.com/search-me"},
        headers={"X-Arachne-Client": "alpha"},
    )
    assert by_requested.status_code == 200
    ids = [job["job_id"] for job in by_requested.json()["jobs"]]
    assert job_id in ids

    by_final = api_client.get(
        "/jobs/search",
        params={"url": final_url},
        headers={"X-Arachne-Client": "alpha"},
    )
    assert by_final.status_code == 200
    assert job_id in [job["job_id"] for job in by_final.json()["jobs"]]

    isolated = api_client.get(
        "/jobs/search",
        params={"url": "https://example.com/search-me"},
        headers={"X-Arachne-Client": "beta"},
    )
    assert isolated.status_code == 200
    assert isolated.json()["jobs"] == []

    forbidden_delete = api_client.delete(f"/jobs/{job_id}", headers={"X-Arachne-Client": "beta"})
    assert forbidden_delete.status_code == 404
    assert api_client.get(f"/jobs/{job_id}", headers={"X-Arachne-Client": "alpha"}).status_code == 200

    deleted = api_client.delete(f"/jobs/{job_id}", headers={"X-Arachne-Client": "alpha"})
    assert deleted.status_code == 204
    assert api_client.get(f"/jobs/{job_id}", headers={"X-Arachne-Client": "alpha"}).status_code == 404


def test_search_requires_url_and_clamps_limit(api_client: TestClient):
    missing = api_client.get("/jobs/search")
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "bad_url"

    too_big = api_client.get("/jobs/search", params={"url": "https://example.com", "limit": 101})
    assert too_big.status_code == 400
    assert too_big.json()["error"]["code"] == "bad_url"
