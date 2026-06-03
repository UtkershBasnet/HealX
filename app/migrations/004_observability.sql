-- ─────────────────────────────────────────────────────────────────────────
-- HealX migration 004 — Observability (2026-05-30)
--
-- Stores the deterministic Langfuse trace URL per repair job so the API
-- and dashboard can deep-link directly to the agent traces.
--
-- Idempotent — safe to re-run.
-- ─────────────────────────────────────────────────────────────────────────

BEGIN;

ALTER TABLE repair_jobs
    ADD COLUMN IF NOT EXISTS langfuse_trace_url TEXT;

COMMIT;
