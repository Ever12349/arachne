"""Arachne P5: URL → structured JSON extract, with persisted batch jobs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import Depends, FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import config
from app.cache import ResultCache
from app.db import create_db_engine, init_db, session_factory
from app.errors import ArachneError, http_status_for
from app.fetch import create_http_client
from app.jobs.router import router as jobs_router
from app.jobs.store import JobStore
from app.jobs.worker import JobWorker
from app.limits import FixedWindowRateLimiter, Stats, extract_semaphore
from app.logging_setup import setup_logging
from app.models import ErrorResponse, ExtractRequest, ExtractResponse, StatsResponse
from app.profiles.loader import ProfileRegistry
from app.service import run_extract

setup_logging()
logger = logging.getLogger("arachne")

ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "bad_url | session_invalid | profile_invalid"},
    403: {"model": ErrorResponse, "description": "challenge_detected"},
    422: {"model": ErrorResponse, "description": "unsupported_content | extract_empty | too_large"},
    429: {"model": ErrorResponse, "description": "rate_limited"},
    404: {"model": ErrorResponse, "description": "job_not_found"},
    500: {"model": ErrorResponse, "description": "internal"},
    501: {"model": ErrorResponse, "description": "render_unavailable"},
    502: {"model": ErrorResponse, "description": "fetch_failed | unauthorized_upstream | render_failed"},
    504: {"model": ErrorResponse, "description": "timeout"},
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = create_db_engine()
    await init_db(engine)
    app.state.db_engine = engine
    async with create_http_client() as client:
        app.state.http_client = client
        app.state.extract_cache = ResultCache(maxsize=config.CACHE_MAXSIZE, ttl=config.CACHE_TTL_SECONDS)
        app.state.rate_limiter = FixedWindowRateLimiter(qps=config.QPS)
        app.state.extract_semaphore = extract_semaphore(config.MAX_CONCURRENCY)
        app.state.stats = Stats()
        app.state.profile_registry = ProfileRegistry.from_config()
        store = JobStore(session_factory(engine))
        app.state.job_store = store
        worker = JobWorker(app, store)
        app.state.job_worker = worker
        await worker.start()
        for job_id in await store.recover_incomplete():
            await worker.enqueue(job_id)
        cleanup_task: asyncio.Task[None] | None = None
        if config.JOB_DB_TTL_SECONDS > 0:
            interval = float(min(60, max(1, config.JOB_DB_TTL_SECONDS)))
            cleanup_task = asyncio.create_task(store.run_cleanup_loop(interval), name="arachne-job-ttl")
        try:
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cleanup_task
            await worker.stop()
            await engine.dispose()


app = FastAPI(
    title="arachne",
    version="0.6.0",
    description="URL → structured JSON extract for AI agents, with SQLite-backed batch jobs.",
    lifespan=lifespan,
)


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


@app.exception_handler(ArachneError)
async def arachne_error_handler(_request: Request, exc: ArachneError) -> JSONResponse:
    return JSONResponse(
        status_code=http_status_for(exc.code),
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "detail": exc.detail,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "bad_url",
                "message": "Invalid request",
                "detail": {"errors": jsonable_encoder(exc.errors())},
            }
        },
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/stats", response_model=StatsResponse)
async def stats(request: Request) -> StatsResponse:
    return StatsResponse.model_validate(request.app.state.stats.snapshot())


async def _run_extract(
    request: Request,
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    max_chars: int | None = None,
    session_id: str | None = None,
    ua_strategy: str = "default",
    render: bool = False,
    site_profile: str | None = None,
) -> ExtractResponse:
    state = request.app.state
    return await run_extract(
        url=url,
        client=client,
        headers=headers,
        cookies=cookies,
        max_chars=max_chars,
        session_id=session_id,
        ua_strategy=ua_strategy,
        render=render,
        site_profile=site_profile,
        profiles=state.profile_registry,
        cache=state.extract_cache,
        limiter=state.rate_limiter,
        semaphore=state.extract_semaphore,
        stats=state.stats,
    )


@app.get("/extract", response_model=ExtractResponse, responses=ERROR_RESPONSES)
async def extract_get(
    request: Request,
    url: str = Query(..., description="http(s) page URL to fetch and extract"),
    max_chars: int | None = Query(
        default=None,
        description="Optional main_text cap; clamped to [1, MAIN_TEXT_MAX_CHARS]",
    ),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ExtractResponse:
    return await _run_extract(request, url, client, max_chars=max_chars)


@app.post("/extract", response_model=ExtractResponse, responses=ERROR_RESPONSES)
async def extract_post(
    request: Request,
    body: ExtractRequest,
    client: httpx.AsyncClient = Depends(get_http_client),
) -> ExtractResponse:
    return await _run_extract(
        request,
        body.url,
        client,
        headers=body.headers,
        cookies=body.cookies,
        max_chars=body.max_chars,
        session_id=body.session_id,
        ua_strategy=body.ua_strategy,
        render=body.render,
        site_profile=body.site_profile,
    )


app.include_router(jobs_router)
