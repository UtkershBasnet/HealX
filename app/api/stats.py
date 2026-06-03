"""
Stats — counts + top lists for the dashboard and operator tooling.

All queries are cheap aggregates over existing tables. No materialized views.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from sqlalchemy import case, desc, func, select

from app.models.db import PatchAttempt, RepairJob, async_session

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["stats"])

# Status buckets we expose. Anything in the DB not listed here is ignored
# (e.g. legacy pre-pivot rows we may not have cleaned up yet).
STATUS_BUCKETS = [
    "queued",
    "repairing",
    "retrying",
    "pr_opened",
    "failed",
    "needs-human-review",
    "undiagnosable",
]

# What counts as "terminal" for fix-rate purposes.
TERMINAL_STATUSES = ("pr_opened", "failed", "needs-human-review", "undiagnosable")


@router.get("/stats", summary="Aggregate stats across all repair jobs")
async def get_stats():
    async with async_session() as session:
        # ── totals by status ────────────────────────────────────────────
        counts_result = await session.execute(
            select(RepairJob.status, func.count(RepairJob.id))
            .group_by(RepairJob.status)
        )
        raw_counts = {row[0]: row[1] for row in counts_result.all()}
        totals = {bucket: int(raw_counts.get(bucket, 0)) for bucket in STATUS_BUCKETS}

        # ── fix rate ─────────────────────────────────────────────────────
        attempted = sum(totals[s] for s in TERMINAL_STATUSES)
        succeeded = totals["pr_opened"]
        fix_rate = (succeeded / attempted) if attempted else 0.0

        # ── mean attempts to PR ─────────────────────────────────────────
        mean_attempts_result = await session.execute(
            select(func.avg(RepairJob.retry_count + 1)).where(
                RepairJob.status == "pr_opened"
            )
        )
        mean_attempts = mean_attempts_result.scalar()
        mean_attempts = float(mean_attempts) if mean_attempts is not None else None

        # ── mean seconds queued→pr_opened ────────────────────────────────
        # We don't have a dedicated audit log, so we approximate using
        # updated_at - created_at on pr_opened rows.
        mean_seconds_result = await session.execute(
            select(
                func.avg(
                    func.extract("epoch", RepairJob.updated_at - RepairJob.created_at)
                )
            ).where(RepairJob.status == "pr_opened")
        )
        mean_seconds = mean_seconds_result.scalar()
        mean_seconds = float(mean_seconds) if mean_seconds is not None else None

        # ── top 5 repos by job count ─────────────────────────────────────
        top_repos_result = await session.execute(
            select(RepairJob.repo_name, func.count(RepairJob.id).label("c"))
            .group_by(RepairJob.repo_name)
            .order_by(desc("c"))
            .limit(5)
        )
        top_repos = [{"repo": r[0], "count": int(r[1])} for r in top_repos_result.all()]

        # ── failure-type distribution ────────────────────────────────────
        ft_result = await session.execute(
            select(RepairJob.failure_type, func.count(RepairJob.id).label("c"))
            .where(RepairJob.failure_type.isnot(None))
            .group_by(RepairJob.failure_type)
            .order_by(desc("c"))
        )
        failure_types = [
            {"type": r[0], "count": int(r[1])} for r in ft_result.all()
        ]

    return {
        "totals": totals,
        "fix_rate": {
            "attempted": attempted,
            "succeeded": succeeded,
            "rate": round(fix_rate, 4),
        },
        "mean_attempts_to_pr": round(mean_attempts, 2) if mean_attempts else None,
        "mean_seconds_to_pr": round(mean_seconds, 1) if mean_seconds else None,
        "top_repos": top_repos,
        "failure_types": failure_types,
    }
