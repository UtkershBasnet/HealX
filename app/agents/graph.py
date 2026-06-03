"""
HealX Agent Graph — LangGraph state machine for one repair attempt.

The graph runs ONCE per webhook arrival and produces a patch (or a drop-out
status). It does not verify, it does not retry. Verification is GitHub
Actions; retry is the webhook-driven orchestrator picking up the next
workflow_run.completed event for the internal branch.

    START → triage → repair → END
                ↓        ↓
        undiagnosable  cannot_fix

The retry counter, attempt history, and internal branch state all live in
the database, not in the graph. The orchestrator reads them, primes the
repair agent with the previous patch + new CI logs, and re-invokes the
graph for each attempt.
"""

from typing import TypedDict

import structlog
from langgraph.graph import StateGraph, START, END

from app.agents.triage import run_triage
from app.agents.repair import run_repair

logger = structlog.get_logger(__name__)


# ─── State Definition ───


class PipelineState(TypedDict):
    """State for a single triage→repair invocation."""

    # ─── Job context (set before graph runs) ───
    job_id: str
    repo: str
    commit_sha: str
    branch: str
    ci_logs: str
    attempt_number: int  # 1, 2, or 3
    previous_patch: str | None  # from prior attempt, or None on first attempt

    # ─── Triage outputs ───
    failure_type: str
    failing_file: str | None
    failing_line: int | None
    error_summary: str
    relevant_files: list[str]
    error_snippet: str

    # ─── Repair outputs ───
    patch_diff: str | None
    can_fix: bool
    repair_reason: str | None

    # ─── Final result ───
    status: str  # patch_ready | undiagnosable | cannot_fix
    total_tokens: int


# ─── Nodes ───


def triage_node(state: PipelineState) -> dict:
    """Analyze CI logs to identify the root cause."""
    logger.info("graph_node_triage", job_id=state["job_id"], attempt=state["attempt_number"])

    result = run_triage(
        ci_logs=state["ci_logs"],
        repo_name=state["repo"],
        job_id=state["job_id"],
        attempt_number=state["attempt_number"],
        branch=state["branch"],
    )
    tokens = result.get("token_usage", {}).get("total_tokens", 0)

    if not result.get("failing_file") and result.get("failure_type") == "Unknown":
        return {
            "failure_type": result.get("failure_type", "Unknown"),
            "failing_file": None,
            "failing_line": None,
            "error_summary": result.get("error_summary", ""),
            "relevant_files": result.get("relevant_files", []),
            "error_snippet": result.get("error_snippet", ""),
            "status": "undiagnosable",
            "total_tokens": state.get("total_tokens", 0) + tokens,
        }

    return {
        "failure_type": result.get("failure_type", "Unknown"),
        "failing_file": result.get("failing_file"),
        "failing_line": result.get("failing_line"),
        "error_summary": result.get("error_summary", ""),
        "relevant_files": result.get("relevant_files", []),
        "error_snippet": result.get("error_snippet", ""),
        "total_tokens": state.get("total_tokens", 0) + tokens,
    }


def repair_node(state: PipelineState) -> dict:
    """Generate a patch. On retry, the orchestrator passes the previous patch
    and the new CI logs (already in state['ci_logs'])."""
    logger.info(
        "graph_node_repair",
        job_id=state["job_id"],
        attempt=state["attempt_number"],
    )

    triage_result = {
        "failure_type": state["failure_type"],
        "failing_file": state.get("failing_file"),
        "failing_line": state.get("failing_line"),
        "error_summary": state.get("error_summary", ""),
        "relevant_files": state.get("relevant_files", []),
        "error_snippet": state.get("error_snippet", ""),
    }

    result = run_repair(
        triage_result=triage_result,
        repo=state["repo"],
        sha=state["commit_sha"],
        previous_patch=state.get("previous_patch"),
        previous_ci_logs=state["ci_logs"] if state["attempt_number"] > 1 else None,
        attempt_number=state["attempt_number"],
        job_id=state["job_id"],
        branch=state["branch"],
    )

    tokens = result.get("token_usage", {}).get("total_tokens", 0)
    can_fix = result.get("can_fix", False)

    return {
        "patch_diff": result.get("patch_diff"),
        "can_fix": can_fix,
        "repair_reason": result.get("reason"),
        "status": "patch_ready" if can_fix else "cannot_fix",
        "total_tokens": state.get("total_tokens", 0) + tokens,
    }


# ─── Routing ───


def route_after_triage(state: PipelineState) -> str:
    return END if state.get("status") == "undiagnosable" else "repair"


# ─── Graph ───


def build_repair_graph() -> StateGraph:
    graph = StateGraph(PipelineState)

    graph.add_node("triage", triage_node)
    graph.add_node("repair", repair_node)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", route_after_triage, {"repair": "repair", END: END})
    graph.add_edge("repair", END)

    return graph.compile()


repair_graph = build_repair_graph()
