"""
HealX Triage Agent — CI log analysis and failure classification.

Takes raw, noisy CI logs and produces a structured error summary:
- failure_type (SyntaxError, TestFailure, etc.)
- failing_file (relative path)
- failing_line (line number)
- error_summary (one sentence root cause)

Uses an LLM to handle the huge variety of CI log formats across
Python, Java, Node.js, Go, Ruby, Rust, etc.
"""

import json

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

from app.config import settings
from app.observability.langfuse_client import langchain_run_config

logger = structlog.get_logger(__name__)

# ─── Triage Prompt ───

TRIAGE_SYSTEM_PROMPT = """You are a CI/CD failure analyst. Your job is to extract structured information from raw CI pipeline logs.

Given CI logs, you MUST extract:
1. **failure_type**: One of: SyntaxError | TestFailure | ImportError | LintError | BuildError | TypeCheckError | DependencyError | ConfigError | Unknown
2. **failing_file**: The relative file path that caused the failure (e.g., "src/auth/login.py"). Return null if you cannot determine it.
3. **failing_line**: The line number where the error occurred. Return null if not visible.
4. **error_summary**: A single, precise sentence describing the root cause. Be specific — mention the actual error, not just "tests failed".
5. **relevant_files**: A list of up to 5 file paths that are relevant to understanding or fixing this error (e.g., the failing test file AND the source file it tests).
6. **error_snippet**: The most relevant 5-10 lines from the logs showing the actual error (not install noise).

Rules:
- IGNORE dependency installation output (pip install, npm install, mvn download lines)
- IGNORE build tool boilerplate and progress bars
- FOCUS on stack traces, assertion errors, compilation errors, and test output
- If multiple tests fail, focus on the FIRST failure (usually the root cause)
- Return ONLY valid JSON, no explanation

Output format:
{
  "failure_type": "...",
  "failing_file": "..." or null,
  "failing_line": 42 or null,
  "error_summary": "...",
  "relevant_files": ["...", "..."],
  "error_snippet": "..."
}"""


def run_triage(
    ci_logs: str,
    repo_name: str = "",
    *,
    job_id: str = "",
    attempt_number: int = 1,
    branch: str = "",
) -> dict:
    """
    Analyze CI logs and extract structured failure information.

    Args:
        ci_logs: Raw CI log text (can be very long).
        repo_name: Repository name for context.
        job_id: Repair job UUID — used for Langfuse trace identity.
        attempt_number: 1, 2, or 3 — Langfuse trace metadata.
        branch: Developer branch — Langfuse session grouping.

    Returns:
        Dict with failure_type, failing_file, failing_line,
        error_summary, relevant_files, error_snippet.
    """
    # Truncate logs if too long (LLM context limits)
    # Keep the last portion — errors are usually at the end
    max_log_chars = 15000
    if len(ci_logs) > max_log_chars:
        truncated_logs = (
            "[... earlier log output truncated ...]\n\n"
            + ci_logs[-max_log_chars:]
        )
    else:
        truncated_logs = ci_logs

    logger.info(
        "triage_starting",
        repo=repo_name,
        log_size=len(ci_logs),
        truncated_size=len(truncated_logs),
    )

    # Opus 4.7 rejects temperature/top_p/top_k — adaptive thinking is the only
    # steering knob and we leave it off here since we want raw JSON, not a
    # thinking block to parse around.
    llm = ChatAnthropic(
        model="claude-opus-4-7",
        api_key=settings.api_key,
        max_tokens=1000,
        timeout=60,
    )

    messages = [
        SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
        HumanMessage(content=f"Repository: {repo_name}\n\nCI Logs:\n```\n{truncated_logs}\n```"),
    ]

    lf_config = langchain_run_config(
        job_id=job_id,
        attempt_number=attempt_number,
        repo=repo_name,
        branch=branch,
        failure_type=None,
        run_name=f"triage (attempt {attempt_number})",
    )
    response = llm.invoke(messages, **({"config": lf_config} if lf_config else {}))
    raw_output = response.content.strip()

    # Parse JSON response
    try:
        # Handle markdown code blocks if the LLM wraps the output
        if raw_output.startswith("```"):
            raw_output = raw_output.split("```")[1]
            if raw_output.startswith("json"):
                raw_output = raw_output[4:]

        result = json.loads(raw_output)
    except json.JSONDecodeError:
        logger.warning(
            "triage_json_parse_failed",
            raw_output=raw_output[:200],
        )
        result = {
            "failure_type": "Unknown",
            "failing_file": None,
            "failing_line": None,
            "error_summary": "Failed to parse triage output. Raw: " + raw_output[:200],
            "relevant_files": [],
            "error_snippet": "",
        }

    # Validate and normalize
    valid_types = {
        "SyntaxError", "TestFailure", "ImportError", "LintError",
        "BuildError", "TypeCheckError", "DependencyError", "ConfigError", "Unknown",
    }
    if result.get("failure_type") not in valid_types:
        result["failure_type"] = "Unknown"

    if not isinstance(result.get("relevant_files"), list):
        result["relevant_files"] = []

    logger.info(
        "triage_completed",
        failure_type=result.get("failure_type"),
        failing_file=result.get("failing_file"),
        failing_line=result.get("failing_line"),
        error_summary=result.get("error_summary", "")[:100],
        relevant_file_count=len(result.get("relevant_files", [])),
    )

    # Track token usage via LangChain's standardized usage_metadata
    usage = getattr(response, "usage_metadata", None) or {}
    token_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    result["token_usage"] = token_usage

    return result
