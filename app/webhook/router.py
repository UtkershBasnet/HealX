"""
HealX Webhook Router — GitHub Actions webhook endpoint.

Two interesting event shapes share the same webhook:

1. workflow_run.completed (conclusion=failure) on a DEVELOPER branch:
       → create a fresh RepairJob (idempotent on workflow_run_id)
       → enqueue handle_initial_failure

2. workflow_run.completed on a healx/internal/run-* branch:
       → look up the existing RepairJob by current_internal_branch
       → enqueue handle_internal_branch_completion with the CI conclusion

All other shapes are ignored.
"""

import json
import urllib.parse
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from redis import Redis
from rq import Queue
from sqlalchemy import select

from app.config import settings
from app.models.db import RepairJob, async_session
from app.models.schemas import WebhookAcceptedResponse
from app.webhook.validator import validate_github_signature

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/webhook", tags=["webhook"])

INTERNAL_BRANCH_PREFIX = "healx/internal/run-"

# ─── Redis / RQ Setup ───

_redis_conn: Redis | None = None
_queue: Queue | None = None


def get_redis() -> Redis:
    global _redis_conn
    if _redis_conn is None:
        _redis_conn = Redis.from_url(settings.redis_url)
    return _redis_conn


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue("healx-jobs", connection=get_redis())
    return _queue


# ─── Webhook Endpoint ───


@router.post(
    "/github",
    status_code=202,
    summary="Receive GitHub Actions webhook",
    description="Routes workflow_run.completed events to the right orchestrator entry point.",
)
async def handle_github_webhook(
    request: Request,
    body: bytes = Depends(validate_github_signature),
):
    event_type = request.headers.get("X-GitHub-Event", "")
    content_type = request.headers.get("Content-Type", "").lower()

    payload = _parse_github_body(body, content_type)

    logger.info(
        "webhook_received",
        event_type=event_type,
        action=payload.get("action"),
    )

    # GitHub fires `ping` once when the webhook is created. Acknowledge it
    # so the UI shows a green check without falling through to ignored.
    if event_type == "ping":
        return Response(
            content=json.dumps({"pong": True, "zen": payload.get("zen", "")}),
            status_code=200,
            media_type="application/json",
        )

    if event_type != "workflow_run":
        return _ignored("Not a workflow_run event")

    action = payload.get("action")
    if action != "completed":
        return _ignored(f"Skipped: action={action}")

    workflow_run = payload.get("workflow_run", {})
    conclusion = workflow_run.get("conclusion")
    repo_name = payload["repository"]["full_name"]
    branch_name = workflow_run.get("head_branch", "unknown") or "unknown"
    commit_sha = workflow_run.get("head_sha", "")
    run_id = workflow_run.get("id")

    # ─── Branch on whether this is an internal-branch event ───
    if branch_name.startswith(INTERNAL_BRANCH_PREFIX):
        return await _handle_internal_branch_event(
            repo_name=repo_name,
            branch_name=branch_name,
            conclusion=conclusion or "failure",
            run_id=run_id,
        )

    # ─── Developer-branch event: only act on failures ───
    if conclusion != "failure":
        return _ignored(f"Skipped developer-branch event: conclusion={conclusion}")

    return await _handle_developer_failure_event(
        repo_name=repo_name,
        branch_name=branch_name,
        commit_sha=commit_sha,
        run_id=run_id,
    )


# ─── Developer-branch failure: create job + enqueue initial repair ───


async def _handle_developer_failure_event(
    repo_name: str,
    branch_name: str,
    commit_sha: str,
    run_id: int | None,
):
    logger.info(
        "ci_failure_detected",
        repo=repo_name,
        branch=branch_name,
        sha=commit_sha[:8] if commit_sha else "",
        run_id=run_id,
    )

    # ─── Idempotency on workflow_run_id ─────────────────────────────
    # Duplicate webhook deliveries from GitHub must not double-fire repair.
    # We use both a Redis short-TTL lock (fast path) AND a DB unique
    # constraint (durable path).
    redis = get_redis()
    lock_key = f"healx:lock:workflow_run:{run_id}"

    if run_id is not None and not redis.set(lock_key, "locked", ex=600, nx=True):
        return _ignored("Duplicate webhook delivery for this workflow_run_id")

    job_id = uuid.uuid4()
    try:
        async with async_session() as session:
            # Reject duplicates via the unique constraint on workflow_run_id.
            existing = await session.execute(
                select(RepairJob).filter_by(workflow_run_id=run_id)
            )
            if existing.scalar_one_or_none() is not None:
                logger.info("job_already_exists_for_run", run_id=run_id)
                if run_id is not None:
                    redis.delete(lock_key)
                return _ignored("Job already exists for this workflow_run_id")

            job = RepairJob(
                id=job_id,
                repo_name=repo_name,
                branch_name=branch_name,
                commit_sha=commit_sha,
                workflow_run_id=run_id,
                status="queued",
            )
            session.add(job)
            await session.commit()

        logger.info("job_created", job_id=str(job_id), repo=repo_name, branch=branch_name)

        queue = get_queue()
        queue.enqueue(
            "app.pipeline.orchestrator.handle_initial_failure",
            str(job_id),
            job_timeout=600,
            result_ttl=3600,
        )
        logger.info("initial_failure_enqueued", job_id=str(job_id))

    except Exception as e:
        if run_id is not None:
            redis.delete(lock_key)
        logger.exception(
            "job_creation_failed", repo=repo_name, branch=branch_name, error=str(e)
        )
        raise

    return WebhookAcceptedResponse(
        job_id=job_id,
        message=f"Repair job enqueued for {repo_name}@{branch_name}",
    )


# ─── Internal-branch completion: route to retry/finalize ───


async def _handle_internal_branch_event(
    repo_name: str,
    branch_name: str,
    conclusion: str,
    run_id: int | None,
):
    logger.info(
        "internal_branch_event",
        repo=repo_name,
        branch=branch_name,
        conclusion=conclusion,
        run_id=run_id,
    )

    async with async_session() as session:
        result = await session.execute(
            select(RepairJob).filter_by(
                repo_name=repo_name,
                current_internal_branch=branch_name,
            )
        )
        job = result.scalar_one_or_none()

    if job is None:
        logger.warning(
            "internal_branch_unknown",
            repo=repo_name,
            branch=branch_name,
        )
        return _ignored("No active repair job for this internal branch")

    # Per-internal-run idempotency: ignore duplicate webhook deliveries.
    redis = get_redis()
    if run_id is not None:
        lock_key = f"healx:lock:internal_run:{run_id}"
        if not redis.set(lock_key, "locked", ex=1800, nx=True):
            return _ignored("Duplicate webhook delivery for internal-branch run")

    queue = get_queue()
    queue.enqueue(
        "app.pipeline.orchestrator.handle_internal_branch_completion",
        str(job.id),
        conclusion,
        run_id or 0,
        job_timeout=600,
        result_ttl=3600,
    )
    logger.info(
        "internal_completion_enqueued",
        job_id=str(job.id),
        conclusion=conclusion,
        run_id=run_id,
    )

    return WebhookAcceptedResponse(
        job_id=job.id,
        message=f"Internal-branch completion routed for {branch_name}",
    )


# ─── Helpers ───


def _parse_github_body(body: bytes, content_type: str) -> dict:
    """
    GitHub webhooks default to application/x-www-form-urlencoded, where the
    JSON payload sits in a `payload=` field. application/json is the better
    setting but we accept both so a misconfigured webhook still works.
    """
    raw = body.decode("utf-8", errors="replace")

    if "application/x-www-form-urlencoded" in content_type:
        parsed = urllib.parse.parse_qs(raw)
        payload_field = parsed.get("payload", [""])[0]
        if not payload_field:
            raise HTTPException(
                status_code=400,
                detail="Form-encoded webhook missing 'payload' field",
            )
        raw = payload_field

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("webhook_body_unparseable", error=str(e), preview=raw[:200])
        raise HTTPException(
            status_code=400,
            detail=f"Webhook body is not valid JSON: {e}",
        )


def _ignored(reason: str) -> Response:
    return Response(
        content=json.dumps({"ignored": True, "reason": reason}),
        status_code=200,
        media_type="application/json",
    )
