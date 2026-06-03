"""
HealX Git Operations — Internal repair branches and clean PR branches.

Two-phase delivery:

1. push_patch_to_internal_branch — pushes an attempt to a hidden internal
   branch `healx/internal/run-{job_id}`. GitHub Actions runs CI on it.
   Developers never see this branch. Each retry is force-pushed on top of
   the failing SHA so the internal branch always contains exactly the
   current attempt as a single commit.

2. open_clean_fix_pr — once CI is green on the internal branch, take that
   tree, write it as ONE squashed commit on `healx/fix-{slug}` (branched
   from the original failing SHA), push, and open a PR to the developer's
   branch. The retry history is discarded.

Why subprocess git instead of the Contents API:
- Patches can touch multiple files; one git commit keeps the change atomic.
- Edge cases (new files, deletions, mode changes, binary content) are handled
  by git natively — re-implementing them on top of the Contents API is fragile.
"""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import structlog

from app.config import settings
from app.pipeline.github_client import github_client

logger = structlog.get_logger(__name__)

BOT_NAME = "HealX Bot"
BOT_EMAIL = "healx-bot@users.noreply.github.com"


@dataclass
class InternalPushResult:
    """Result from pushing a patch to the internal branch."""

    branch_name: str
    commit_sha: str


@dataclass
class PRResult:
    """Result from a successful PR creation on the clean branch."""

    pr_url: str
    branch_name: str
    commit_sha: str


class PatchApplyError(Exception):
    """
    Raised when `git apply` rejects the agent's patch before it can be pushed.

    This is the agent producing a syntactically-valid-looking diff that doesn't
    line up with the actual source tree (wrong line numbers, missing context,
    nonexistent target file). The orchestrator treats it as a recoverable
    diagnostic — recorded as a failed attempt, surfaced to the developer,
    not crashed as a generic exception.
    """

    def __init__(self, message: str, stderr: str, patch_preview: str):
        super().__init__(message)
        self.message = message
        self.stderr = stderr
        self.patch_preview = patch_preview


# ─── Public API ───


def push_patch_to_internal_branch(
    repo: str,
    base_sha: str,
    job_id: str,
    patch_diff: str,
    attempt_number: int,
    error_summary: str,
    failure_type: str,
    failing_file: str | None,
) -> InternalPushResult:
    """
    Apply a patch attempt to the internal branch and push.

    The internal branch is always reset to the failing SHA before applying
    the patch — each attempt is a single commit on top of the original
    failure, not a chain of fix-the-fix commits. This keeps the squash on
    the clean branch trivial (one commit in, one commit out).

    Returns the new commit SHA so the orchestrator can record it.
    """
    branch_name = _internal_branch_name(job_id)
    clone_url = _build_clone_url(repo)

    logger.info(
        "internal_push_starting",
        job_id=job_id,
        attempt=attempt_number,
        branch=branch_name,
        base_sha=base_sha[:8],
    )

    tmpdir = tempfile.mkdtemp(prefix="healx-internal-")
    try:
        _git(tmpdir, "clone", "--quiet", clone_url, ".")
        _git(tmpdir, "checkout", "--quiet", base_sha)
        _git(tmpdir, "checkout", "-b", branch_name)
        _git(tmpdir, "config", "user.email", BOT_EMAIL)
        _git(tmpdir, "config", "user.name", BOT_NAME)

        _apply_patch(tmpdir, patch_diff, job_id=job_id)

        commit_msg = _build_commit_message(
            error_summary=error_summary,
            failure_type=failure_type,
            failing_file=failing_file,
            job_id=job_id,
            attempt_number=attempt_number,
        )
        _git(tmpdir, "add", "-A")
        _git(tmpdir, "commit", "-m", commit_msg)

        # Force-push: each attempt rewrites the branch on top of base_sha so
        # the internal branch always holds exactly the current attempt.
        _git(tmpdir, "push", "--force", "--set-upstream", "origin", branch_name)

        commit_sha = _git(tmpdir, "rev-parse", "HEAD").strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    logger.info(
        "internal_push_done",
        job_id=job_id,
        attempt=attempt_number,
        branch=branch_name,
        commit_sha=commit_sha[:8],
    )
    return InternalPushResult(branch_name=branch_name, commit_sha=commit_sha)


def open_clean_fix_pr(
    repo: str,
    internal_branch: str,
    base_branch: str,
    base_sha: str,
    job_id: str,
    error_summary: str,
    failure_type: str,
    failing_file: str | None,
    failing_line: int | None,
    retry_count: int,
) -> PRResult:
    """
    Take the green tree from the internal branch, squash it into one commit
    on a fresh `healx/fix-{slug}` branch off the original failing SHA, push,
    and open a PR back to the developer's branch.

    Retry history on the internal branch is intentionally dropped.
    """
    clean_branch = _clean_branch_name(failure_type=failure_type, job_id=job_id)
    clone_url = _build_clone_url(repo)

    logger.info(
        "clean_pr_starting",
        job_id=job_id,
        internal_branch=internal_branch,
        clean_branch=clean_branch,
        base_branch=base_branch,
    )

    tmpdir = tempfile.mkdtemp(prefix="healx-clean-")
    try:
        _git(tmpdir, "clone", "--quiet", clone_url, ".")
        _git(tmpdir, "config", "user.email", BOT_EMAIL)
        _git(tmpdir, "config", "user.name", BOT_NAME)

        # Fetch the internal branch so we can read its tree.
        _git(tmpdir, "fetch", "origin", internal_branch)
        internal_ref = f"origin/{internal_branch}"

        # Branch off the original failing SHA and stage the internal tree on top
        # as ONE commit (squash). `read-tree` + `checkout-index` reproduces the
        # final state without any of the retry commits.
        _git(tmpdir, "checkout", "--quiet", base_sha)
        _git(tmpdir, "checkout", "-b", clean_branch)

        _git(tmpdir, "read-tree", "-u", "--reset", internal_ref)

        commit_msg = _build_squashed_commit_message(
            error_summary=error_summary,
            failure_type=failure_type,
            failing_file=failing_file,
            job_id=job_id,
        )
        _git(tmpdir, "add", "-A")
        _git(tmpdir, "commit", "-m", commit_msg)

        _git(tmpdir, "push", "--force", "--set-upstream", "origin", clean_branch)

        commit_sha = _git(tmpdir, "rev-parse", "HEAD").strip()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    title = _build_pr_title(failure_type=failure_type, error_summary=error_summary)
    body = _build_pr_body(
        job_id=job_id,
        base_sha=base_sha,
        error_summary=error_summary,
        failure_type=failure_type,
        failing_file=failing_file,
        failing_line=failing_line,
        retry_count=retry_count,
    )

    pr_url = github_client.open_pr(
        repo=repo,
        title=title,
        body=body,
        head=clean_branch,
        base=base_branch,
    )

    logger.info(
        "clean_pr_done",
        job_id=job_id,
        pr_url=pr_url,
        clean_branch=clean_branch,
        commit_sha=commit_sha[:8],
    )
    return PRResult(pr_url=pr_url, branch_name=clean_branch, commit_sha=commit_sha)


# ─── Internal: git plumbing ───


def _apply_patch(tmpdir: str, patch_diff: str, job_id: str) -> None:
    patch_path = os.path.join(tmpdir, ".healx.patch")
    # git apply is strict about trailing newlines — LLM output often omits one.
    normalized = patch_diff if patch_diff.endswith("\n") else patch_diff + "\n"
    with open(patch_path, "w") as f:
        f.write(normalized)

    plain_stderr = ""
    try:
        _git(tmpdir, "apply", patch_path)
        return
    except subprocess.CalledProcessError as e:
        plain_stderr = (e.stderr or "").strip()
        logger.info(
            "git_apply_retrying_3way",
            job_id=job_id,
            plain_stderr=plain_stderr[:500],
        )

    try:
        _git(tmpdir, "apply", "--3way", patch_path)
    except subprocess.CalledProcessError as e3:
        threeway_stderr = (e3.stderr or "").strip()
        logger.warning(
            "git_apply_failed",
            job_id=job_id,
            plain_stderr=plain_stderr[:500],
            threeway_stderr=threeway_stderr[:500],
            patch_preview=normalized[:300],
        )
        raise PatchApplyError(
            message="git apply rejected the agent's patch in both plain and 3-way modes",
            stderr=(
                f"--- plain apply ---\n{plain_stderr or '(no stderr)'}\n\n"
                f"--- 3-way apply ---\n{threeway_stderr or '(no stderr)'}"
            ),
            patch_preview=normalized[:2000],
        ) from e3
    finally:
        if os.path.exists(patch_path):
            os.remove(patch_path)


def _git(cwd: str, *args: str) -> str:
    """Run a git subcommand. On non-zero exit, log stderr before raising so
    failures are diagnosable from the worker logs without needing a re-run."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.warning(
            "git_command_failed",
            args=list(args)[:2],  # don't log full args; patch paths are noisy
            returncode=e.returncode,
            stderr=(e.stderr or "").strip()[:1000],
        )
        raise


def _build_clone_url(repo: str) -> str:
    return f"https://x-access-token:{settings.github_token}@github.com/{repo}.git"


# ─── Internal: branch naming ───


def _internal_branch_name(job_id: str) -> str:
    return f"healx/internal/run-{job_id}"


def _clean_branch_name(failure_type: str, job_id: str) -> str:
    """healx/fix-{kebab-slug}-{short-id}. Slug derived from failure_type."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", failure_type.lower()).strip("-") or "ci"
    short = job_id.replace("-", "")[:8]
    return f"healx/fix-{slug}-{short}"


# ─── Internal: copy ───


def _build_pr_title(*, failure_type: str, error_summary: str) -> str:
    summary = error_summary.strip().rstrip(".")
    if len(summary) > 60:
        summary = summary[:57] + "..."
    return f"HealX: fix {failure_type} — {summary}"


def _build_commit_message(
    *,
    error_summary: str,
    failure_type: str,
    failing_file: str | None,
    job_id: str,
    attempt_number: int,
) -> str:
    short_summary = error_summary.strip().rstrip(".")
    if len(short_summary) > 72:
        short_summary = short_summary[:69] + "..."
    return (
        f"fix(healx): {short_summary}\n\n"
        f"Attempt {attempt_number} for HealX repair job {job_id}.\n"
        f"Failure type: {failure_type}\n"
        f"File: {failing_file or 'unknown'}\n"
    )


def _build_squashed_commit_message(
    *,
    error_summary: str,
    failure_type: str,
    failing_file: str | None,
    job_id: str,
) -> str:
    short_summary = error_summary.strip().rstrip(".")
    if len(short_summary) > 72:
        short_summary = short_summary[:69] + "..."
    return (
        f"fix(healx): {short_summary}\n\n"
        f"HealX auto-generated fix for {failure_type}.\n"
        f"File: {failing_file or 'unknown'}\n"
        f"Job: {job_id}\n"
    )


def _build_pr_body(
    *,
    job_id: str,
    base_sha: str,
    error_summary: str,
    failure_type: str,
    failing_file: str | None,
    failing_line: int | None,
    retry_count: int,
) -> str:
    file_line = f"`{failing_file}`" if failing_file else "_unknown_"
    if failing_file and failing_line:
        file_line += f" — Line {failing_line}"

    attempts_label = "1st attempt" if retry_count <= 1 else f"{retry_count} attempts"

    return f"""## 🤖 HealX Automated Fix

### Root Cause
{error_summary}

### Failing Location
{file_line}

**Failure type:** `{failure_type}`

### Verification
- ✅ Branched from failing commit `{base_sha[:8]}`
- ✅ GitHub Actions CI passed on the fix
- 🔁 {attempts_label} required

### Job
`{job_id}`

---
*Opened automatically by HealX. The retry history was squashed into a single commit — review the diff carefully before merging.*
"""


# ─── Escalation ───


def post_escalation_comment(
    repo: str,
    commit_sha: str,
    job_id: str,
    error_summary: str,
    failure_type: str,
    attempts: list[dict],
    max_retries: int,
) -> None:
    """
    Post a commit comment summarizing why HealX could not auto-fix.

    Includes every attempted patch and its CI output so an engineer can
    pick up where HealX left off without rerunning anything.
    """
    attempts_md = ""
    for i, attempt in enumerate(attempts, start=1):
        ci_output = (attempt.get("ci_output") or "").strip()
        if len(ci_output) > 2000:
            ci_output = "[... truncated ...]\n" + ci_output[-2000:]

        attempt_failure_type = attempt.get("failure_type") or "Unknown"
        attempt_summary = attempt.get("error_summary") or "(no summary recorded)"
        attempt_file = attempt.get("failing_file")
        attempt_line = attempt.get("failing_line")
        location = f"`{attempt_file}`" if attempt_file else "_unknown_"
        if attempt_file and attempt_line:
            location += f" — Line {attempt_line}"

        attempts_md += (
            f"\n#### Attempt {i}\n"
            f"**Diagnosis:** {attempt_summary}\n"
            f"**Failure type:** `{attempt_failure_type}` &nbsp;·&nbsp; **Location:** {location}\n\n"
            f"<details><summary>Patch diff</summary>\n\n"
            f"```diff\n{attempt.get('patch_diff') or '(no diff)'}\n```\n"
            f"</details>\n\n"
            f"<details><summary>CI output (run {attempt.get('ci_run_id', 'N/A')})</summary>\n\n"
            f"```\n{ci_output or '(empty)'}\n```\n"
            f"</details>\n"
        )

    body = (
        "## ⚠️ HealX Could Not Auto-Fix This Failure\n\n"
        f"HealX attempted **{len(attempts)} of {max_retries}** patches. "
        "None passed GitHub Actions CI.\n\n"
        f"**Root cause (from triage):** {error_summary}\n"
        f"**Failure type:** `{failure_type}`\n\n"
        f"### What Was Tried{attempts_md}\n\n"
        "### Recommendation\n"
        "Manual review required. The failure is beyond HealX's automated repair scope.\n\n"
        f"_Job: `{job_id}`_"
    )

    github_client.post_comment(repo=repo, sha=commit_sha, body=body)
    logger.info("escalation_comment_posted", job_id=job_id, repo=repo)
