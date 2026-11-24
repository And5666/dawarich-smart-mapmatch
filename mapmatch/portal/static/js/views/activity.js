// Activity: background jobs and the push log.
import { html, useState, useEffect, api, useStore, toast, Icon, full, ago, navigate } from "../lib.js";
import { watchJobs } from "../session.js";

const KIND = { sync: "Sync & match", auto: "Automatic run", import_csv: "CSV import", rematch: "Rematch", push_all: "Push all", revert_all: "Revert all" };
const STATUS = { done: "ok", failed: "danger", cancelled: "", running: "accent", queued: "" };

export function Activity() {
  const version = useStore((s) => s.dataVersion);
  const job = useStore((s) => s.job);
  const [jobs, setJobs] = useState(null);
  const [pushes, setPushes] = useState(null);
  useEffect(() => {
    api("/jobs").then((x) => setJobs(x.jobs)).catch((e) => toast(e.message, "error"));
    api("/pushes").then((x) => setPushes(x.pushes)).catch(() => {});
  }, [version, job?.progress]);

  return html`<div class="content">
    <h1>Activity</h1>
    <p class="lead">What the portal has been doing, and every change it made in Dawarich.</p>
    <div class="stack" style="gap:20px">
      <div class="card"><header><h2>Jobs</h2><p>Syncs, imports and bulk actions. The newest 50 are kept here.</p></header>
        <div class="body table-wrap">${jobs == null ? html`<p class="muted">Loading…</p>` : !jobs.length ? html`<p class="muted">Nothing has run yet.</p>` : html`
          <table class="tbl"><thead><tr><th>Job</th><th>Status</th><th>Started</th><th>Result</th><th></th></tr></thead><tbody>
          ${jobs.map((j) => html`<tr key=${j.id}>
            <td><b>${KIND[j.kind] || j.kind}</b>${j.params?.name ? html`<div class="faint small">${j.params.name}</div>` : ""}</td>
            <td><span class=${`badge ${STATUS[j.status] || ""}`}>${j.status}</span>
              ${(j.status === "running" || j.status === "queued") && html`<div class="progress" style="margin-top:6px;width:110px"><i style=${`width:${j.progress}%`}></i></div>`}</td>
            <td class="num" title=${full(j.created_at)}>${ago(j.started_at || j.created_at)}</td>
            <td>${j.error ? html`<span style="color:var(--danger)">${j.error}</span>` : html`<span class="muted">${j.message}</span>`}</td>
            <td>${(j.status === "running" || j.status === "queued") && html`<button class="sm" onClick=${() => api(`/jobs/${j.id}/cancel`, { method: "POST" }).then(watchJobs)}>Cancel</button>`}</td>
          </tr>`)}</tbody></table>`}</div></div>

      <div class="card"><header><h2>Changes in Dawarich</h2><p>Each push replaces one trip's points with the matched route; each revert puts the original GPS back. Originals are always kept in the portal's archive.</p></header>
        <div class="body table-wrap">${pushes == null ? html`<p class="muted">Loading…</p>` : !pushes.length ? html`<p class="muted">Nothing has been pushed yet.</p>` : html`
          <table class="tbl"><thead><tr><th>When</th><th>Trip</th><th>Action</th><th>Points</th></tr></thead><tbody>
          ${pushes.map((p) => html`<tr key=${p.id}>
            <td class="num" title=${full(p.created_at)}>${ago(p.created_at)}</td>
            <td><a href=${`#/trip/${p.trip_id}`} onClick=${(e) => { e.preventDefault(); navigate(`#/trip/${p.trip_id}`); }}>Trip ${p.trip_id}</a></td>
            <td>${p.kind === "push" ? `Pushed v${p.version}` : "Reverted to original"}${p.dry_run ? html` <span class="badge">dry run</span>` : ""}</td>
            <td class="num muted">${p.kind === "push" ? `${p.posted} written, ${p.deleted} removed` : `${p.posted} restored, ${p.deleted} matched removed`}</td>
          </tr>`)}</tbody></table>`}</div></div>
    </div>
  </div>`;
}
