"""HTTP routes for POST /jobs, GET /jobs/{id}, POST /jobs/{id}/cancel."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app import config
from app.errors import bad_url
from app.jobs.models import CreateJobRequest, CreateJobResponse, JobView
from app.jobs.store import JobStore
from app.jobs.worker import JobWorker
from app.models import ErrorResponse

router = APIRouter(tags=["jobs"])

JOB_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "bad_url"},
    404: {"model": ErrorResponse, "description": "job_not_found"},
}


def _store(request: Request) -> JobStore:
    return request.app.state.job_store


def _worker(request: Request) -> JobWorker:
    return request.app.state.job_worker


@router.post(
    "/jobs",
    status_code=202,
    response_model=CreateJobResponse,
    responses=JOB_ERROR_RESPONSES,
)
async def create_job(request: Request, body: CreateJobRequest) -> CreateJobResponse:
    if len(body.items) > config.JOB_MAX_URLS:
        raise bad_url(
            f"items length must be between 1 and {config.JOB_MAX_URLS}",
            {"max_urls": config.JOB_MAX_URLS, "count": len(body.items)},
        )
    job = await _store(request).create(body)
    await _worker(request).enqueue(job.job_id)
    return CreateJobResponse(job_id=job.job_id, status="queued", total=job.total)


@router.get(
    "/jobs/{job_id}",
    response_model=JobView,
    response_model_exclude_none=True,
    responses=JOB_ERROR_RESPONSES,
)
async def get_job(request: Request, job_id: str) -> JobView:
    return await _store(request).snapshot(job_id)


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobView,
    response_model_exclude_none=True,
    responses=JOB_ERROR_RESPONSES,
)
async def cancel_job(request: Request, job_id: str) -> JobView:
    return await _store(request).cancel(job_id)
