// Activity: what ran, and every change made in Dawarich.
import { html, useState, useEffect, api, useStore, toast, full, ago, navigate } from "../lib.js";
import { watchJobs } from "../session.js";

const KIND = { sync: "Sync", auto: "Automatic run", import_csv: "CSV import", rematch: "Matching", push_all: "Push", revert_all: "Revert all", repair: "Repair tracks", reset: "Start again" };
const CHANGE = {
  push: (p) => `Pushed version ${p.version}`,
  revert: () => "Put original GPS back",
  incomplete: () => "Stopped halfway (points missing in Dawarich)",
  "incomplete-repaired": () => "Stopped halfway, since repaired",
};
const STATUS = { done: "", failed: "color:var(--bad)", cancelled: "color:var(--ink-3)", running: "", queued: "color:var(--ink-3)" };

export function Activity() {
  const version = useStore((s) => s.dataVersion);
  const job = useStore((s) => s.job);
  const [jobs, setJobs] = useState(null);
  const [pushes, setPushes] = useState(null);
  useEffect(() => {
    api("/jobs").then((x) => setJobs(x.jobs)).catch((e) => toast(e.message, "bad"));
    api("/pushes").then((x) => setPushes(x.pushes)).catch(() => {});
  }, [version, job?.progress]);

  return html`<div class="doc">
    <h1>Activity</h1>
    <p class="lede">What has run, and every change made in your Dawarich account.</p>
    <section class="full">
      <header><h2>Runs</h2></header>
      <div class="scroll-x">${jobs == null ? html`<p class="help">Loading…</p>` : !jobs.length ? html`<p class="help">Nothing has run yet.</p>` : html`
        <table class="t"><thead><tr><th>What</th><th>When</th><th>Result</th><th></th></tr></thead><tbody>
        ${jobs.map((j) => html`<tr key=${j.id}>
          <td>${KIND[j.kind] || j.kind}${j.params?.name ? html`<div class="faint small">${j.params.name}</div>` : ""}</td>
          <td class="num" title=${full(j.created_at)}>${ago(j.started_at || j.created_at)}</td>
          <td style=${STATUS[j.status] || ""}>${j.error || (j.status === "running" ? `${j.message} (${j.progress}%)` : j.status === "done" ? j.message : j.status)}</td>
          <td>${(j.status === "running" || j.status === "queued") && html`<button class="sm" onClick=${() => api(`/jobs/${j.id}/cancel`, { method: "POST" }).then(watchJobs)}>Stop</button>`}</td>
        </tr>`)}</tbody></table>`}</div>
    </section>
    <section class="full">
      <header><h2>Changes in Dawarich</h2><p>A push replaces a trip's points with its matched route; a revert puts the original GPS back.</p></header>
      <div class="scroll-x">${pushes == null ? html`<p class="help">Loading…</p>` : !pushes.length ? html`<p class="help">Nothing has been pushed yet.</p>` : html`
        <table class="t"><thead><tr><th>When</th><th>Trip</th><th>Change</th><th>Points</th></tr></thead><tbody>
        ${pushes.map((p) => html`<tr key=${p.id}>
          <td class="num" title=${full(p.created_at)}>${ago(p.created_at)}</td>
          <td>${p.trip_id > 0 ? html`<a href=${`#/trip/${p.trip_id}`} onClick=${(e) => { e.preventDefault(); navigate(`#/trip/${p.trip_id}`); }}>Open trip</a>` : html`<span class="faint">–</span>`}</td>
          <td>${(CHANGE[p.kind] || (() => p.kind))(p)}${p.dry_run ? html` <span class="faint">(dry run)</span>` : ""}</td>
          <td class="num dim">${p.detail?.matched_points != null ? `${p.detail.matched_points} matched, ${p.detail.original_points} original, ${p.deleted} replaced` : `${p.posted} written, ${p.deleted} removed`}</td>
        </tr>`)}</tbody></table>`}</div>
    </section>
  </div>`;
}
