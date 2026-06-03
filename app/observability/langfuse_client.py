"""
HealX observability — Langfuse glue.

This module is the single point of integration with Langfuse. Every other
part of the app should import from here, not from `langfuse` directly, so
swapping providers (or temporarily disabling tracing) stays local.

Design notes:
- All entry points are no-ops when keys are unset. Local dev without a
  Langfuse account must still work end-to-end.
- Each repair job gets a deterministic trace_id (`healx-job-{uuid}`) so we
  can construct the trace URL without round-tripping through the API.
- Multi-attempt jobs share one trace via session_id grouping, so a single
  Langfuse view shows the entire retry sequence.
"""

from __future__ import annotations

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)


def _enabled() -> bool:
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def trace_id_for(job_id: str) -> str:
    """Deterministic trace ID for a repair job. Stable across retries."""
    return f"healx-job-{job_id}"


def trace_url_for(job_id: str) -> str | None:
    """Construct the Langfuse trace URL without an API call. None if disabled."""
    if not _enabled():
        return None
    host = settings.langfuse_host.rstrip("/")
    return f"{host}/trace/{trace_id_for(job_id)}"


def get_langfuse_handler(
    job_id: str,
    attempt_number: int,
    repo: str,
    branch: str,
    failure_type: str | None = None,
):
    """
    Return a configured LangChain CallbackHandler, or None when Langfuse is
    disabled. Callers should pass the result inside `config={"callbacks": [...]}`
    when invoking the LLM.

    Trace identity:
    - trace_id  : healx-job-{job_id}  (stable across retries → one trace per job)
    - session_id: {repo}:{branch}     (lets Langfuse filter by repo+branch)
    - user_id   : {repo}              (operator-friendly "user" facet)
    - tags      : ["healx", "attempt-{n}", "<failure_type>"]
    - metadata  : {job_id, attempt, failure_type}
    """
    if not _enabled():
        return None

    try:
        from langfuse.langchain import CallbackHandler  # type: ignore
    except Exception as e:  # pragma: no cover — wrong langfuse version
        logger.warning("langfuse_import_failed", error=str(e))
        return None

    tags = ["healx", f"attempt-{attempt_number}"]
    if failure_type:
        tags.append(failure_type)

    try:
        handler = CallbackHandler(
            trace_name=f"healx-{job_id}",
            session_id=f"{repo}:{branch}",
            user_id=repo,
            tags=tags,
            metadata={
                "langfuse_trace_id": trace_id_for(job_id),
                "job_id": job_id,
                "attempt": attempt_number,
                "failure_type": failure_type or "unknown",
                "repo": repo,
                "branch": branch,
            },
        )
        return handler
    except TypeError:
        # Langfuse SDK v3 stripped the constructor kwargs — trace identity
        # has to be passed at invoke time via config metadata. Return a bare
        # handler; agents will attach the metadata themselves.
        try:
            return CallbackHandler()
        except Exception as e:  # pragma: no cover
            logger.warning("langfuse_handler_init_failed", error=str(e))
            return None


def langchain_run_config(
    job_id: str,
    attempt_number: int,
    repo: str,
    branch: str,
    failure_type: str | None,
    run_name: str,
) -> dict:
    """
    Build the `config=` kwarg for a LangChain `.invoke()` call.

    Returns an empty dict when Langfuse is disabled, so the call site can
    safely `llm.invoke(messages, **{"config": cfg} if cfg else {})`.
    """
    handler = get_langfuse_handler(
        job_id=job_id,
        attempt_number=attempt_number,
        repo=repo,
        branch=branch,
        failure_type=failure_type,
    )
    if handler is None:
        return {}

    return {
        "callbacks": [handler],
        "run_name": run_name,
        "metadata": {
            "langfuse_trace_id": trace_id_for(job_id),
            "langfuse_session_id": f"{repo}:{branch}",
            "langfuse_user_id": repo,
            "langfuse_tags": ["healx", f"attempt-{attempt_number}"]
            + ([failure_type] if failure_type else []),
            "job_id": job_id,
            "attempt": attempt_number,
        },
    }


def flush_langfuse() -> None:
    """Drain any buffered traces. Safe to call when disabled."""
    if not _enabled():
        return
    try:
        from langfuse import Langfuse  # type: ignore

        Langfuse().flush()
        logger.info("langfuse_flushed")
    except Exception as e:
        logger.warning("langfuse_flush_failed", error=str(e))
