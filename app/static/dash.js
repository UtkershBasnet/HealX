// HealX dashboard — vanilla JS polling, no framework.

(function () {
  "use strict";

  const POLL_MS = 5000;

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleString(undefined, {
        month: "short", day: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch { return iso; }
  }

  function shortSha(sha) {
    return sha ? sha.slice(0, 8) : "—";
  }

  function statusBadge(status) {
    const s = (status || "").replace(/[^a-z0-9_-]/gi, "");
    return `<span class="status-badge ${s}">${status || "—"}</span>`;
  }

  async function fetchJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return r.json();
  }

  // ─── Stats strip ─────────────────────────────────────────────────
  async function pollStats() {
    try {
      const stats = await fetchJSON("/stats");
      document.querySelectorAll(".stat-chip[data-bucket]").forEach((chip) => {
        const bucket = chip.dataset.bucket;
        const n = stats.totals[bucket] ?? 0;
        chip.querySelector(".count").textContent = n;
      });
      const fixChip = document.getElementById("fix-rate-chip");
      if (fixChip && stats.fix_rate) {
        const pct = Math.round(stats.fix_rate.rate * 100);
        fixChip.querySelector(".count").textContent = `${pct}%`;
      }
    } catch (e) {
      console.warn("stats poll failed:", e);
    } finally {
      setTimeout(pollStats, POLL_MS);
    }
  }

  // ─── Jobs table ──────────────────────────────────────────────────
  function buildJobsUrl() {
    const status = document.getElementById("filter-status")?.value || "";
    const repo = document.getElementById("filter-repo")?.value?.trim() || "";
    const failureType = document.getElementById("filter-failure-type")?.value?.trim() || "";
    const since = document.getElementById("filter-since")?.value || "";

    const params = new URLSearchParams({ per_page: "50" });
    if (status) params.set("status", status);
    if (repo) params.set("repo", repo);
    if (failureType) params.set("failure_type", failureType);
    if (since) params.set("since", since);
    return `/jobs?${params.toString()}`;
  }

  async function loadJobs() {
    const tbody = document.getElementById("jobs-tbody");
    if (!tbody) return;
    try {
      const data = await fetchJSON(buildJobsUrl());
      if (!data.jobs.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="muted">no jobs match these filters</td></tr>`;
        return;
      }
      tbody.innerHTML = data.jobs.map((j) => `
        <tr>
          <td class="muted">${fmtTime(j.created_at)}</td>
          <td><code>${j.repo_name}</code></td>
          <td><code>${j.branch_name}</code></td>
          <td>${statusBadge(j.status)}</td>
          <td>${j.failure_type ? `<code>${j.failure_type}</code>` : "—"}</td>
          <td>${(j.retry_count || 0) + (["pr_opened", "failed", "needs-human-review"].includes(j.status) ? 1 : 0)}</td>
          <td>${j.pr_url ? `<a href="${j.pr_url}" target="_blank" rel="noopener">PR</a>` : "—"}</td>
          <td><a href="/dashboard/jobs/${j.id}">view</a></td>
        </tr>
      `).join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="muted">failed to load: ${e.message}</td></tr>`;
    }
  }

  function bindFilterButtons() {
    const apply = document.getElementById("apply-filters");
    const clear = document.getElementById("clear-filters");
    if (apply) apply.addEventListener("click", loadJobs);
    if (clear) {
      clear.addEventListener("click", () => {
        ["filter-status", "filter-repo", "filter-failure-type", "filter-since"].forEach((id) => {
          const el = document.getElementById(id);
          if (el) el.value = "";
        });
        loadJobs();
      });
    }
  }

  // ─── Boot ────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", () => {
    // The dashboard index page has these elements; the job-detail page does not.
    if (document.getElementById("stats-strip")) pollStats();
    if (document.getElementById("jobs-tbody")) {
      bindFilterButtons();
      loadJobs();
      setInterval(loadJobs, POLL_MS * 2);  // refresh jobs every 10s
    }
  });

  // Expose for the detail page
  window.HealxUI = { fmtTime, shortSha, statusBadge, fetchJSON };
})();
