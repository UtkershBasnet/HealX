"""
HealX GitHub Client — All GitHub API interactions.

Uses PyGithub for authenticated operations and httpx for raw log fetching.
Provides a clean interface for the rest of the application to interact with GitHub
without coupling to the PyGithub library internals.
"""

import zipfile
import io
import structlog
import httpx
from github import Github, GithubException

from app.config import settings

logger = structlog.get_logger(__name__)


class GitHubClient:
    """Encapsulates all GitHub API operations for HealX."""

    def __init__(self, token: str | None = None):
        self._token = token or settings.github_token
        self._github = Github(self._token)
        self._http_headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

    # ─── Log Fetching ───

    async def get_workflow_logs(self, repo: str, run_id: int) -> str:
        """
        Fetch workflow run logs from GitHub.

        Downloads the log archive, extracts, and returns the combined log text.
        Uses httpx because PyGithub doesn't support async log downloads.

        Args:
            repo: Full repo name (e.g., "owner/repo").
            run_id: GitHub Actions workflow run ID.

        Returns:
            Combined log text from all job steps.
        """
        url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/logs"

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=self._http_headers)
            response.raise_for_status()

            # GitHub returns a zip archive of logs
            log_lines: list[str] = []
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                for name in sorted(zf.namelist()):
                    if name.endswith(".txt"):
                        content = zf.read(name).decode("utf-8", errors="replace")
                        log_lines.append(f"--- {name} ---\n{content}")

            combined = "\n".join(log_lines)

            logger.info(
                "fetched_workflow_logs",
                repo=repo,
                run_id=run_id,
                log_size=len(combined),
            )
            return combined

    # ─── File Operations ───

    def get_file_content(self, repo: str, path: str, ref: str) -> str:
        """
        Fetch file content from a repository at a specific ref.

        Args:
            repo: Full repo name.
            path: Relative file path within the repo.
            ref: Git ref (branch, tag, or SHA).

        Returns:
            Decoded file content as string.
        """
        gh_repo = self._github.get_repo(repo)
        content = gh_repo.get_contents(path, ref=ref)

        # get_contents can return a list for directories
        if isinstance(content, list):
            raise ValueError(
                f"Path '{path}' is a directory, not a file."
            )

        decoded = content.decoded_content.decode("utf-8")
        logger.info("fetched_file", repo=repo, path=path, ref=ref[:8])
        return decoded

    # ─── Branch Operations ───

    def create_branch(self, repo: str, branch_name: str, sha: str) -> None:
        """
        Create a new branch from a specific commit SHA.

        Args:
            repo: Full repo name.
            branch_name: Name for the new branch (e.g., "fix/healx-abc123").
            sha: Commit SHA to branch from.
        """
        gh_repo = self._github.get_repo(repo)
        ref = f"refs/heads/{branch_name}"

        try:
            gh_repo.create_git_ref(ref=ref, sha=sha)
            logger.info("created_branch", repo=repo, branch=branch_name, sha=sha[:8])
        except GithubException as e:
            if e.status == 422:
                # Branch already exists — not necessarily an error
                logger.warning(
                    "branch_already_exists", repo=repo, branch=branch_name
                )
            else:
                raise

    # ─── Commit Operations ───

    def commit_file(
        self,
        repo: str,
        path: str,
        content: str,
        branch: str,
        message: str,
    ) -> None:
        """
        Create or update a file in the repository via the Contents API.

        This avoids needing to git push — works entirely through the GitHub API.

        Args:
            repo: Full repo name.
            path: File path in the repo.
            content: New file content.
            branch: Branch to commit to.
            message: Commit message.
        """
        gh_repo = self._github.get_repo(repo)

        try:
            # Try to get existing file (needed for update SHA)
            existing = gh_repo.get_contents(path, ref=branch)
            if isinstance(existing, list):
                raise ValueError(f"Path '{path}' is a directory.")
            gh_repo.update_file(
                path=path,
                message=message,
                content=content,
                sha=existing.sha,
                branch=branch,
            )
            logger.info("updated_file", repo=repo, path=path, branch=branch)
        except GithubException as e:
            if e.status == 404:
                # File doesn't exist yet — create it
                gh_repo.create_file(
                    path=path,
                    message=message,
                    content=content,
                    branch=branch,
                )
                logger.info("created_file", repo=repo, path=path, branch=branch)
            else:
                raise

    # ─── Pull Request Operations ───

    def open_pr(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> str:
        """
        Open a pull request.

        Args:
            repo: Full repo name.
            title: PR title.
            body: PR body (markdown).
            head: Head branch (the fix branch).
            base: Base branch (the branch that was broken).

        Returns:
            URL of the created pull request.
        """
        gh_repo = self._github.get_repo(repo)
        pr = gh_repo.create_pull(
            title=title,
            body=body,
            head=head,
            base=base,
        )
        logger.info("opened_pr", repo=repo, pr_url=pr.html_url)
        return pr.html_url

    # ─── Comment Operations ───

    def post_comment(self, repo: str, sha: str, body: str) -> None:
        """
        Post a comment on a specific commit.

        Used for: escalation notices, status updates, CI-pass confirmations.

        Args:
            repo: Full repo name.
            sha: Commit SHA to comment on.
            body: Comment body (markdown).
        """
        gh_repo = self._github.get_repo(repo)
        commit = gh_repo.get_commit(sha)
        commit.create_comment(body=body)
        logger.info("posted_comment", repo=repo, sha=sha[:8])


# ─── Singleton ───

github_client = GitHubClient()
