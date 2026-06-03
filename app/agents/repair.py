"""
HealX Repair Agent — Patch generation for CI failures.

Takes the triage output (structured error info) + the actual source code
and generates a minimal unified diff patch to fix the issue.

On retry, it receives the previous patch AND the fresh CI logs from the
GitHub Actions run that failed with that patch applied — so it can see
exactly how its last attempt broke and try a different approach.
"""

import json

import structlog
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

from app.config import settings
from app.observability.langfuse_client import langchain_run_config
from app.pipeline.github_client import github_client

logger = structlog.get_logger(__name__)

# ─── Repair Prompt ───

REPAIR_SYSTEM_PROMPT = """You are an expert software repair agent. Your job is to generate minimal, surgical patches that fix CI pipeline failures.

You will receive:
- An error summary from triage
- The contents of the failing file(s)
- Optionally (on retry): the previous patch you produced and the CI logs from the GitHub Actions run that failed with that patch applied

Your task: Generate a unified diff patch that fixes the specific error.

Rules:
1. Generate a valid unified diff format (--- a/file, +++ b/file, @@ hunks)
2. Modify MAXIMUM 3 files
3. Change MAXIMUM 50 lines total
4. Do NOT touch: infra/, migrations/, secrets/, .github/
5. Do NOT introduce new dependencies unless absolutely necessary
6. Make the MINIMUM change needed — no refactoring, no style changes
7. If fixing a test, fix the source code NOT the test (unless the test itself is wrong)
8. Preserve existing code style (indentation, quotes, semicolons)

Output format — return ONLY the unified diff, nothing else:
```
--- a/path/to/file.py
+++ b/path/to/file.py
@@ -10,6 +10,7 @@
 unchanged line
-old line
+new line
 unchanged line
```

If you cannot generate a fix (the error is too complex, or requires architectural changes), return:
```
CANNOT_FIX: <one sentence explanation>
```"""

RETRY_CONTEXT = """
⚠️ PREVIOUS ATTEMPT FAILED ON CI

Your previous patch was committed and CI was re-run on GitHub Actions, but it still failed.

Previous patch:
```
{previous_patch}
```

CI logs from the run with that patch applied:
```
{previous_ci_logs}
```

Try a DIFFERENT approach this time. The previous fix did not work — analyse the new logs above to understand why, then produce a patch that addresses the actual failure mode you see now."""


def run_repair(
    triage_result: dict,
    repo: str,
    sha: str,
    previous_patch: str | None = None,
    previous_ci_logs: str | None = None,
    attempt_number: int = 1,
    *,
    job_id: str = "",
    branch: str = "",
) -> dict:
    """
    Generate a patch to fix the identified failure.

    Args:
        triage_result: Output from the Triage Agent (failure_type, failing_file, etc.)
        repo: Full repo name (owner/repo).
        sha: Commit SHA to read source files from.
        previous_patch: The patch from the previous attempt, if this is a retry.
        previous_ci_logs: CI logs from the GitHub Actions run that failed with
                          previous_patch applied. None on first attempt.
        attempt_number: 1, 2, or 3.

    Returns:
        Dict with:
            - patch_diff: Unified diff string (or None if cannot fix)
            - can_fix: Boolean
            - reason: Explanation if cannot fix
            - files_analyzed: List of files read for context
            - token_usage: Token count dict
    """
    failing_file = triage_result.get("failing_file")
    relevant_files = triage_result.get("relevant_files", [])
    error_summary = triage_result.get("error_summary", "Unknown error")
    failure_type = triage_result.get("failure_type", "Unknown")
    error_snippet = triage_result.get("error_snippet", "")

    # ─── Gather Source Code Context ───
    files_to_read = []

    # Add the failing file first
    if failing_file:
        files_to_read.append(failing_file)

    # Add relevant files
    for f in relevant_files:
        if f and f not in files_to_read:
            files_to_read.append(f)

    # Cap at 5 files to avoid token explosion
    files_to_read = files_to_read[:5]

    file_contents = {}
    for file_path in files_to_read:
        try:
            content = github_client.get_file_content(repo, file_path, sha)
            file_contents[file_path] = content
        except Exception as e:
            logger.warning(
                "repair_file_fetch_failed",
                file=file_path,
                error=str(e),
            )

    if not file_contents:
        logger.warning("repair_no_files_found", repo=repo, sha=sha[:8])
        return {
            "patch_diff": None,
            "can_fix": False,
            "reason": "Could not fetch any source files to analyze",
            "files_analyzed": [],
            "token_usage": {},
        }

    logger.info(
        "repair_starting",
        repo=repo,
        failure_type=failure_type,
        files_analyzed=list(file_contents.keys()),
        attempt_number=attempt_number,
    )

    # ─── Build Prompt ───
    file_context = ""
    for path, content in file_contents.items():
        # Add line numbers for LLM reference
        numbered_lines = []
        for i, line in enumerate(content.split("\n"), 1):
            numbered_lines.append(f"{i:4d} | {line}")
        numbered_content = "\n".join(numbered_lines)

        file_context += f"\n--- {path} ---\n{numbered_content}\n"

    human_message = f"""## CI Failure Details

**Failure Type:** {failure_type}
**Failing File:** {failing_file or "Unknown"}
**Error Summary:** {error_summary}

**Error Snippet from CI Logs:**
```
{error_snippet}
```

## Source Code

{file_context}
"""

    # Add retry context if this is a retry
    if previous_patch and previous_ci_logs:
        human_message += RETRY_CONTEXT.format(
            previous_patch=previous_patch,
            previous_ci_logs=previous_ci_logs[-3000:],
        )
        human_message += f"\nThis is attempt #{attempt_number} of 3.\n"

    # ─── Call LLM ───
    # Opus 4.7 doesn't accept temperature/top_p; the previous 0.2 setting for
    # "slightly creative retries" is replaced by the natural variance Claude
    # produces from the retry-context block in the prompt itself.
    llm = ChatAnthropic(
        model="claude-opus-4-7",
        api_key=settings.api_key,
        max_tokens=2000,
        timeout=120,
    )

    messages = [
        SystemMessage(content=REPAIR_SYSTEM_PROMPT),
        HumanMessage(content=human_message),
    ]

    lf_config = langchain_run_config(
        job_id=job_id,
        attempt_number=attempt_number,
        repo=repo,
        branch=branch,
        failure_type=failure_type,
        run_name=f"repair (attempt {attempt_number})",
    )
    response = llm.invoke(messages, **({"config": lf_config} if lf_config else {}))
    raw_output = response.content.strip()

    # ─── Parse Response ───
    usage = getattr(response, "usage_metadata", None) or {}
    token_usage = {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }

    # Check if the LLM says it can't fix
    if raw_output.startswith("CANNOT_FIX"):
        reason = raw_output.replace("CANNOT_FIX:", "").strip()
        logger.info("repair_cannot_fix", reason=reason)
        return {
            "patch_diff": None,
            "can_fix": False,
            "reason": reason,
            "files_analyzed": list(file_contents.keys()),
            "token_usage": token_usage,
        }

    # Extract diff from possible markdown code blocks
    patch_diff = _extract_diff(raw_output)

    if not patch_diff:
        logger.warning("repair_no_diff_extracted", raw_output=raw_output[:200])
        return {
            "patch_diff": None,
            "can_fix": False,
            "reason": "LLM output did not contain a valid unified diff",
            "files_analyzed": list(file_contents.keys()),
            "token_usage": token_usage,
        }

    # ─── Validate Patch Constraints ───
    validation = _validate_patch(patch_diff)
    if not validation["valid"]:
        logger.warning("repair_patch_invalid", reason=validation["reason"])
        return {
            "patch_diff": patch_diff,
            "can_fix": False,
            "reason": validation["reason"],
            "files_analyzed": list(file_contents.keys()),
            "token_usage": token_usage,
        }

    # Count lines changed
    lines_added = patch_diff.count("\n+") - patch_diff.count("\n+++")
    lines_removed = patch_diff.count("\n-") - patch_diff.count("\n---")

    logger.info(
        "repair_completed",
        files_analyzed=list(file_contents.keys()),
        lines_added=lines_added,
        lines_removed=lines_removed,
        patch_size=len(patch_diff),
    )

    return {
        "patch_diff": patch_diff,
        "can_fix": True,
        "reason": None,
        "files_analyzed": list(file_contents.keys()),
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "token_usage": token_usage,
    }


# ─── Helpers ───


def _extract_diff(raw_output: str) -> str | None:
    """
    Extract unified diff from LLM output.

    Handles cases where the LLM wraps the diff in markdown code blocks.
    """
    # Try to find diff within code blocks
    if "```" in raw_output:
        blocks = raw_output.split("```")
        for i, block in enumerate(blocks):
            if i % 2 == 1:  # Inside a code block
                # Remove language identifier
                clean = block.strip()
                if clean.startswith("diff"):
                    clean = clean[4:].strip()
                elif clean.startswith("patch"):
                    clean = clean[5:].strip()

                # Check if it looks like a diff
                if "---" in clean and "+++" in clean:
                    return clean

    # Try to extract directly (no code blocks)
    if "---" in raw_output and "+++" in raw_output:
        # Find the start of the diff
        lines = raw_output.split("\n")
        diff_lines = []
        in_diff = False

        for line in lines:
            if line.startswith("--- "):
                in_diff = True
            if in_diff:
                diff_lines.append(line)

        if diff_lines:
            return "\n".join(diff_lines)

    return None


def _validate_patch(patch_diff: str) -> dict:
    """
    Validate patch against safety constraints.

    Checks:
    - Maximum 3 files modified
    - Maximum 50 lines changed
    - No forbidden directories
    """
    # Count files modified
    files_modified = patch_diff.count("\n--- a/") + (1 if patch_diff.startswith("--- a/") else 0)
    if files_modified > 3:
        return {
            "valid": False,
            "reason": f"Patch modifies {files_modified} files (max 3)",
        }

    # Count lines changed
    lines_changed = 0
    for line in patch_diff.split("\n"):
        if (line.startswith("+") and not line.startswith("+++")) or \
           (line.startswith("-") and not line.startswith("---")):
            lines_changed += 1

    if lines_changed > 50:
        return {
            "valid": False,
            "reason": f"Patch changes {lines_changed} lines (max 50)",
        }

    # Check forbidden directories
    forbidden = ["infra/", "migrations/", "secrets/", ".github/"]
    for f in forbidden:
        if f"a/{f}" in patch_diff or f"b/{f}" in patch_diff:
            return {
                "valid": False,
                "reason": f"Patch modifies forbidden directory: {f}",
            }

    return {"valid": True, "reason": None}
