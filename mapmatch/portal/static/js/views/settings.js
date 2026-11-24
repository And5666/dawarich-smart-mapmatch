// Settings: Dawarich connection, automation, matching, data, danger zone.
import { html, useState, useEffect, useRef, api, useStore, store, toast, Icon, ConfirmButton, ago, full } from "../lib.js";
import { loadMe, startJob, watchJobs } from "../session.js";

export function Settings() {
  const me = useStore((s) => s.me);
  const s = me?.settings || {};
  const [f, setF] = useState(null);
  const [key, setKey] = useState("");
  const [test, setTest] = useState(null);
  const [saving, setSaving] = useState("");
  const [typed, setTyped] = useState("");
  const fileRef = useRef(null);
  useEffect(() => { if (me && !f) setF({ ...s }); }, [me]);
  if (!f) return html`<div class="content muted">Loading…</div>`;
  const upd = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });

  async function save(section, fields) {
    setSaving(section);
    try {
      const body = {};
      for (const k of fields) body[k] = f[k];
      if (section === "connection" && key.trim()) body.api_key = key.trim();
      if ("interval_min" in body) body.interval_min = Number(body.interval_min);
      if ("trip_gap_min" in body) body.trip_gap_min = Number(body.trip_gap_min);
      await api("/settings", { method: "PUT", body });
      setKey("");
      await loadMe();
      setF({ ...store.get().me.settings });
      toast("Saved");
    } catch (e) { toast(e.message, "error"); }
    finally { setSaving(""); }
  }
  async function testConn() {
    setTest({ busy: true });
    try { setTest(await api("/settings/test", { method: "POST" })); } catch (e) { setTest({ ok: false, message: e.message }); }
  }
  async function upload() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setSaving("csv");
    try { await api("/import/csv", { method: "POST", form }); toast("Import started; see the status in the top bar"); watchJobs(); fileRef.current.value = ""; }
    catch (e) { toast(e.message, "error"); }
    finally { setSaving(""); }
  }
  const arch = me.archive || {};

  return html`<div class="content">
    <h1>Settings</h1>
    <p class="lead">Connect Dawarich, decide how much runs on its own, and tune how trips are matched.</p>
    <div class="stack" style="gap:20px">

      <div class="card"><header><h2>Dawarich connection</h2><p>The portal reads your points with this key and, when you push, writes matched trips back to the same account.</p></header>
        <div class="body">
          <label class="field"><span>Dawarich URL</span><input value=${f.dawarich_url} onInput=${upd("dawarich_url")} placeholder=${s.default_dawarich_url} />
            <p class="hint">Leave empty to use the server default (${s.default_dawarich_url}).</p></label>
          <label class="field"><span>API key</span><input type="password" value=${key} onInput=${(e) => setKey(e.target.value)} autocomplete="off"
            placeholder=${s.api_key_set ? `Saved (${s.api_key_hint}). Paste a new key to replace it.` : "From Dawarich → Account → API key"} />
            <p class="hint">Stored on this server only.</p></label>
          ${test && !test.busy && html`<div class=${`callout ${test.ok ? "info" : "danger"}`}><${Icon} name=${test.ok ? "check" : "alert"} size="sm" />${test.message}</div>`}
        </div>
        <div class="foot"><button class="primary" disabled=${saving === "connection"} onClick=${() => save("connection", ["dawarich_url"])}>Save</button>
          <button disabled=${!s.api_key_set || test?.busy} onClick=${testConn}>${test?.busy ? "Testing…" : "Test connection"}</button></div></div>

      <div class="card"><header><h2>Automation</h2><p>On a schedule the portal fetches new points, splits them into trips and matches them. Pushing to Dawarich is a separate choice.</p></header>
        <div class="body">
          <label class="switch"><input type="checkbox" checked=${f.auto_enabled} onChange=${upd("auto_enabled")} />Run automatically</label>
          <div class="grid2">
            <label class="field"><span>Every (minutes)</span><input type="number" min="15" max="10080" value=${f.interval_min} onInput=${upd("interval_min")} /></label>
            <label class="field"><span>Only process data from</span><input type="date" value=${f.start_date} onInput=${upd("start_date")} />
              <p class="hint">Empty means all of it.</p></label>
          </div>
          <label class="switch"><input type="checkbox" checked=${f.auto_push} onChange=${upd("auto_push")} />Push finished trips to Dawarich automatically</label>
          <p class="hint">${f.auto_push ? "Each finished trip's points in Dawarich are replaced by its matched route. Locked trips are skipped, and every original stays in the archive so any trip can be reverted."
            : "Matched trips wait here until you push them, one by one or all at once."}</p>
          ${s.last_error && html`<div class="callout danger"><${Icon} name="alert" size="sm" />Last run failed: ${s.last_error}</div>`}
          <p class="hint">Last run: <span title=${s.last_run ? full(s.last_run) : ""}>${ago(s.last_run)}</span>.</p>
        </div>
        <div class="foot"><button class="primary" disabled=${saving === "auto"} onClick=${() => save("auto", ["auto_enabled", "interval_min", "start_date", "auto_push"])}>Save</button>
          <button disabled=${!s.api_key_set} onClick=${() => startJob("sync")}><${Icon} name="sync" size="sm" />Sync now</button>
          <button onClick=${() => startJob("push_all")} disabled=${!s.api_key_set}><${Icon} name="push" size="sm" />Push all matched trips</button></div></div>

      <div class="card"><header><h2>Matching</h2><p>Changes apply to trips matched from now on. Use “Rematch all” to redo existing ones.</p></header>
        <div class="body">
          <label class="field"><span>A trip ends after</span>
            <div class="row"><input type="number" min="5" max="1440" style="width:110px" value=${f.trip_gap_min} onInput=${upd("trip_gap_min")} /><span class="muted">minutes without movement or data</span></div></label>
          <div class="field"><span class="small" style="font-weight:600;color:var(--muted)">When GPS drops out</span>
            <div class="seg-ctl" role="radiogroup">
              <button class=${f.strength === "fill" ? "on" : ""} role="radio" aria-checked=${f.strength === "fill"} onClick=${() => setF({ ...f, strength: "fill" })}>Route across gaps</button>
              <button class=${f.strength === "strict" ? "on" : ""} role="radio" aria-checked=${f.strength === "strict"} onClick=${() => setF({ ...f, strength: "strict" })}>Only what was recorded</button>
            </div>
            <p class="hint">${f.strength === "fill" ? "Missing stretches are routed along roads and paths when the route fits the time and the fixes around it."
              : "Only short breaks in the matched line are routed; anything with recorded fixes that will not match stays as the original GPS."}</p></div>
        </div>
        <div class="foot"><button class="primary" disabled=${saving === "match"} onClick=${() => save("match", ["trip_gap_min", "strength"])}>Save</button>
          <button onClick=${() => startJob("rematch", { scope: "all" })}><${Icon} name="recalc" size="sm" />Rematch all trips</button></div></div>

      <div class="card"><header><h2>Original GPS archive</h2><p>Every point read from Dawarich or imported is kept here, unchanged. Nothing in the portal deletes from it.</p></header>
        <div class="body">
          <div class="quality"><div class="kpi"><b class="num">${(arch.points || 0).toLocaleString()}</b><span>original points</span></div>
            <div class="kpi"><b class="num">${arch.first ? new Date(arch.first * 1000).toLocaleDateString() : "–"}</b><span>oldest</span></div>
            <div class="kpi"><b class="num">${arch.last ? new Date(arch.last * 1000).toLocaleDateString() : "–"}</b><span>newest</span></div></div>
          <label class="field"><span>Import a CSV export</span><input type="file" accept=".csv,text/csv" ref=${fileRef} />
            <p class="hint">Columns: timestamp, lat, lon, and optionally tracker_id, accuracy, velocity, altitude. Duplicates are skipped.</p></label>
        </div>
        <div class="foot"><button disabled=${saving === "csv"} onClick=${upload}><${Icon} name="upload" size="sm" />Import</button></div></div>

      <div class="card" style="border-color:color-mix(in srgb, var(--danger) 40%, var(--border))"><header><h2>Undo everything in Dawarich</h2>
        <p>Put the original GPS back for every pushed trip and remove all matched points the portal wrote.</p></header>
        <div class="body"><label class="field"><span>Type REVERT to enable</span><input value=${typed} onInput=${(e) => setTyped(e.target.value)} style="max-width:200px" /></label></div>
        <div class="foot"><${ConfirmButton} cls="danger" disabled=${typed !== "REVERT" || !s.api_key_set} confirmLabel="This changes Dawarich. Confirm?"
          onConfirm=${() => { setTyped(""); startJob("revert_all"); }}><${Icon} name="undo" size="sm" />Revert all pushed trips<//></div></div>
    </div>
  </div>`;
}
