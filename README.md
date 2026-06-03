# HealX

**Autonomous CI repair orchestrator.** HealX listens for failed GitHub Actions runs, diagnoses the failure with an LLM, generates a minimal patch, and ships it as a clean pull request — but only after the patch has actually passed CI on GitHub Actions itself.

Verification is real CI, not a local simulation. There is no Docker sandbox replaying workflow YAML; HealX commits to a hidden internal branch, watches the workflow_run webhooks, and either ships a PR (CI green) or retries up to 3 times (CI red).

---

## Why

Most CI failures are repetitive and mechanically fixable, but engineers still spend hours reading logs, patching, pushing, and waiting on reruns. HealX automates that loop.

What it does **not** do:
- Reproduce CI locally
- Emulate GitHub Actions runners
- Open PRs that haven't been verified by the real CI

What it does:
- React to `workflow_run.failed` webhooks
- Run a 2-agent pipeline (triage → repair) per attempt
- Push each attempt to a hidden `healx/internal/run-{job_id}` branch
- Let GitHub Actions verify, then either open a clean PR or retry with the new logs
- Trace every LLM call to Langfuse, surface stats and a per-job timeline through a built-in dashboard at `/dashboard`

---

## Architecture

```
                       ┌────────────────────────────────┐
   GitHub Actions ────▶│  workflow_run.failed webhook   │
   (developer branch)  │  POST /webhook/github          │
                       └──────────────┬─────────────────┘
                                      │ (signature-validated)
                                      ▼
                       ┌────────────────────────────────┐
                       │  webhook/router.py             │
                       │  - idempotency on run_id       │
                       │  - create RepairJob            │
                       │  - enqueue RQ job              │
                       └──────────────┬─────────────────┘
                                      │
                                      ▼
                       ┌────────────────────────────────┐
                       │  RQ worker:                    │
                       │  handle_initial_failure(id)    │
                       │                                │
                       │  1. fetch CI logs              │
                       │  2. graph: triage → repair     │
                       │  3. push patch to              │
                       │     healx/internal/run-{id}    │
                       │  4. mark status=repairing      │
                       └──────────────┬─────────────────┘
                                      │
                                      │   (GitHub Actions reruns CI
                                      │    on the internal branch)
                                      ▼
                       ┌────────────────────────────────┐
                       │  workflow_run.completed        │
                       │  on healx/internal/run-*       │
                       │  → webhook fires again         │
                       └──────────────┬─────────────────┘
                                      ▼
                       ┌────────────────────────────────┐
                       │  handle_internal_branch_       │
                       │  completion(id, conclusion)    │
                       │                                │
                       │  ┌─ success → squash internal  │
                       │  │   tree onto healx/fix-...   │
                       │  │   open PR to dev branch     │
                       │  │   status=pr_opened          │
                       │  │                             │
                       │  ├─ failure + retries < 3 →    │
                       │  │   fetch new logs, re-run    │
                       │  │   graph w/ prev patch +     │
                       │  │   new logs, push next       │
                       │  │   attempt, status=retrying  │
                       │  │                             │
                       │  └─ failure + retries ≥ 3 →    │
                       │      escalation comment,       │
                       │      status=failed             │
                       └────────────────────────────────┘
```

### Key invariants

- Internal branches are always force-pushed off the **exact failing SHA**, never off `main`. Each attempt is one commit; no accumulation.
- The retry loop is driven by **incoming webhook events**, not by polling or by the graph looping internally.
- Developers never see internal branches. They only see one clean PR with one squashed commit (or an escalation comment).
- Duplicate webhook deliveries cannot fire duplicate jobs (Redis NX-set + DB unique constraint on `workflow_run_id`).

---

## How it works in detail

### The agent graph (`app/agents/graph.py`)

Strictly two nodes:

```
START → triage → repair → END
            ↓        ↓
   undiagnosable  cannot_fix
```

- **triage** reads truncated CI logs, returns structured JSON: `failure_type`, `failing_file`, `failing_line`, `error_summary`, `relevant_files`, `error_snippet`.
- **repair** reads the failing file (plus up to 4 relevant ones) from GitHub at the failing SHA, generates a unified diff bounded by safety constraints (≤3 files, ≤50 lines changed, never in `infra/`, `migrations/`, `secrets/`, `.github/`).
- On retry, the orchestrator primes the repair agent with the **previous patch** and the **new CI logs** so it can see exactly how its last attempt broke.

State (retry count, current internal branch, last triage snapshot) lives in Postgres between graph invocations — never in the graph itself.

### Git operations (`app/pipeline/git_ops.py`)

Two operations:

- `push_patch_to_internal_branch(repo, base_sha, job_id, patch_diff, ...)` — clone, branch from `base_sha`, apply patch (`git apply --3way` fallback), force-push to `healx/internal/run-{job_id}`. Returns the new commit SHA.
- `open_clean_fix_pr(repo, internal_branch, base_branch, base_sha, ...)` — clone, branch from `base_sha`, use `git read-tree --reset` to stage the entire internal-branch tree, commit once with a clean message, force-push to `healx/fix-{slug}-{short_id}`, open PR back to the developer branch.

The squash via `read-tree` is deliberate: it produces a single commit containing exactly the green tree, regardless of how messy the internal branch history is.

### Orchestrator (`app/pipeline/orchestrator.py`)

Two RQ entry points, both fired by webhooks:

| Function | Triggered by | What it does |
|---|---|---|
| `handle_initial_failure(job_id)` | First `workflow_run.failed` on developer branch | Fetch logs, run graph, push attempt #1, set `status=repairing` |
| `handle_internal_branch_completion(job_id, ci_conclusion, new_run_id)` | `workflow_run.completed` on `healx/internal/*` | success → open clean PR; failure + retries left → next attempt; failure + exhausted → escalate |

### Webhook router (`app/webhook/router.py`)

Single endpoint `POST /webhook/github`. Differentiates events by `head_branch`:

- `healx/internal/run-*` → look up the existing job by `current_internal_branch`, route to the completion handler
- Everything else → developer-branch path; on `conclusion=failure`, create a new job (idempotent via Redis lock + DB unique constraint)

---

## Getting started

### Prerequisites

- Docker + Docker Compose
- A GitHub PAT or App token with repo write access (used by the bot to push internal/clean branches and open PRs)
- An Anthropic API key (Claude Opus 4.7)
- A public webhook URL pointing at this app (use `ngrok` or similar for local dev)

### Setup (fresh install)

```bash
cp .env.example .env   # then edit with your credentials
docker compose build
docker compose up
```

That's it. On first boot the FastAPI lifespan calls `Base.metadata.create_all()` against an empty database, which creates `repair_jobs` and `patch_attempts` with the current schema — no migrations needed. The app listens on `:8000`, the RQ worker pulls from the `healx-jobs` queue, and the dashboard is at `http://localhost:8000/dashboard`.

The SQL migration files under `app/migrations/` only matter when **upgrading an existing database** in place — see [Migrations](#migrations) below.

### Configure the GitHub webhook

On the repo (or org) you want HealX to repair:

1. Settings → Webhooks → Add webhook
2. Payload URL: `https://<your-public-host>/webhook/github`
3. **Content type:** `application/json` &nbsp;— GitHub defaults to `application/x-www-form-urlencoded`; the router accepts both but JSON is the recommended setting.
4. Secret: same value as `GITHUB_WEBHOOK_SECRET` in `.env`
5. Events: select **Workflow runs**

GitHub will fire a `ping` event when you save the webhook — the router returns 200 with the `zen` field and the GitHub UI shows a green check. If you see a 401, the secret doesn't match; if you see a 400, the body wasn't valid JSON (check the content-type setting).

### Smoke test

```bash
./test_webhook.sh
```

Sends a signed mock `workflow_run.failed` payload, confirms the webhook validates, creates a job, and enqueues it. Useful for confirming the webhook + queue plumbing without burning real CI minutes.

---

## Configuration

All settings load from `.env` via `pydantic-settings`. The model has `extra="ignore"` so stale variables don't break boot.

| Variable | Purpose |
|---|---|
| `GITHUB_TOKEN` | Bot PAT — used for clone/push/PR/comment operations |
| `GITHUB_WEBHOOK_SECRET` | HMAC-SHA256 secret shared with the GitHub webhook |
| `API_KEY` | Anthropic API key (Claude Opus 4.7) |
| `DATABASE_URL` | Async URL, e.g. `postgresql+asyncpg://healx:healx@db:5432/healx` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | Langfuse tracing for agent LLM calls. Leave empty to disable cleanly. |
| `APP_ENV` | `development` or `production` (production skips `create_all` on startup) |
| `LOG_LEVEL` | `INFO` (default), `DEBUG`, etc. |

---

## Project layout

```
healX/
├── app/
│   ├── main.py                 — FastAPI app, lifespan, /health, dashboard routes
│   ├── worker.py               — RQ worker entry point
│   ├── config.py               — pydantic-settings, .env-driven
│   │
│   ├── agents/
│   │   ├── graph.py            — LangGraph: triage → repair (no internal retry)
│   │   ├── triage.py           — CI logs → structured failure JSON
│   │   └── repair.py           — Triage output + source → unified diff
│   │
│   ├── pipeline/
│   │   ├── orchestrator.py     — handle_initial_failure + handle_internal_branch_completion
│   │   ├── git_ops.py          — push_patch_to_internal_branch + open_clean_fix_pr
│   │   └── github_client.py    — Thin wrapper around PyGithub + httpx
│   │
│   ├── webhook/
│   │   ├── router.py           — POST /webhook/github, routes by branch prefix
│   │   └── validator.py        — HMAC-SHA256 signature validation
│   │
│   ├── api/
│   │   ├── jobs.py             — GET/POST /jobs* with status/repo/failure_type/since filters
│   │   ├── stats.py            — GET /stats (totals, fix_rate, top_repos, failure_types)
│   │   └── timeline.py         — GET /jobs/{id}/timeline (reconstructed event log)
│   │
│   ├── observability/
│   │   └── langfuse_client.py  — Langfuse handler factory + flush; no-op when keys unset
│   │
│   ├── models/
│   │   ├── db.py               — SQLAlchemy ORM (RepairJob, PatchAttempt, PatchFeedback)
│   │   └── schemas.py          — Pydantic request/response schemas
│   │
│   ├── templates/              — Jinja2 templates for /dashboard
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   └── job_detail.html
│   │
│   ├── static/                 — CSS + vanilla-JS polling for the dashboard
│   │   ├── dash.css            — light + auto dark mode
│   │   ├── dash.js             — /stats polling, /jobs filtering
│   │   └── job_detail.js       — timeline + attempts rendering
│   │
│   └── migrations/
│       ├── 002_realignment.sql        — Pivot to webhook-driven verification
│       ├── 003_per_attempt_triage.sql — Triage snapshot per attempt
│       └── 004_observability.sql      — langfuse_trace_url column
│
├── docker-compose.yml         — db (Postgres 16) + redis + app + worker
├── Dockerfile
├── requirements.txt
├── test_webhook.sh            — Local webhook smoke test
├── phase5_plan.md             — Phase 5 design doc (observability)
├── changeinplan.md            — Architectural pivot rationale (Phase 4.5)
└── README.md
```

---

## Data model

### `repair_jobs` — one row per CI failure intercepted

| Column | Purpose |
|---|---|
| `id` | UUID primary key |
| `repo_name`, `branch_name`, `commit_sha` | Failing context |
| `workflow_run_id` | **UNIQUE** — idempotency anchor for webhook deliveries |
| `status` | `queued` → `repairing` → `retrying` → `pr_opened \| failed \| undiagnosable` |
| `retry_count` | 0..3 |
| `current_internal_branch` | `healx/internal/run-{id}` — what to look up on the next internal-branch webhook |
| `final_clean_branch` | `healx/fix-{slug}-{short}` — set when PR is opened |
| `failure_type`, `error_summary`, `failing_file`, `failing_line` | Latest triage understanding — used in the success-path PR body |
| `pr_url` | Set when the clean PR is opened |
| `langfuse_trace_url` | Deterministic deep link to the agent trace (`{LANGFUSE_HOST}/trace/healx-job-{id}`); null when Langfuse is disabled |
| `error_message` | Final error text on terminal-failure paths |

### `patch_attempts` — one row per repair attempt (max 3 per job)

| Column | Purpose |
|---|---|
| `attempt_number` | 1, 2, or 3 |
| `success` | True only when CI passes on the internal branch |
| `patch_diff` | The unified diff produced by the repair agent for this attempt |
| `failure_type`, `error_summary`, `failing_file`, `failing_line` | **Per-attempt** triage snapshot (different retries can diagnose differently) |
| `internal_branch`, `internal_commit_sha` | What was pushed |
| `ci_run_id`, `ci_output` | Stamped when the resulting workflow_run.completed arrives |

### `patch_feedback` — engineer signals on PRs

Stores `ACCEPT | NACK | PARTIAL_NACK | SKIP` for future evaluation work.

---

## Migrations

There is no Alembic. Migrations live in `app/migrations/NNN_*.sql` and are applied manually. They exist for one purpose: **upgrading an existing database that was running an older version of the schema**. On a fresh install you do not need to run them — `Base.metadata.create_all()` creates the current schema directly.

| File | What it changes | When you need to run it |
|---|---|---|
| `002_realignment.sql` | Post-pivot columns on `repair_jobs` (`current_internal_branch`, `final_clean_branch`, persisted triage fields), unique constraint on `workflow_run_id`, retires the sandbox-era `PatchAttempt.sandbox_output` column and replaces it with CI-driven columns. Migrates pre-pivot rows off retired status values. | You have rows from a pre-pivot HealX install. |
| `003_per_attempt_triage.sql` | Adds `failure_type` / `error_summary` / `failing_file` / `failing_line` to `patch_attempts` so the escalation comment can render a per-attempt diagnosis. | You ran HealX before this column landed. |
| `004_observability.sql` | Adds `repair_jobs.langfuse_trace_url` for deep-linking from the API/dashboard into agent traces. | You ran HealX before Langfuse integration. |

Apply them in numeric order:

```bash
docker compose exec -T db psql -U healx -d healx < app/migrations/002_realignment.sql
docker compose exec -T db psql -U healx -d healx < app/migrations/003_per_attempt_triage.sql
docker compose exec -T db psql -U healx -d healx < app/migrations/004_observability.sql
```

All migrations are idempotent (`ALTER TABLE ... IF NOT EXISTS`, `DO $$ ... $$` constraint guards) so re-running them is safe. They are *not* idempotent against an empty DB — they require the base tables to already exist, which `create_all()` handles on first app boot.

### Why three files instead of one consolidated schema?

Each migration encodes a real architectural step in the project's history. `002` is the Docker-sandbox-to-webhook-driven pivot. `003` is the per-attempt triage refinement. `004` is the observability layer landing. Keeping them separate preserves that narrative; consolidating them would lose it. The trade-off is one extra `psql` invocation per upgrade — acceptable.

---

## API surface

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook/github` | GitHub webhook receiver (signature-validated) |
| `GET` | `/health` | Liveness |
| `GET` | `/jobs` | Paginated list of repair jobs. Filters: `status`, `repo`, `failure_type`, `since` (ISO-8601), plus `page` / `per_page`. |
| `GET` | `/jobs/{id}` | Single job status (includes `langfuse_trace_url`) |
| `GET` | `/jobs/{id}/attempts` | Patch attempts for a job, enriched with `github_run_url` + `langfuse_trace_url` |
| `GET` | `/jobs/{id}/timeline` | Chronological event log (reconstructed from existing rows) |
| `GET` | `/stats` | Totals by status + fix_rate + mean attempts-to-pr + mean seconds-to-pr + top 5 repos + failure-type distribution |
| `POST` | `/jobs/{id}/retry` | Manually re-trigger initial repair (only for terminal states) |
| `GET` | `/dashboard` | Operator dashboard (status strip, filters, recent jobs) |
| `GET` | `/dashboard/jobs/{id}` | Per-job detail page (header, timeline, attempts) |

Quick examples:

```bash
# Filtered list
curl -s "http://localhost:8000/jobs?status=pr_opened&repo=owner/repo&since=2026-05-01" | jq

# Operator overview
curl -s http://localhost:8000/stats | jq

# Timeline for one job
curl -s http://localhost:8000/jobs/<uuid>/timeline | jq
```

---

## Observability

### Langfuse traces

When `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set, every Claude call from the triage and repair agents is traced. Identity is:

- `trace_id` = `healx-job-{job_uuid}` — stable across all retries for one job
- `session_id` = `{repo}:{branch}` — filter by source repo + branch in the UI
- `user_id` = `{repo}` — operator-friendly facet
- `tags` = `["healx", "attempt-N", failure_type]`

The deterministic trace URL is stored on `RepairJob.langfuse_trace_url` and surfaced in the API + dashboard. Leaving the keys empty disables Langfuse cleanly — agents still run, no errors.

All glue lives in `app/observability/langfuse_client.py`. The LangChain `CallbackHandler` is the only integration point; agents pass it via `llm.invoke(messages, config={"callbacks": [...]})`.

### Dashboard

`http://localhost:8000/dashboard` is a server-rendered Jinja2 page plus ~150 lines of vanilla JS:

- **Status strip** — counts by status + fix rate, polled every 5s from `/stats`
- **Filters** — status / repo / failure type / since-date, calls `/jobs?...`
- **Recent-jobs table** — refreshed every 10s, links into per-job detail
- **Job detail** — header (with PR link + Langfuse trace link + original CI run link), reconstructed timeline, per-attempt collapsible diff + CI output

No framework, no build step. Dark mode follows `prefers-color-scheme`. If you ever expose this beyond localhost, add auth.

### Stats vocabulary

The `/stats` endpoint exposes everything the dashboard needs and is also useful for ad-hoc operator queries via `curl + jq`:

```jsonc
{
  "totals": { "queued": 0, "repairing": 1, "retrying": 0, "pr_opened": 24, "failed": 3, ... },
  "fix_rate": { "attempted": 27, "succeeded": 24, "rate": 0.8889 },
  "mean_attempts_to_pr": 1.42,
  "mean_seconds_to_pr": 187.4,
  "top_repos": [ { "repo": "owner/repo", "count": 18 }, ... ],   // top 5
  "failure_types": [ { "type": "TestFailure", "count": 14 }, ... ]
}
```

---

## Limitations

- The repair agent is bounded to ≤3 files and ≤50 lines per attempt. Complex, cross-cutting fixes won't pass these limits and will escalate.
- Internal branches share a name with the job ID, so once a job hits a terminal state its internal branch is preserved on GitHub. Cleanup is intentionally out of scope here — internal branches make great forensics if HealX gets a fix wrong.
- Anthropic-only. Other model providers would require swapping `ChatAnthropic` in `triage.py` and `repair.py`.
- Single concurrent repair per `workflow_run_id`, not per repo. Two genuinely independent failures on the same repo will both be handled in parallel.
- The `/jobs/{id}/timeline` endpoint reconstructs events from existing columns — intermediate `retrying ↔ repairing` flips are lossy. If you need a full audit log, add a `repair_job_events(id, job_id, at, kind, details JSONB)` table and have the orchestrator append on every status change.
- The dashboard is unauthenticated. Sit it behind an auth proxy before exposing it.

---

## License

Not specified. Treat as proprietary until a license is added.
