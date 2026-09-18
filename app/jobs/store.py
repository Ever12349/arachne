"""SQLite-backed job store. The database is the sole source of truth."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import config
from app.errors import job_not_found
from app.jobs.models import (
    CreateJobRequest,
    ItemStatus,
    JobDefaults,
    JobItemInput,
    JobItemView,
    JobStatus,
    JobView,
)
from app.jobs.tables import JobItemRow, JobRow
from app.models import ErrorBody, ErrorResponse, ExtractResponse

logger = logging.getLogger("arachne")

T = TypeVar("T")

_TERMINAL_JOB = frozenset({"completed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class JobItemRecord:
    index: int
    url: str
    headers: dict[str, str] | None
    cookies: dict[str, str] | None
    max_chars: int | None
    session_id: str | None
    ua_strategy: str
    render: bool
    site_profile: str | None
    status: ItemStatus = "pending"
    result: ExtractResponse | None = None
    error: ErrorBody | None = None

    def to_view(self) -> JobItemView:
        payload: ExtractResponse | ErrorResponse | None = None
        if self.status == "succeeded":
            payload = self.result
        elif self.status == "failed" and self.error is not None:
            payload = ErrorResponse(error=self.error)
        return JobItemView(index=self.index, url=self.url, status=self.status, result=payload)


@dataclass
class JobRecord:
    job_id: str
    items: list[JobItemRecord]
    client_id: str = "default"
    status: JobStatus = "queued"
    cancel_requested: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def total(self) -> int:
        return len(self.items)

    def counts(self) -> tuple[int, int, int]:
        succeeded = failed = cancelled = 0
        for item in self.items:
            if item.status == "succeeded":
                succeeded += 1
            elif item.status == "failed":
                failed += 1
            elif item.status == "cancelled":
                cancelled += 1
        return succeeded, failed, cancelled

    def to_view(self) -> JobView:
        succeeded, failed, cancelled = self.counts()
        return JobView(
            job_id=self.job_id,
            client_id=self.client_id,
            status=self.status,
            total=self.total,
            succeeded_count=succeeded,
            failed_count=failed,
            cancelled_count=cancelled,
            items=[item.to_view() for item in self.items],
        )


def _pick(item_value: T | None, default_value: T) -> T:
    return default_value if item_value is None else item_value


def resolve_item(defaults: JobDefaults, item: JobItemInput, index: int) -> JobItemRecord:
    ua = _pick(item.ua_strategy, defaults.ua_strategy) or "default"
    render = _pick(item.render, defaults.render)
    return JobItemRecord(
        index=index,
        url=item.url,
        headers=_pick(item.headers, defaults.headers),
        cookies=_pick(item.cookies, defaults.cookies),
        max_chars=_pick(item.max_chars, defaults.max_chars),
        session_id=_pick(item.session_id, defaults.session_id),
        ua_strategy=ua,
        render=bool(render),
        site_profile=_pick(item.site_profile, defaults.site_profile),
    )


def _item_from_row(row: JobItemRow) -> JobItemRecord:
    result = ExtractResponse.model_validate(row.result) if row.result is not None else None
    error = ErrorBody.model_validate(row.error) if row.error is not None else None
    return JobItemRecord(
        index=row.index,
        url=row.requested_url,
        headers=dict(row.headers) if row.headers is not None else None,
        cookies=dict(row.cookies) if row.cookies is not None else None,
        max_chars=row.max_chars,
        session_id=row.session_id,
        ua_strategy=row.ua_strategy,
        render=bool(row.render),
        site_profile=row.site_profile,
        status=row.status,  # type: ignore[arg-type]
        result=result,
        error=error,
    )


def _record_from_row(row: JobRow) -> JobRecord:
    items = [_item_from_row(item) for item in sorted(row.items, key=lambda i: i.index)]
    return JobRecord(
        job_id=row.id,
        client_id=row.client_id,
        items=items,
        status=row.status,  # type: ignore[arg-type]
        cancel_requested=bool(row.cancel_requested),
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _item_row(job: JobRow, index: int) -> JobItemRow | None:
    for item in job.items:
        if item.index == index:
            return item
    return None


class JobStore:
    """Persists jobs to SQLite. Worker status changes are awaited writes."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._ttl_seconds = config.JOB_DB_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        self._locks_guard = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    async def _lock_for(self, job_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[job_id] = lock
            return lock

    async def _load_row(
        self,
        session: AsyncSession,
        job_id: str,
        client_id: str | None = None,
    ) -> JobRow | None:
        stmt = select(JobRow).where(JobRow.id == job_id)
        if client_id is not None:
            stmt = stmt.where(JobRow.client_id == client_id)
        return (await session.scalars(stmt)).first()

    async def cleanup_expired(self, *, now: datetime | None = None) -> int:
        """Delete completed/cancelled jobs older than TTL. 0 means keep forever."""
        ttl = self._ttl_seconds
        if ttl <= 0:
            return 0
        cutoff = (now or _utcnow()) - timedelta(seconds=ttl)
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(JobRow).where(
                            JobRow.status.in_(tuple(_TERMINAL_JOB)),
                        )
                    )
                ).all()
                stale = [row for row in rows if _as_utc(row.updated_at) < cutoff]
                for row in stale:
                    await session.delete(row)
                return len(stale)

    async def run_cleanup_loop(self, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                deleted = await self.cleanup_expired()
                if deleted:
                    logger.info("job ttl cleanup deleted=%s", deleted)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job ttl cleanup failed")

    async def create(self, body: CreateJobRequest, client_id: str = "default") -> JobRecord:
        await self.cleanup_expired()
        job_id = str(uuid.uuid4())
        now = _utcnow()
        items = [resolve_item(body.defaults, item, index) for index, item in enumerate(body.items)]
        async with self._sessionmaker() as session:
            async with session.begin():
                session.add(
                    JobRow(
                        id=job_id,
                        client_id=client_id,
                        status="queued",
                        cancel_requested=False,
                        created_at=now,
                        updated_at=now,
                    )
                )
                for item in items:
                    session.add(
                        JobItemRow(
                            job_id=job_id,
                            index=item.index,
                            requested_url=item.url,
                            result_url=None,
                            headers=item.headers,
                            cookies=item.cookies,
                            max_chars=item.max_chars,
                            session_id=item.session_id,
                            ua_strategy=item.ua_strategy,
                            render=item.render,
                            site_profile=item.site_profile,
                            status="pending",
                            result=None,
                            error=None,
                        )
                    )
        return JobRecord(
            job_id=job_id,
            client_id=client_id,
            items=items,
            status="queued",
            created_at=now,
            updated_at=now,
        )

    async def get(self, job_id: str, client_id: str | None = None) -> JobRecord | None:
        await self.cleanup_expired()
        async with self._sessionmaker() as session:
            row = await self._load_row(session, job_id, client_id=client_id)
            if row is None:
                return None
            return _record_from_row(row)

    async def snapshot(self, job_id: str, client_id: str = "default") -> JobView:
        job = await self.get(job_id, client_id=client_id)
        if job is None:
            raise job_not_found()
        return job.to_view()

    async def cancel(self, job_id: str, client_id: str = "default") -> JobView:
        await self.cleanup_expired()
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id, client_id=client_id)
                    if row is None:
                        raise job_not_found()
                    if row.status in _TERMINAL_JOB:
                        return _record_from_row(row).to_view()
                    now = _utcnow()
                    row.cancel_requested = True
                    running = False
                    for item in row.items:
                        if item.status == "pending":
                            item.status = "cancelled"
                        elif item.status == "running":
                            running = True
                    if not running:
                        row.status = "cancelled"
                    row.updated_at = now
                    return _record_from_row(row).to_view()

    async def delete(self, job_id: str, client_id: str = "default") -> None:
        await self.cleanup_expired()
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id, client_id=client_id)
                    if row is None:
                        raise job_not_found()
                    await session.delete(row)

    async def search(self, url: str, client_id: str = "default", limit: int = 20) -> list[JobView]:
        await self.cleanup_expired()
        cap = max(1, min(limit, 100))
        async with self._sessionmaker() as session:
            matching_ids = (
                select(JobItemRow.job_id).where(
                    or_(JobItemRow.requested_url == url, JobItemRow.result_url == url)
                )
            )
            stmt = (
                select(JobRow)
                .where(JobRow.client_id == client_id)
                .where(JobRow.id.in_(matching_ids))
                .order_by(JobRow.created_at.desc(), JobRow.id.desc())
                .limit(cap)
            )
            rows = (await session.scalars(stmt)).all()
            return [_record_from_row(row).to_view() for row in rows]

    async def start_job(self, job_id: str) -> JobRecord | None:
        """Mark queued job running, or apply a pending cancel. Worker API."""
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id)
                    if row is None:
                        return None
                    if row.status in _TERMINAL_JOB:
                        return None
                    now = _utcnow()
                    if row.cancel_requested:
                        for item in row.items:
                            if item.status == "pending":
                                item.status = "cancelled"
                        row.status = "cancelled"
                        row.updated_at = now
                        return None
                    row.status = "running"
                    row.updated_at = now
                    return _record_from_row(row)

    async def start_item(self, job_id: str, index: int) -> bool:
        """Mark item running. Returns False if it should be skipped."""
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id)
                    if row is None:
                        return False
                    item = _item_row(row, index)
                    if item is None:
                        return False
                    if row.cancel_requested or item.status != "pending":
                        if item.status == "pending":
                            item.status = "cancelled"
                            row.updated_at = _utcnow()
                        return False
                    item.status = "running"
                    row.updated_at = _utcnow()
                    return True

    async def succeed_item(self, job_id: str, index: int, result: ExtractResponse) -> None:
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id)
                    if row is None:
                        return
                    item = _item_row(row, index)
                    if item is None:
                        return
                    item.status = "succeeded"
                    item.result = result.model_dump()
                    item.error = None
                    item.result_url = result.url
                    row.updated_at = _utcnow()

    async def fail_item(self, job_id: str, index: int, error: ErrorBody) -> None:
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id)
                    if row is None:
                        return
                    item = _item_row(row, index)
                    if item is None:
                        return
                    item.status = "failed"
                    item.error = error.model_dump()
                    item.result = None
                    row.updated_at = _utcnow()

    async def finish_job(self, job_id: str) -> None:
        async with await self._lock_for(job_id):
            async with self._sessionmaker() as session:
                async with session.begin():
                    row = await self._load_row(session, job_id)
                    if row is None or row.status in _TERMINAL_JOB:
                        return
                    cancelled = row.cancel_requested or any(item.status == "cancelled" for item in row.items)
                    row.status = "cancelled" if cancelled else "completed"
                    row.updated_at = _utcnow()

    async def recover_incomplete(self) -> list[str]:
        """Reset interrupted running items and return ids that still need work."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    update(JobItemRow).where(JobItemRow.status == "running").values(status="pending")
                )
                await session.execute(
                    update(JobRow)
                    .where(JobRow.status == "running")
                    .values(status="queued", updated_at=_utcnow())
                )
                ids = (
                    await session.scalars(
                        select(JobRow.id).where(JobRow.status.in_(("queued", "running")))
                    )
                ).all()
                return list(ids)
