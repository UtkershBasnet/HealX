"""
HealX Pipeline Orchestrator — Webhook-driven repair lifecycle.

Two RQ entry points, each fired by an incoming GitHub webhook:

    handle_initial_failure(job_id)
        ↓ triggered by:  workflow_run.completed (conclusion=failure)
                         on a developer branch.
        - fetch CI logs for the failing run
        - run the agent graph (triage → repair) once
        - push the patch to healx/internal/run-{job_id}
        - mark job as repairing; wait for GitHub to run CI on the push.

    handle_internal_branch_completion(job_id, ci_conclusion, new_run_id)
        ↓ triggered by:  workflow_run.completed on a healx/internal/* branch.
        - success  → open clean PR, mark pr_opened
        - failure + retries left   → fetch new logs, re-run graph with
          previous patch + new logs, push next attempt to same internal
          branch, mark retrying
        - failure + retries exhausted → mark failed, post escalation comment

Verification is GitHub Actions; nothing runs locally except git.
"""

import asyncio
import structlog
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.observability.langfuse_client import trace_url_for
from app.pipeline.git_ops import (
    PatchApplyError,
    open_clean_fix_pr,
    post_escalation_comment,
    push_patch_to_internal_branch,
)
from app.pipeline.github_client import github_client

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3


# ─── Sync DB Session (RQ workers are synchronous) ───

_sync_engine = create_engine(
    settings.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://"),
    echo=(not settings.is_production),
)
SyncSession = sessionmaker(_sync_engine)


# ─── Entry point #1: initial failure on a developer branch ───


def handle_initial_failure(job_id: str) -> dict:
    """
    First attempt on a fresh failure.

    Job was just created from a workflow_run.failed webhook on a developer
    branch. Fetch the logs, run the graph once, push the patch — then yield
    control. The webhook for the resulting internal-branch CI run will fire
    handle_internal_branch_completion.
    """
    from app.models.db import RepairJob, PatchAttempt

    session = SyncSession()
    try:
        job = session.query(RepairJob).filter_by(id=job_id).first()
        if not job:
            logger.error("job_not_found", job_id=job_id)
            return {"error": "Job not found"}

        logger.info(
            "initial_failure_start",
            job_id=job_id,
            repo=job.repo_name,
            branch=job.branch_name,
            sha=job.commit_sha[:8],
        )

        job.status = "repairing"
        # Stamp the trace URL once on first attempt — same trace covers all retries.
        if not job.langfuse_trace_url:
            job.langfuse_trace_url = trace_url_for(job_id)
        session.commit()

        ci_logs = _fetch_logs_or_placeholder(job.repo_name, job.workflow_run_id, job_id)

        final_state = _invoke_repair_graph(
            job=job,
            ci_logs=ci_logs,
            attempt_number=1,
            previous_patch=None,
        )

        return _post_graph_dispatch(
            session=session,
            job=job,
            final_state=final_state,
            attempt_number=1,
            ci_logs_for_attempt=ci_logs,
            ci_run_id=job.workflow_run_id,
        )

    except Exception as e:
        logger.exception("handle_initial_failure_failed", job_id=job_id, error=str(e))
        _mark_job_error(session, job_id, str(e))
        return {"error": str(e)}
    finally:
        session.close()


# ─── Entry point #2: completion of CI on a healx/internal/* branch ───


def handle_internal_branch_completion(
    job_id: str,
    ci_conclusion: str,
    new_run_id: int,
) -> dict:
    """
    Webhook fired when CI completes on the internal branch.

    Args:
        job_id: The repair job whose internal branch this is.
        ci_conclusion: "success" | "failure" | other (treated as failure).
        new_run_id: workflow_run_id of the CI run that just finished.
    """
    from app.models.db import RepairJob, PatchAttempt

    session = SyncSession()
    try:
        job = session.query(RepairJob).filter_by(id=job_id).first()
        if not job:
            logger.error("job_not_found", job_id=job_id)
            return {"error": "Job not found"}

        logger.info(
            "internal_completion",
            job_id=job_id,
            ci_conclusion=ci_conclusion,
            new_run_id=new_run_id,
            current_retries=job.retry_count,
        )

        # ─── Green CI → open clean PR ───
        if ci_conclusion == "success":
            return _finalize_success(session, job, new_run_id)

        # ─── Red CI ───
        if job.retry_count >= MAX_RETRIES:
            return _finalize_failure(session, job)

        # ─── Red CI with retries left → re-run graph with new logs ───
        new_logs = _fetch_logs_or_placeholder(job.repo_name, new_run_id, job_id)

        # The previous patch is the latest PatchAttempt for this job.
        last_attempt = (
            session.query(PatchAttempt)
            .filter_by(job_id=job_id)
            .order_by(PatchAttempt.attempt_number.desc())
            .first()
        )
        previous_patch = last_attempt.patch_diff if last_attempt else None

        # Stamp the failed run's CI output on the last attempt for traceability.
        if last_attempt is not None:
            last_attempt.ci_run_id = new_run_id
            last_attempt.ci_output = new_logs[:50000]
            last_attempt.success = False
            session.commit()

        job.status = "retrying"
        job.retry_count += 1
        session.commit()

        next_attempt_number = job.retry_count + 1  # retry_count tracks attempts after the first
        final_state = _invoke_repair_graph(
            job=job,
            ci_logs=new_logs,
            attempt_number=next_attempt_number,
            previous_patch=previous_patch,
        )

        return _post_graph_dispatch(
            session=session,
            job=job,
            final_state=final_state,
            attempt_number=next_attempt_number,
            ci_logs_for_attempt=new_logs,
            ci_run_id=new_run_id,
        )

    except Exception as e:
        logger.exception(
            "handle_internal_branch_completion_failed", job_id=job_id, error=str(e)
        )
        _mark_job_error(session, job_id, str(e))
        return {"error": str(e)}
    finally:
        session.close()


# ─── Shared internals ───


def _invoke_repair_graph(
    job,
    ci_logs: str,
    attempt_number: int,
    previous_patch: str | None,
) -> dict:
    """Run the LangGraph triage→repair pipeline once."""
    from app.agents.graph import repair_graph

    initial_state = {
        "job_id": str(job.id),
        "repo": job.repo_name,
        "commit_sha": job.commit_sha,
        "branch": job.branch_name,
        "ci_logs": ci_logs,
        "attempt_number": attempt_number,
        "previous_patch": previous_patch,
        "failure_type": "",
        "failing_file": None,
        "failing_line": None,
        "error_summary": "",
        "relevant_files": [],
        "error_snippet": "",
        "patch_diff": None,
        "can_fix": False,
        "repair_reason": None,
        "status": "running",
        "total_tokens": 0,
    }

    logger.info("agent_graph_starting", job_id=str(job.id), attempt=attempt_number)
    final_state = repair_graph.invoke(initial_state)
    logger.info(
        "agent_graph_completed",
        job_id=str(job.id),
        attempt=attempt_number,
        status=final_state.get("status"),
        total_tokens=final_state.get("total_tokens", 0),
    )
    return final_state


def _post_graph_dispatch(
    session,
    job,
    final_state: dict,
    attempt_number: int,
    ci_logs_for_attempt: str,
    ci_run_id: int | None,
) -> dict:
    """Common path after the graph runs: persist attempt, push patch, or escalate."""
    from app.models.db import PatchAttempt

    status = final_state.get("status")

    # ─── Graph dropped out before producing a patch ───
    if status in ("undiagnosable", "cannot_fix"):
        job.status = "undiagnosable" if status == "undiagnosable" else "needs-human-review"
        job.failure_type = final_state.get("failure_type")
        job.error_message = (
            final_state.get("repair_reason")
            or final_state.get("error_summary")
            or "Agent graph could not produce a patch"
        )[:1000]
        session.commit()

        try:
            github_client.post_comment(
                repo=job.repo_name,
                sha=job.commit_sha,
                body=_short_dropout_comment(job.status, final_state),
            )
        except Exception:
            logger.warning("dropout_comment_post_failed", job_id=str(job.id))

        return {"job_id": str(job.id), "status": job.status}

    # ─── Patch produced → push to internal branch ───
    patch_diff = final_state["patch_diff"]
    try:
        push = push_patch_to_internal_branch(
            repo=job.repo_name,
            base_sha=job.commit_sha,
            job_id=str(job.id),
            patch_diff=patch_diff,
            attempt_number=attempt_number,
            error_summary=final_state.get("error_summary", ""),
            failure_type=final_state.get("failure_type", "Unknown"),
            failing_file=final_state.get("failing_file"),
        )
    except PatchApplyError as e:
        return _record_apply_failure(
            session=session,
            job=job,
            final_state=final_state,
            attempt_number=attempt_number,
            patch_diff=patch_diff,
            apply_error=e,
        )

    # Record this attempt; ci_output is filled in when CI completes.
    # The triage snapshot is captured per-attempt so the escalation comment
    # can show how each retry diagnosed the failure (which can differ once
    # a prior patch reshapes the CI logs).
    attempt = PatchAttempt(
        job_id=job.id,
        attempt_number=attempt_number,
        success=False,  # flipped to True later if CI passes
        patch_diff=patch_diff,
        model_used="claude-opus-4-7",
        token_count=final_state.get("total_tokens", 0),
        failure_type=final_state.get("failure_type"),
        error_summary=final_state.get("error_summary"),
        failing_file=final_state.get("failing_file"),
        failing_line=final_state.get("failing_line"),
        internal_branch=push.branch_name,
        internal_commit_sha=push.commit_sha,
        ci_run_id=None,  # set when the resulting workflow_run.completed arrives
        ci_output=None,
    )
    session.add(attempt)

    # Persist latest triage understanding so finalize/escalate paths can
    # reach it across webhook-invocation boundaries.
    job.current_internal_branch = push.branch_name
    job.failure_type = final_state.get("failure_type")
    job.error_summary = final_state.get("error_summary")
    job.failing_file = final_state.get("failing_file")
    job.failing_line = final_state.get("failing_line")
    if job.status != "retrying":
        job.status = "repairing"
    session.commit()

    return {
        "job_id": str(job.id),
        "status": job.status,
        "internal_branch": push.branch_name,
        "attempt": attempt_number,
    }


def _finalize_success(session, job, new_run_id: int) -> dict:
    """Internal CI is green — squash into clean branch and open the PR."""
    from app.models.db import PatchAttempt

    # Mark the latest attempt as successful with the passing CI run id.
    last_attempt = (
        session.query(PatchAttempt)
        .filter_by(job_id=job.id)
        .order_by(PatchAttempt.attempt_number.desc())
        .first()
    )
    if last_attempt is not None:
        last_attempt.success = True
        last_attempt.ci_run_id = new_run_id
        session.commit()

    pr_result = open_clean_fix_pr(
        repo=job.repo_name,
        internal_branch=job.current_internal_branch,
        base_branch=job.branch_name,
        base_sha=job.commit_sha,
        job_id=str(job.id),
        error_summary=job.error_summary or "",
        failure_type=job.failure_type or "Unknown",
        failing_file=job.failing_file,
        failing_line=job.failing_line,
        retry_count=job.retry_count + 1,
    )

    job.status = "pr_opened"
    job.pr_url = pr_result.pr_url
    job.final_clean_branch = pr_result.branch_name
    session.commit()

    try:
        github_client.post_comment(
            repo=job.repo_name,
            sha=job.commit_sha,
            body=(
                "## ✅ HealX — Fix Verified by CI\n\n"
                f"**Failure type:** `{job.failure_type or 'Unknown'}`\n"
                f"**Attempts:** {job.retry_count + 1}\n\n"
                f"**Pull Request:** {pr_result.pr_url}"
            ),
        )
    except Exception:
        logger.warning("success_comment_post_failed", job_id=str(job.id))

    return {"job_id": str(job.id), "status": "pr_opened", "pr_url": pr_result.pr_url}


def _finalize_failure(session, job) -> dict:
    """Max retries reached. Mark failed, post escalation, no PR."""
    from app.models.db import PatchAttempt

    job.status = "failed"
    job.error_message = (
        f"All {MAX_RETRIES} repair attempts failed CI. Manual review required."
    )
    session.commit()

    attempts = (
        session.query(PatchAttempt)
        .filter_by(job_id=job.id)
        .order_by(PatchAttempt.attempt_number.asc())
        .all()
    )
    attempts_dicts = [
        {
            "patch_diff": a.patch_diff,
            "ci_output": a.ci_output,
            "ci_run_id": a.ci_run_id,
            "failure_type": a.failure_type,
            "error_summary": a.error_summary,
            "failing_file": a.failing_file,
            "failing_line": a.failing_line,
        }
        for a in attempts
    ]

    try:
        post_escalation_comment(
            repo=job.repo_name,
            commit_sha=job.commit_sha,
            job_id=str(job.id),
            error_summary=job.error_summary or job.error_message or "",
            failure_type=job.failure_type or "Unknown",
            attempts=attempts_dicts,
            max_retries=MAX_RETRIES,
        )
    except Exception:
        logger.warning("escalation_comment_post_failed", job_id=str(job.id))

    return {"job_id": str(job.id), "status": "failed"}


def _record_apply_failure(
    session,
    job,
    final_state: dict,
    attempt_number: int,
    patch_diff: str,
    apply_error: PatchApplyError,
) -> dict:
    """
    Patch apply rejected the agent's diff before it could be pushed to GitHub.

    Record the attempt (with the apply stderr in ci_output for forensics),
    mark the job needs-human-review, and post a developer-facing comment.
    Apply failures do not count against the CI retry budget — there is no
    CI run to count.
    """
    from app.models.db import PatchAttempt

    logger.warning(
        "patch_apply_failed",
        job_id=str(job.id),
        attempt=attempt_number,
        stderr_preview=apply_error.stderr[:300],
    )

    attempt = PatchAttempt(
        job_id=job.id,
        attempt_number=attempt_number,
        success=False,
        patch_diff=patch_diff,
        model_used="claude-opus-4-7",
        token_count=final_state.get("total_tokens", 0),
        failure_type=final_state.get("failure_type"),
        error_summary=final_state.get("error_summary"),
        failing_file=final_state.get("failing_file"),
        failing_line=final_state.get("failing_line"),
        internal_branch=None,
        internal_commit_sha=None,
        ci_run_id=None,
        ci_output=f"[Local patch apply failed before CI could run]\n\n{apply_error.stderr}",
    )
    session.add(attempt)

    job.status = "needs-human-review"
    job.failure_type = final_state.get("failure_type")
    job.error_summary = final_state.get("error_summary")
    job.failing_file = final_state.get("failing_file")
    job.failing_line = final_state.get("failing_line")
    job.error_message = (
        f"Generated patch did not apply cleanly to {job.commit_sha[:8]}. "
        f"git apply stderr: {apply_error.stderr[:400]}"
    )
    session.commit()

    try:
        github_client.post_comment(
            repo=job.repo_name,
            sha=job.commit_sha,
            body=(
                "## ⚠️ HealX — Generated patch did not apply\n\n"
                f"The repair agent proposed a fix for `{final_state.get('failure_type', 'Unknown')}`, "
                f"but `git apply` rejected the diff against `{job.commit_sha[:8]}`. "
                "This usually means the agent's view of the source drifted from the real tree "
                "(stale line numbers, missing context, or a file path that doesn't exist at that SHA).\n\n"
                f"**Triage summary:** {final_state.get('error_summary', 'N/A')}\n\n"
                "<details><summary>git apply stderr</summary>\n\n"
                f"```\n{apply_error.stderr[:3000]}\n```\n</details>\n\n"
                "<details><summary>Proposed patch</summary>\n\n"
                f"```diff\n{apply_error.patch_preview}\n```\n</details>\n\n"
                "Manual review recommended."
            ),
        )
    except Exception:
        logger.warning("apply_failure_comment_post_failed", job_id=str(job.id))

    return {
        "job_id": str(job.id),
        "status": "needs-human-review",
        "reason": "patch_apply_failed",
    }


def _fetch_logs_or_placeholder(repo: str, run_id: int | None, job_id: str) -> str:
    if not run_id:
        return "[No workflow_run_id available]"
    try:
        return asyncio.run(github_client.get_workflow_logs(repo, run_id))
    except Exception as e:
        logger.warning("ci_logs_fetch_failed", job_id=job_id, error=str(e))
        return f"[Failed to fetch CI logs: {e}]"


def _mark_job_error(session, job_id: str, message: str) -> None:
    from app.models.db import RepairJob

    try:
        job = session.query(RepairJob).filter_by(id=job_id).first()
        if job:
            job.status = "failed"
            job.error_message = message[:1000]
            session.commit()
    except Exception:
        logger.exception("failed_to_update_job_status", job_id=job_id)


def _short_dropout_comment(status: str, final_state: dict) -> str:
    if status == "undiagnosable":
        return (
            "## 🔍 HealX — Could Not Diagnose\n\n"
            "Triage could not identify the root cause of this failure.\n\n"
            f"**Summary:** {final_state.get('error_summary', 'N/A')}\n"
        )
    return (
        "## ⚠️ HealX — Could Not Generate a Fix\n\n"
        f"**Reason:** {final_state.get('repair_reason', 'unknown')}\n\n"
        f"**Triage summary:** {final_state.get('error_summary', 'N/A')}\n"
    )
