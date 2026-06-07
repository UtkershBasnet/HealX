"""
HealX ORM Models — SQLAlchemy database models.

These are the database-layer representations. NEVER expose these directly
through the API — use the Pydantic schemas in schemas.py instead.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import settings


# ─── Engine & Session Factory ───


engine = create_async_engine(
    settings.database_url,
    echo=(not settings.is_production),
    pool_size=5,
    max_overflow=5,
)

async_session = async_sessionmaker(engine, expire_on_commit=False)


# ─── Base ───


class Base(AsyncAttrs, DeclarativeBase):
    """Base class for all ORM models."""

    pass


# ─── Models ───


class RepairJob(Base):
    """
    Represents a single CI failure repair job.

    Lifecycle:
        queued → repairing → (retrying ×N) → (pr_opened | failed | undiagnosable)

    Verification runs on GitHub Actions, not locally. Each attempt pushes a
    patch to `current_internal_branch` and waits for the workflow_run.completed
    webhook to advance state. On green CI the final tree is squashed onto
    `final_clean_branch` and a PR is opened.
    """

    __tablename__ = "repair_jobs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", name="uq_repair_jobs_workflow_run_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_name = Column(String(255), nullable=False, index=True)
    branch_name = Column(String(255), nullable=False, index=True)
    commit_sha = Column(String(40), nullable=False)
    workflow_run_id = Column(BigInteger, nullable=True)
    failure_type = Column(String(50), nullable=True)
    status = Column(
        String(30),
        nullable=False,
        default="queued",
        index=True,
    )
    retry_count = Column(Integer, nullable=False, default=0)
    current_internal_branch = Column(String(255), nullable=True, index=True)
    final_clean_branch = Column(String(255), nullable=True)
    error_summary = Column(Text, nullable=True)
    failing_file = Column(String(500), nullable=True)
    failing_line = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    pr_url = Column(Text, nullable=True)
    langfuse_trace_url = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    attempts = relationship(
        "PatchAttempt", back_populates="job", cascade="all, delete-orphan"
    )
    feedback = relationship(
        "PatchFeedback", back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<RepairJob id={self.id} repo={self.repo_name} "
            f"branch={self.branch_name} status={self.status}>"
        )


class PatchAttempt(Base):
    """
    Records a single repair attempt for a job.

    Each job can have up to 3 attempts (retry_count limit).
    """

    __tablename__ = "patch_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(
        UUID(as_uuid=True), ForeignKey("repair_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    attempt_number = Column(Integer, nullable=False)
    success = Column(Boolean, nullable=False, default=False)
    patch_diff = Column(Text, nullable=True)
    model_used = Column(String(100), nullable=True)
    token_count = Column(Integer, nullable=True)
    # Per-attempt triage snapshot — different retries can diagnose differently;
    # the escalation comment uses these so each row reflects its own diagnosis.
    failure_type = Column(String(50), nullable=True)
    error_summary = Column(Text, nullable=True)
    failing_file = Column(String(500), nullable=True)
    failing_line = Column(Integer, nullable=True)
    internal_branch = Column(String(255), nullable=True)
    internal_commit_sha = Column(String(40), nullable=True)
    ci_run_id = Column(BigInteger, nullable=True)
    ci_output = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    job = relationship("RepairJob", back_populates="attempts")

    def __repr__(self) -> str:
        return (
            f"<PatchAttempt id={self.id} job={self.job_id} "
            f"attempt={self.attempt_number} success={self.success}>"
        )


class PatchFeedback(Base):
    """
    Captures human feedback on AI-generated patches.

    Signals: ACCEPT | NACK | PARTIAL_NACK | SKIP
    """

    __tablename__ = "patch_feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(
        UUID(as_uuid=True), ForeignKey("repair_jobs.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    signal = Column(String(20), nullable=False)
    engineer_comment = Column(Text, nullable=True)
    recorded_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    job = relationship("RepairJob", back_populates="feedback")

    def __repr__(self) -> str:
        return f"<PatchFeedback id={self.id} job={self.job_id} signal={self.signal}>"
