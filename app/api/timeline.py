"""
Timeline — chronological event view for a single repair job.

We don't persist explicit state transitions (no audit log table), so this
endpoint *reconstructs* a sensible timeline from the columns we do store:
  - RepairJob.created_at, updated_at, status, current_internal_branch, pr_url
  - PatchAttempt.created_at, internal_branch, ci_run_id, success
  - PatchAttempt.failure_type / error_summary / failing_file (per attempt)

Intermediate retrying↔repairing flips aren't recoverable without an audit
log. If that becomes important, add a `repair_job_events` table and have
the orchestrator append on every status change.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.models.db import PatchAttempt, RepairJob, async_session

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["jobs"])


def _ci_url(repo: str, run_id: int | None) -> str | None:
    return f"https://github.com/{repo}/actions/runs/{run_id}" if run_id else None


def _patch_lines(patch: str | None) -> int:
    if not patch:
        return 0
    return sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


@router.get(
    "/jobs/{job_id}/timeline",
    summary="Chronological events for a repair job (reconstructed)",
)
async def get_timeline(job_id: uuid.UUID):
    async with async_session() as session:
        job_result = await session.execute(select(RepairJob).filter_by(id=job_id))
        job = job_result.scalar_one_or_none()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        attempts_result = await session.execute(
            select(PatchAttempt)
            .filter_by(job_id=job_id)
            .order_by(PatchAttempt.attempt_number)
        )
        attempts = attempts_result.scalars().all()

    events: list[dict] = []

    # Job created
    events.append(
        {
            "at": job.created_at.isoformat(),
            "kind": "job_created",
            "details": {
                "repo": job.repo_name,
                "branch": job.branch_name,
                "sha": (job.commit_sha or "")[:8],
                "workflow_run_id": job.workflow_run_id,
                "ci_url": _ci_url(job.repo_name, job.workflow_run_id),
            },
        }
    )

    # One attempt block: pushed + (CI result if known)
    for a in attempts:
        events.append(
            {
                "at": a.created_at.isoformat(),
                "kind": "attempt_pushed",
                "details": {
                    "attempt": a.attempt_number,
                    "internal_branch": a.internal_branch,
                    "internal_commit_sha": (a.internal_commit_sha or "")[:8],
                    "failure_type": a.failure_type,
                    "error_summary": a.error_summary,
                    "failing_file": a.failing_file,
                    "failing_line": a.failing_line,
                    "patch_lines": _patch_lines(a.patch_diff),
                },
            }
        )
        if a.ci_run_id is not None:
            events.append(
                {
                    "at": a.created_at.isoformat(),  # approximate — webhook timestamp not stored
                    "kind": "ci_result",
                    "details": {
                        "attempt": a.attempt_number,
                        "ci_run_id": a.ci_run_id,
                        "conclusion": "success" if a.success else "failure",
                        "ci_url": _ci_url(job.repo_name, a.ci_run_id),
                    },
                }
            )

    # Terminal event
    if job.status == "pr_opened" and job.pr_url:
        events.append(
            {
                "at": job.updated_at.isoformat(),
                "kind": "pr_opened",
                "details": {
                    "pr_url": job.pr_url,
                    "clean_branch": job.final_clean_branch,
                },
            }
        )
    elif job.status in ("failed", "needs-human-review", "undiagnosable"):
        events.append(
            {
                "at": job.updated_at.isoformat(),
                "kind": "terminal",
                "details": {
                    "status": job.status,
                    "message": job.error_message,
                },
            }
        )

    return {
        "job_id": str(job.id),
        "status": job.status,
        "langfuse_trace_url": job.langfuse_trace_url,
        "events": events,
    }
