"""HTTP routes for jobs: create, get, search, cancel, delete."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response

from app import config
from app.errors import bad_url
from app.jobs.models import CreateJobRequest, CreateJobResponse, JobSearchResponse, JobView
from app.jobs.store import JobStore
from app.jobs.worker import JobWorker
from app.models import ErrorResponse

router = APIRouter(tags=["jobs"])

CLIENT_HEADER = "X-Arachne-Client"
DEFAULT_CLIENT_ID = "default"

JOB_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse, "description": "bad_url"},
    404: {"model": ErrorResponse, "description": "job_not_found"},
}


def request_client_id(request: Request) -> str:
    raw = request.headers.get(CLIENT_HEADER)
    if raw is None:
        return DEFAULT_CLIENT_ID
    stripped = raw.strip()
    return stripped if stripped else DEFAULT_CLIENT_ID


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
    job = await _store(request).create(body, client_id=request_client_id(request))
    await _worker(request).enqueue(job.job_id)
    return CreateJobResponse(job_id=job.job_id, status="queued", total=job.total)


@router.get(
    "/jobs/search",
    response_model=JobSearchResponse,
    response_model_exclude_none=True,
    responses=JOB_ERROR_RESPONSES,
)
async def search_jobs(
    request: Request,
    url: str = Query(..., description="Exact match on item requested_url or result final url"),
    limit: int = Query(default=20, ge=1, le=100),
) -> JobSearchResponse:
    jobs = await _store(request).search(url=url, client_id=request_client_id(request), limit=limit)
    return JobSearchResponse(jobs=jobs)


@router.get(
    "/jobs/{job_id}",
    response_model=JobView,
    response_model_exclude_none=True,
    responses=JOB_ERROR_RESPONSES,
)
async def get_job(request: Request, job_id: str) -> JobView:
    return await _store(request).snapshot(job_id, client_id=request_client_id(request))


@router.post(
    "/jobs/{job_id}/cancel",
    response_model=JobView,
    response_model_exclude_none=True,
    responses=JOB_ERROR_RESPONSES,
)
async def cancel_job(request: Request, job_id: str) -> JobView:
    return await _store(request).cancel(job_id, client_id=request_client_id(request))


@router.delete(
    "/jobs/{job_id}",
    status_code=204,
    responses=JOB_ERROR_RESPONSES,
)
async def delete_job(request: Request, job_id: str) -> Response:
    await _store(request).delete(job_id, client_id=request_client_id(request))
    return Response(status_code=204)
