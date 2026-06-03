"""
Jobs API — paginated list, single job, attempt history, manual retry.

Filters on the list endpoint:
    /jobs?status=pr_opened&repo=owner/repo&failure_type=TestFailure&since=2026-05-01

All filters are optional and composable. Indexed columns (status, repo_name)
make the common cases cheap. failure_type is unindexed but fine at this scale.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Query
from redis import Redis
from rq import Queue
from sqlalchemy import desc, func, select

from app.config import settings
from app.models.db import PatchAttempt, RepairJob, async_session
from app.models.schemas import (
    PatchAttemptResponse,
    RepairJobListResponse,
    RepairJobResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["jobs"])


def _github_run_url(repo: str, run_id: int | None) -> str | None:
    if not run_id:
        return None
    return f"https://github.com/{repo}/actions/runs/{run_id}"


@router.get(
    "/jobs",
    response_model=RepairJobListResponse,
    summary="List recent jobs (filterable)",
)
async def list_jobs(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    status: str | None = Query(None, description="Exact match on RepairJob.status"),
    repo: str | None = Query(None, description="Exact match on RepairJob.repo_name"),
    failure_type: str | None = Query(None, description="Exact match on RepairJob.failure_type"),
    since: datetime | None = Query(None, description="created_at >= since (ISO-8601)"),
):
    offset = (page - 1) * per_page

    async with async_session() as session:
        base = select(RepairJob)
        count_base = select(func.count(RepairJob.id))

        if status:
            base = base.where(RepairJob.status == status)
            count_base = count_base.where(RepairJob.status == status)
        if repo:
            base = base.where(RepairJob.repo_name == repo)
            count_base = count_base.where(RepairJob.repo_name == repo)
        if failure_type:
            base = base.where(RepairJob.failure_type == failure_type)
            count_base = count_base.where(RepairJob.failure_type == failure_type)
        if since:
            base = base.where(RepairJob.created_at >= since)
            count_base = count_base.where(RepairJob.created_at >= since)

        count_result = await session.execute(count_base)
        total = count_result.scalar() or 0

        result = await session.execute(
            base.order_by(desc(RepairJob.created_at)).offset(offset).limit(per_page)
        )
        jobs = result.scalars().all()

        return RepairJobListResponse(
            jobs=[RepairJobResponse.model_validate(j) for j in jobs],
            total=total,
            page=page,
            per_page=per_page,
        )


@router.get("/jobs/{job_id}", response_model=RepairJobResponse, summary="Get job status")
async def get_job(job_id: uuid.UUID):
    async with async_session() as session:
        result = await session.execute(select(RepairJob).filter_by(id=job_id))
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return RepairJobResponse.model_validate(job)


@router.get(
    "/jobs/{job_id}/attempts",
    summary="Get patch attempts for a job (enriched with GitHub + Langfuse links)",
)
async def get_job_attempts(job_id: uuid.UUID):
    """
    Returns the per-attempt list. Each attempt is enriched with:
    - github_run_url: deep link to the GitHub Actions run for that attempt's CI
    - langfuse_trace_url: inherited from the parent job (one trace per job)
    """
    async with async_session() as session:
        job_result = await session.execute(select(RepairJob).filter_by(id=job_id))
        job = job_result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        result = await session.execute(
            select(PatchAttempt)
            .filter_by(job_id=job_id)
            .order_by(PatchAttempt.attempt_number)
        )
        attempts = result.scalars().all()

    return [
        {
            **PatchAttemptResponse.model_validate(a).model_dump(mode="json"),
            "github_run_url": _github_run_url(job.repo_name, a.ci_run_id),
            "langfuse_trace_url": job.langfuse_trace_url,
        }
        for a in attempts
    ]


@router.post(
    "/jobs/{job_id}/retry",
    response_model=RepairJobResponse,
    summary="Manually retry a job",
)
async def retry_job(job_id: uuid.UUID):
    """
    Re-trigger the initial-failure path for a terminal-state job. Resets
    retry_count and clears the internal branch tracking so the next attempt
    starts fresh from the original failing SHA.
    """
    async with async_session() as session:
        result = await session.execute(select(RepairJob).filter_by(id=job_id))
        job = result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        if job.status not in ("failed", "needs-human-review", "undiagnosable"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot retry job with status '{job.status}'",
            )

        job.status = "queued"
        job.retry_count = 0
        job.current_internal_branch = None
        job.error_message = None
        await session.commit()
        await session.refresh(job)

    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("healx-jobs", connection=redis_conn)
    queue.enqueue(
        "app.pipeline.orchestrator.handle_initial_failure",
        str(job_id),
        job_timeout=600,
        result_ttl=3600,
    )

    logger.info("job_retried", job_id=str(job_id))
    return RepairJobResponse.model_validate(job)
