"""Single-consumer asyncio queue that runs each job through run_extract."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from app import config
from app.errors import INTERNAL, ArachneError
from app.jobs.store import JobItemRecord, JobRecord, JobStore
from app.models import ErrorBody
from app.service import run_extract

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("arachne")


class JobWorker:
    """One loop: dequeue a job, run its items with a per-job semaphore, mark terminal."""

    def __init__(self, app: FastAPI, store: JobStore) -> None:
        self.app = app
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="arachne-job-worker")

    def is_alive(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def _loop(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job worker failed job_id=%s", job_id)
            finally:
                self.queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        job = await self.store.start_job(job_id)
        if job is None:
            return

        logger.info("job start job_id=%s total=%s", job.job_id, job.total)
        sem = asyncio.Semaphore(config.JOB_CONCURRENCY)
        await asyncio.gather(*(self._run_item(job, item, sem) for item in job.items))
        await self.store.finish_job(job_id)
        done = await self.store.get(job_id)
        status = done.status if done is not None else "missing"
        logger.info("job done job_id=%s status=%s", job.job_id, status)

    async def _run_item(
        self,
        job: JobRecord,
        item: JobItemRecord,
        sem: asyncio.Semaphore,
    ) -> None:
        async with sem:
            started = await self.store.start_item(job.job_id, item.index)
            if not started:
                return
            try:
                result = await run_extract(
                    url=item.url,
                    client=self.app.state.http_client,
                    headers=item.headers,
                    cookies=item.cookies,
                    max_chars=item.max_chars,
                    session_id=item.session_id,
                    ua_strategy=item.ua_strategy,
                    render=item.render,
                    site_profile=item.site_profile,
                    profiles=self.app.state.profile_registry,
                    cache=self.app.state.extract_cache,
                    limiter=self.app.state.rate_limiter,
                    semaphore=self.app.state.extract_semaphore,
                    stats=self.app.state.stats,
                )
            except ArachneError as exc:
                await self.store.fail_item(
                    job.job_id,
                    item.index,
                    ErrorBody(code=exc.code, message=exc.message, detail=exc.detail),
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job item failed job_id=%s index=%s", job.job_id, item.index)
                await self.store.fail_item(
                    job.job_id,
                    item.index,
                    ErrorBody(code=INTERNAL, message="Internal server error", detail={}),
                )
                return
            await self.store.succeed_item(job.job_id, item.index, result)
