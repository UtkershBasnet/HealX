# HealX — Build Tasks

## Phase 1 — Foundation ✅
- [x] All scaffolding, DB, webhook, queue, worker, API

## Phase 2 — Docker Sandbox ⛔ Removed 
Architectural pivot — GitHub Actions is now the verification engine; the local Docker sandbox path was removed from the critical path entirely.
- [x] ~~Docker runner~~ — file deleted
- [x] ~~Workflow YAML analyzer~~ — file deleted
- [x] ~~Sandbox entrypoint + Dockerfiles~~ — `sandbox/` directory deleted

## Phase 3 — Triage + Repair Agents ✅
- [x] `app/agents/triage.py` — Triage Agent (CI log analysis → structured error)
- [x] `app/agents/repair.py` — Repair Agent (error + code → patch)
- [x] `app/agents/graph.py` — LangGraph state machine
- [x] Retry logic: up to 3 attempts (now driven by webhooks, not the graph)
- [x] Store patch attempts in DB

## Phase 4 — PR Delivery + Escalation ✅
- [x] `app/pipeline/git_ops.py` — internal + clean branch flow
- [x] PR templates (mention CI-verified, not sandbox-verified)
- [x] Escalation comments (include CI run IDs, not sandbox output)

## Phase 4.5 — Architectural Realignment ✅ 
Pivot from local Docker sandbox to GitHub-Actions-as-verifier with webhook-driven retry.

- [x] **Stage 0 — DB schema**
  - [x] `RepairJob` gained `current_internal_branch`, `final_clean_branch`, `error_summary`, `failing_file`, `failing_line` columns
  - [x] Unique constraint on `workflow_run_id` (idempotency for duplicate webhook deliveries)
  - [x] `PatchAttempt` swapped `sandbox_output` → `internal_branch` / `internal_commit_sha` / `ci_run_id` / `ci_output`
  - [x] `JobStatus` reshaped: added `repairing`, `retrying`, `pr_opened`; removed `non-reproducible`, `sandbox-timeout`, `running`
  - [x] `app/migrations/002_realignment.sql` written (idempotent, safe to re-run)
  - [x] Migration applied to dev DB — required a one-off `TRUNCATE repair_jobs CASCADE` first because pre-pivot rows had duplicate `workflow_run_id`s

- [x] **Stage 1 — Sandbox deletion**
  - [x] Deleted `app/pipeline/docker_runner.py`, `workflow_analyzer.py`, `language_detector.py`
  - [x] Deleted `sandbox/` directory
  - [x] Stripped `sandbox_*` settings from `app/config.py`
  - [x] Removed `docker==7.1.0` from `requirements.txt`
  - [x] Removed `/var/run/docker.sock` mounts from `docker-compose.yml`
  - [x] `app/config.py` set `extra="ignore"` so leftover `SANDBOX_*` env vars in `.env` don't break boot

- [x] **Stage 2 — Agent graph shrink**
  - [x] `app/agents/graph.py` is now `START → triage → repair → END`
  - [x] No internal retry loop; no sandbox node
  - [x] `app/agents/repair.py` signature: `(previous_patch, previous_ci_logs, attempt_number)` instead of `previous_attempts: list[dict]`

- [x] **Stage 3 — Two-branch git ops**
  - [x] `push_patch_to_internal_branch()` — force-pushes each attempt as ONE commit on top of failing SHA to `healx/internal/run-{job_id}`
  - [x] `open_clean_fix_pr()` — uses `git read-tree --reset` to squash the green internal tree into one commit on `healx/fix-{slug}`, opens PR to developer branch
  - [x] Developers only ever see the clean PR; retry history is discarded

- [x] **Stage 4 — Orchestrator split**
  - [x] `handle_initial_failure(job_id)` — entry point for `workflow_run.failed` on developer branches
  - [x] `handle_internal_branch_completion(job_id, ci_conclusion, new_run_id)` — entry point for `workflow_run.completed` on `healx/internal/*` branches
  - [x] State (retry_count, internal branch, last triage output) lives in the DB across webhook invocations
  - [x] On success → open clean PR + mark `pr_opened`
  - [x] On failure + retries left → fetch new CI logs, re-run graph with previous patch + new logs, push next attempt to same internal branch
  - [x] On failure + retries exhausted (3) → escalation comment, no PR

- [x] **Stage 5 — Webhook routing**
  - [x] Router differentiates `healx/internal/run-*` events from developer-branch events
  - [x] Internal-branch events look up the existing job by `current_internal_branch` and route to `handle_internal_branch_completion`
  - [x] Developer-branch failures create a new job (idempotent via Redis NX-set + DB unique constraint on `workflow_run_id`)

- [x] **Stage 6 — Cleanup**
  - [x] `/jobs/{job_id}/retry` endpoint now enqueues `handle_initial_failure` and resets internal-branch state
  - [x] Stale `non-reproducible`, `sandbox-timeout`, `previous_attempts`, `run_repair_pipeline` references gone from the code path

- [x] **Stage 7 — Smoke check**
  - [x] `python3 -m compileall app/` passes
  - [x] AST inventory confirms expected public surface in every module

## Phase 5 — Observability ✅ 
Plan: `phase5_plan.md`. Four stages all landed and smoke-tested end-to-end.

- [x] **Stage A — DB + ORM**
  - [x] `repair_jobs.langfuse_trace_url` column + `app/migrations/004_observability.sql` (applied)
  - [x] `RepairJobResponse.langfuse_trace_url` exposed in the API

- [x] **Stage B — Langfuse glue**
  - [x] `app/observability/langfuse_client.py` — handler factory + `langchain_run_config()` + `flush_langfuse()`; graceful no-op when keys are unset
  - [x] `app/agents/triage.py`, `repair.py`, `graph.py` thread `job_id` / `branch` / `attempt_number` through to the LLM via `config={"callbacks": [...]}`
  - [x] Deterministic trace id (`healx-job-{job_id}`) shared across retries → one trace per job
  - [x] `app/main.py` lifespan flushes Langfuse on shutdown
  - [x] Orchestrator stamps `langfuse_trace_url` on the job at the start of `handle_initial_failure`

- [x] **Stage C — Enhanced API**
  - [x] `app/api/` package; endpoints moved out of `main.py` into `jobs.py`, `stats.py`, `timeline.py`
  - [x] `GET /jobs` accepts `status` / `repo` / `failure_type` / `since` filters
  - [x] `GET /stats` returns totals-by-status, fix_rate, mean attempts-to-pr, mean seconds-to-pr, top 5 repos, failure-type distribution
  - [x] `GET /jobs/{id}/timeline` reconstructs the event log from existing rows (intermediate retrying↔repairing flips remain lossy — add a `repair_job_events` audit table if needed)
  - [x] `GET /jobs/{id}/attempts` enriched with `github_run_url` + `langfuse_trace_url`

- [x] **Stage D — Minimal Jinja2 dashboard**
  - [x] `jinja2==3.1.4` added to requirements; images rebuilt
  - [x] `app/templates/{base,dashboard,job_detail}.html`
  - [x] `app/static/{dash.css,dash.js,job_detail.js}` — vanilla JS polling, no framework
  - [x] `/dashboard` and `/dashboard/jobs/{id}` routes (excluded from OpenAPI)
  - [x] Auto dark mode via `prefers-color-scheme`

- [x] **Stage E — Smoke test**
  - [x] `/health`, `/stats`, `/jobs`, `/jobs/{id}/timeline`, `/dashboard`, `/dashboard/jobs/{id}`, `/static/*` all 200
  - [ ] Trigger a new repair against the demo repo and confirm a Langfuse trace appears + `langfuse_trace_url` is populated on the new job

## Phase 4.6 — Per-attempt triage snapshot ✅ 
- [x] `PatchAttempt` gained `failure_type`, `error_summary`, `failing_file`, `failing_line` columns
- [x] `app/migrations/003_per_attempt_triage.sql` written and applied
- [x] Orchestrator `_post_graph_dispatch` records each attempt's triage output on the attempt row
- [x] `_finalize_failure` passes per-attempt diagnosis into the escalation comment
- [x] `post_escalation_comment` renders each attempt's diagnosis (failure type, summary, file:line) above its patch + CI output sections

