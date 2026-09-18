"""In-memory TTL job store. Process restart drops all jobs."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from cachetools import TTLCache

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
from app.models import ErrorBody, ErrorResponse, ExtractResponse

T = TypeVar("T")


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
    status: JobStatus = "queued"
    cancel_requested: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

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


class JobStore:
    """TTLCache of live jobs. Expired or evicted ids are job_not_found."""

    def __init__(
        self,
        maxsize: int | None = None,
        ttl: float | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        size = config.JOB_MAX_STORED if maxsize is None else maxsize
        lifetime = config.JOB_TTL_SECONDS if ttl is None else ttl
        if timer is None:
            self._cache: TTLCache[str, JobRecord] = TTLCache(maxsize=size, ttl=lifetime)
        else:
            self._cache = TTLCache(maxsize=size, ttl=lifetime, timer=timer)
        self._lock = asyncio.Lock()

    async def create(self, body: CreateJobRequest) -> JobRecord:
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            items=[resolve_item(body.defaults, item, index) for index, item in enumerate(body.items)],
        )
        async with self._lock:
            self._cache[job.job_id] = job
        return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._cache.get(job_id)

    async def snapshot(self, job_id: str) -> JobView:
        job = await self.get(job_id)
        if job is None:
            raise job_not_found()
        async with job.lock:
            return job.to_view()

    async def cancel(self, job_id: str) -> JobView:
        job = await self.get(job_id)
        if job is None:
            raise job_not_found()
        async with job.lock:
            if job.status in {"completed", "cancelled"}:
                return job.to_view()
            job.cancel_requested = True
            running = False
            for item in job.items:
                if item.status == "pending":
                    item.status = "cancelled"
                elif item.status == "running":
                    running = True
            if not running:
                job.status = "cancelled"
            return job.to_view()

    def __len__(self) -> int:
        return len(self._cache)
