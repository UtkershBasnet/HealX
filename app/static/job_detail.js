// Job detail page — renders header, timeline, and attempts.

(function () {
  "use strict";

  const jobId = window.HEALX_JOB_ID;
  const { fmtTime, statusBadge, fetchJSON } = window.HealxUI || {};

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  async function loadHeader() {
    const meta = document.getElementById("job-meta");
    try {
      const j = await fetchJSON(`/jobs/${jobId}`);
      const ciUrl = j.workflow_run_id
        ? `https://github.com/${j.repo_name}/actions/runs/${j.workflow_run_id}`
        : null;
      meta.innerHTML = `
        <span><strong>Repo:</strong> <code>${esc(j.repo_name)}</code></span>
        <span><strong>Branch:</strong> <code>${esc(j.branch_name)}</code></span>
        <span><strong>SHA:</strong> <code>${esc((j.commit_sha || "").slice(0,8))}</code></span>
        <span><strong>Status:</strong> ${statusBadge(j.status)}</span>
        ${j.pr_url ? `<span><a href="${esc(j.pr_url)}" target="_blank" rel="noopener">Pull Request</a></span>` : ""}
        ${ciUrl ? `<span><a href="${ciUrl}" target="_blank" rel="noopener">Original CI run</a></span>` : ""}
        ${j.langfuse_trace_url ? `<span><a href="${esc(j.langfuse_trace_url)}" target="_blank" rel="noopener">Langfuse trace</a></span>` : ""}
      `;
    } catch (e) {
      meta.innerHTML = `<span class="muted">failed to load: ${e.message}</span>`;
    }
  }

  async function loadTimeline() {
    const ol = document.getElementById("timeline");
    try {
      const data = await fetchJSON(`/jobs/${jobId}/timeline`);
      if (!data.events.length) {
        ol.innerHTML = `<li class="muted">no events</li>`;
        return;
      }
      ol.innerHTML = data.events.map((ev) => {
        const classes = ["kind-" + ev.kind];
        if (ev.kind === "ci_result") classes.push(ev.details.conclusion);
        const detail = renderEventDetail(ev);
        return `
          <li class="${classes.join(" ")}">
            <div><span class="ev-kind">${esc(ev.kind)}</span><span class="ev-time">${fmtTime(ev.at)}</span></div>
            <div class="ev-detail">${detail}</div>
          </li>
        `;
      }).join("");
    } catch (e) {
      ol.innerHTML = `<li class="muted">failed to load: ${e.message}</li>`;
    }
  }

  function renderEventDetail(ev) {
    const d = ev.details || {};
    switch (ev.kind) {
      case "job_created":
        return `<code>${esc(d.repo)}</code> @ <code>${esc(d.branch)}</code> · sha <code>${esc(d.sha)}</code>` +
          (d.ci_url ? ` · <a href="${esc(d.ci_url)}" target="_blank" rel="noopener">original CI</a>` : "");
      case "attempt_pushed":
        return `attempt #${d.attempt} · ${d.patch_lines} lines · ` +
          `<em>${esc(d.failure_type || "?")}</em> ${d.failing_file ? `in <code>${esc(d.failing_file)}</code>` + (d.failing_line ? `:${d.failing_line}` : "") : ""}<br>` +
          `<span class="muted">${esc(d.error_summary || "")}</span>`;
      case "ci_result":
        return `attempt #${d.attempt} → <strong>${esc(d.conclusion)}</strong>` +
          (d.ci_url ? ` · <a href="${esc(d.ci_url)}" target="_blank" rel="noopener">GitHub Actions run</a>` : "");
      case "pr_opened":
        return `<a href="${esc(d.pr_url)}" target="_blank" rel="noopener">${esc(d.pr_url)}</a>` +
          (d.clean_branch ? ` · branch <code>${esc(d.clean_branch)}</code>` : "");
      case "terminal":
        return `<strong>${esc(d.status)}</strong>${d.message ? ` · ${esc(d.message)}` : ""}`;
      default:
        return `<pre>${esc(JSON.stringify(d, null, 2))}</pre>`;
    }
  }

  async function loadAttempts() {
    const div = document.getElementById("attempts");
    try {
      const attempts = await fetchJSON(`/jobs/${jobId}/attempts`);
      if (!attempts.length) {
        div.innerHTML = `<p class="muted">no attempts yet</p>`;
        return;
      }
      div.innerHTML = attempts.map((a) => `
        <details>
          <summary>
            Attempt #${a.attempt_number} ·
            ${a.success ? "<strong style='color:var(--green)'>passed CI</strong>" : "<strong style='color:var(--red)'>failed</strong>"} ·
            ${esc(a.failure_type || "?")}
            ${a.github_run_url ? `· <a href="${esc(a.github_run_url)}" target="_blank" rel="noopener">CI run</a>` : ""}
          </summary>
          <div>
            <p>${esc(a.error_summary || "")}${a.failing_file ? ` · <code>${esc(a.failing_file)}${a.failing_line ? ":" + a.failing_line : ""}</code>` : ""}</p>
            <strong>Patch</strong>
            <pre>${esc(a.patch_diff || "(no diff)")}</pre>
            ${a.ci_output ? `<strong>CI output</strong><pre>${esc(a.ci_output)}</pre>` : ""}
          </div>
        </details>
      `).join("");
    } catch (e) {
      div.innerHTML = `<p class="muted">failed to load: ${e.message}</p>`;
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    loadHeader();
    loadTimeline();
    loadAttempts();
  });
})();
