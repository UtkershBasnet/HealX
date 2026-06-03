-- ─────────────────────────────────────────────────────────────────────────
-- HealX migration 003 — Per-attempt triage snapshot on patch_attempts
--
-- Different retries can produce different triage diagnoses (the new CI logs
-- after a failed patch may point at a different file or failure type). The
-- escalation comment renders one row per attempt and should show that
-- attempt's diagnosis, not the latest one on repair_jobs.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE patch_attempts
    ADD COLUMN IF NOT EXISTS failure_type  VARCHAR(50),
    ADD COLUMN IF NOT EXISTS error_summary TEXT,
    ADD COLUMN IF NOT EXISTS failing_file  VARCHAR(500),
    ADD COLUMN IF NOT EXISTS failing_line  INTEGER;

COMMIT;
