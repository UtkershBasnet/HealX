-- ─────────────────────────────────────────────────────────────────────────
-- HealX migration 002 — Architectural realignment (2026-05-29)
--
-- Drops the Docker sandbox verification model and moves to a GitHub-Actions-
-- driven verification loop. See changeinplan.md.
--
-- This migration is idempotent — it can be re-run without error.
-- ─────────────────────────────────────────────────────────────────────────

BEGIN;

-- ─── RepairJob: new columns for the two-branch flow + persisted triage ───
ALTER TABLE repair_jobs
    ADD COLUMN IF NOT EXISTS current_internal_branch VARCHAR(255),
    ADD COLUMN IF NOT EXISTS final_clean_branch VARCHAR(255),
    ADD COLUMN IF NOT EXISTS error_summary TEXT,
    ADD COLUMN IF NOT EXISTS failing_file VARCHAR(500),
    ADD COLUMN IF NOT EXISTS failing_line INTEGER;

CREATE INDEX IF NOT EXISTS ix_repair_jobs_current_internal_branch
    ON repair_jobs (current_internal_branch);

-- ─── RepairJob: workflow_run_id must be globally unique for idempotency ───
-- Duplicate webhook deliveries from GitHub must not create duplicate jobs.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_repair_jobs_workflow_run_id'
    ) THEN
        ALTER TABLE repair_jobs
            ADD CONSTRAINT uq_repair_jobs_workflow_run_id UNIQUE (workflow_run_id);
    END IF;
END $$;

-- ─── PatchAttempt: replace sandbox_output with CI-driven columns ───
ALTER TABLE patch_attempts
    ADD COLUMN IF NOT EXISTS internal_branch VARCHAR(255),
    ADD COLUMN IF NOT EXISTS internal_commit_sha VARCHAR(40),
    ADD COLUMN IF NOT EXISTS ci_run_id BIGINT,
    ADD COLUMN IF NOT EXISTS ci_output TEXT;

-- Migrate any historical sandbox_output text into ci_output before dropping.
-- Safe no-op if the column was already removed by an earlier migration run.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'patch_attempts' AND column_name = 'sandbox_output'
    ) THEN
        UPDATE patch_attempts
           SET ci_output = sandbox_output
         WHERE ci_output IS NULL AND sandbox_output IS NOT NULL;

        ALTER TABLE patch_attempts DROP COLUMN sandbox_output;
    END IF;
END $$;

-- ─── Status vocabulary realignment ───
-- The `status` column stays a free-form VARCHAR(30); we just retire the
-- sandbox-era statuses so existing rows aren't left referencing dead states.
UPDATE repair_jobs SET status = 'failed'
 WHERE status IN ('non-reproducible', 'sandbox-timeout', 'running');

COMMIT;
