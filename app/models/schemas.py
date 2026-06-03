"""
HealX API Schemas — Pydantic models for request/response serialization.

These are the API-layer representations. They are COMPLETELY DECOUPLED from
the ORM models in db.py. Never return ORM objects directly from endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─── Enums ───


class JobStatus(str, Enum):
    """Possible states for a repair job."""

    QUEUED = "queued"
    REPAIRING = "repairing"
    RETRYING = "retrying"
    PR_OPENED = "pr_opened"
    FAILED = "failed"
    NEEDS_HUMAN_REVIEW = "needs-human-review"
    UNDIAGNOSABLE = "undiagnosable"


class FailureType(str, Enum):
    """Categories of CI failure."""

    SYNTAX_ERROR = "SyntaxError"
    TEST_FAILURE = "TestFailure"
    IMPORT_ERROR = "ImportError"
    LINT_ERROR = "LintError"
    BUILD_ERROR = "BuildError"
    UNKNOWN = "Unknown"


class FeedbackSignal(str, Enum):
    """Human feedback signals on patches."""

    ACCEPT = "ACCEPT"
    NACK = "NACK"
    PARTIAL_NACK = "PARTIAL_NACK"
    SKIP = "SKIP"


# ─── Webhook Schemas ───


class WebhookPayload(BaseModel):
    """Parsed payload from a GitHub Actions workflow_run webhook."""

    repo_name: str = Field(..., description="Full repo name (owner/repo)")
    branch_name: str = Field(..., description="Head branch of the workflow run")
    commit_sha: str = Field(..., description="HEAD SHA that triggered the run")
    workflow_run_id: int = Field(..., description="GitHub workflow run ID")
    logs_url: str = Field(..., description="URL to fetch workflow logs")


# ─── Repair Job Schemas ───


class RepairJobCreate(BaseModel):
    """Internal schema for creating a new repair job from webhook data."""

    repo_name: str
    branch_name: str
    commit_sha: str
    workflow_run_id: int | None = None


class RepairJobResponse(BaseModel):
    """API response schema for a repair job."""

    id: uuid.UUID
    repo_name: str
    branch_name: str
    commit_sha: str
    workflow_run_id: int | None = None
    failure_type: str | None = None
    status: JobStatus
    retry_count: int
    current_internal_branch: str | None = None
    final_clean_branch: str | None = None
    error_summary: str | None = None
    failing_file: str | None = None
    failing_line: int | None = None
    error_message: str | None = None
    pr_url: str | None = None
    langfuse_trace_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RepairJobListResponse(BaseModel):
    """Paginated list of repair jobs."""

    jobs: list[RepairJobResponse]
    total: int
    page: int
    per_page: int


# ─── Patch Attempt Schemas ───


class PatchAttemptResponse(BaseModel):
    """API response schema for a patch attempt."""

    id: uuid.UUID
    job_id: uuid.UUID
    attempt_number: int
    success: bool
    patch_diff: str | None = None
    model_used: str | None = None
    token_count: int | None = None
    failure_type: str | None = None
    error_summary: str | None = None
    failing_file: str | None = None
    failing_line: int | None = None
    internal_branch: str | None = None
    internal_commit_sha: str | None = None
    ci_run_id: int | None = None
    ci_output: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Patch Feedback Schemas ───


class PatchFeedbackCreate(BaseModel):
    """Schema for submitting feedback on a patch."""

    signal: FeedbackSignal
    engineer_comment: str | None = None


class PatchFeedbackResponse(BaseModel):
    """API response schema for patch feedback."""

    id: uuid.UUID
    job_id: uuid.UUID
    signal: FeedbackSignal
    engineer_comment: str | None = None
    recorded_at: datetime

    model_config = {"from_attributes": True}


# ─── Pipeline State (used by LangGraph agents) ───


class TriageResult(BaseModel):
    """Structured output from the Triage Agent."""

    failure_type: FailureType
    failing_file: str | None = None
    failing_line: int | None = None
    error_summary: str


class RepairResult(BaseModel):
    """Structured output from the Repair Agent."""

    patch_diff: str
    files_modified: int
    lines_changed: int


# ─── Generic API Responses ───


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = "ok"
    version: str = "0.1.0"
    environment: str = "development"


class WebhookAcceptedResponse(BaseModel):
    """Response returned when a webhook is accepted for processing."""

    accepted: bool = True
    job_id: uuid.UUID
    message: str = "Job enqueued for processing"
