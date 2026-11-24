// Settings: automation, matching, import, originals, undo-everything, system.
import { html, useState, useEffect, useRef, api, useStore, store, toast, Icon, Confirm, ago, full, plural } from "../lib.js";
import { loadMe, startJob, watchJobs } from "../session.js";

export function Settings() {
  const me = useStore((s) => s.me);
  const s = me?.settings || {};
  const [f, setF] = useState(null);
  const [saving, setSaving] = useState("");
  const [typed, setTyped] = useState("");
  const [sys, setSys] = useState(null);
  const fileRef = useRef(null);
  useEffect(() => { if (me && !f) setF({ ...s }); }, [me]);
  useEffect(() => { api("/system").then(setSys).catch(() => {}); }, []);
  if (!f) return html`<div class="doc faint">Loading…</div>`;
  const upd = (k) => (e) => setF({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const dirty = (keys) => keys.some((k) => String(f[k]) !== String(s[k]));

  async function save(section, keys) {
    setSaving(section);
    try {
      const body = {};
      for (const k of keys) body[k] = f[k];
      for (const k of ["interval_min", "trip_gap_min"]) if (k in body) body[k] = Number(body[k]);
      await api("/settings", { method: "PUT", body });
      await loadMe();
      setF({ ...store.get().me.settings });
      toast("Saved");
    } catch (e) { toast(e.message, "bad"); }
    finally { setSaving(""); }
  }
  async function upload() {
    const file = fileRef.current?.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setSaving("csv");
    try { await api("/import/csv", { method: "POST", form }); toast("Importing; progress is in the top bar"); watchJobs(); fileRef.current.value = ""; }
    catch (e) { toast(e.message, "bad"); }
    finally { setSaving(""); }
  }
  const arch = me.archive || {};
  const host = (sys?.dawarich || "").replace(/^https?:\/\//, "");

  return html`<div class="doc">
    <h1>Settings</h1>
    <p class="lede">Signed in as <b>${me.name}</b>${host ? html` on ${host}` : ""}.</p>

    <section>
      <header><h2>Automatic runs</h2><p>Off by default. Nothing is pushed to Dawarich unless you turn that on too.</p></header>
      <div class="body">
        <label class="check"><input type="checkbox" checked=${f.auto_enabled} onChange=${upd("auto_enabled")} />Sync and match new trips on a schedule</label>
        <div class="row" style="flex-wrap:wrap;gap:16px">
          <label class="field"><span>Every</span><div class="row"><input type="number" min="15" max="10080" style="width:90px" value=${f.interval_min} onInput=${upd("interval_min")} /><span class="dim">minutes</span></div></label>
          <label class="field"><span>Ignore data before</span><input type="date" style="width:170px" value=${f.start_date} onInput=${upd("start_date")} /></label>
        </div>
        <label class="check"><input type="checkbox" checked=${f.auto_push} onChange=${upd("auto_push")} />Also push finished trips to Dawarich</label>
        <p class="help">${f.auto_push ? "Each finished trip's points in Dawarich are replaced with its matched route. Locked trips are skipped, and your original GPS stays here so any trip can be put back."
          : "Matched trips stay here until you push them."}</p>
        ${s.last_error && html`<div class="note bad">The last run failed: ${s.last_error}</div>`}
        <div class="actions"><button class="go" disabled=${saving === "auto" || !dirty(["auto_enabled", "interval_min", "start_date", "auto_push"])}
          onClick=${() => save("auto", ["auto_enabled", "interval_min", "start_date", "auto_push"])}>Save</button>
          <span class="help" style="align-self:center">Last run ${ago(s.last_run)}</span></div>
      </div>
    </section>

    <section>
      <header><h2>Matching</h2><p>Applies to trips matched from now on.</p></header>
      <div class="body">
        <label class="field"><span>A trip ends after a stop or a gap of</span>
          <div class="row"><input type="number" min="5" max="1440" style="width:90px" value=${f.trip_gap_min} onInput=${upd("trip_gap_min")} /><span class="dim">minutes</span></div></label>
        <div class="field"><span>When the GPS drops out</span>
          <div class="seg" role="radiogroup">
            <button class=${f.strength === "fill" ? "on" : ""} role="radio" aria-checked=${f.strength === "fill"} onClick=${() => setF({ ...f, strength: "fill" })}>Route across the gap</button>
            <button class=${f.strength === "strict" ? "on" : ""} role="radio" aria-checked=${f.strength === "strict"} onClick=${() => setF({ ...f, strength: "strict" })}>Keep what was recorded</button>
          </div>
          <p class="help">${f.strength === "fill" ? "Missing stretches follow the road or path when the timing and the points around them agree."
            : "Only tiny breaks are routed. Anything else that won't match stays as your original GPS."}</p></div>
        <p class="help">Walks and rides only snap to a path when they clearly follow it; off-trail stretches keep your GPS. Bikes ignore one-way streets.</p>
        <div class="actions"><button class="go" disabled=${saving === "match" || !dirty(["trip_gap_min", "strength"])} onClick=${() => save("match", ["trip_gap_min", "strength"])}>Save</button>
          <button onClick=${() => startJob("rematch", { scope: "all" })}>Rematch every trip</button></div>
      </div>
    </section>

    <section>
      <header><h2>Your original GPS</h2><p>Every point read from Dawarich is kept here unchanged. Nothing here ever deletes it.</p></header>
      <div class="body">
        <div class="stat"><div><b class="num">${(arch.points || 0).toLocaleString()}</b><span>points kept</span></div>
          <div><b class="num">${arch.first ? new Date(arch.first * 1000).toLocaleDateString() : "–"}</b><span>oldest</span></div>
          <div><b class="num">${arch.last ? new Date(arch.last * 1000).toLocaleDateString() : "–"}</b><span>newest</span></div></div>
        <label class="field"><span>Import a CSV export</span><input type="file" accept=".csv,text/csv" ref=${fileRef} />
          <p class="help">Columns: timestamp, lat, lon, and optionally accuracy, velocity, altitude. Points already here are skipped.</p></label>
        <div class="actions"><button disabled=${saving === "csv"} onClick=${upload}>Import</button></div>
      </div>
    </section>

    <section>
      <header><h2>Undo everything</h2><p>Puts the original GPS back in Dawarich for every trip you pushed.</p></header>
      <div class="body">
        <label class="field"><span>Type <b>REVERT</b> to enable</span><input value=${typed} onInput=${(e) => setTyped(e.target.value)} style="max-width:160px" /></label>
        <div class="actions"><${Confirm} cls="danger" disabled=${typed !== "REVERT"} armedLabel="This changes Dawarich. Sure?"
          onConfirm=${() => { setTyped(""); startJob("revert_all"); }}>Revert all pushed trips<//></div>
      </div>
    </section>

    <section>
      <header><h2>System</h2><p>The services this server uses.</p></header>
      <div class="body">
        ${!sys ? html`<p class="help">Checking…</p>` : html`
          ${sys.valhalla?.error ? html`<div class="note bad">The map-matching service (Valhalla) is unreachable: ${sys.valhalla.error}</div>`
            : html`<div class="note good">Map matching (Valhalla ${sys.valhalla?.version}) is up${sys.valhalla?.tileset_last_modified ? `; map data from ${new Date(sys.valhalla.tileset_last_modified * 1000).toLocaleDateString()}` : ""}.</div>`}
          ${sys.dry_run && html`<div class="note warn">Dry run is on (PUSH_DRY_RUN): pushes are planned and logged, but Dawarich is not changed.</div>`}
          <p class="help">Matcher version ${sys.engine}.${sys.outdated_trips ? ` ${plural(sys.outdated_trips, "trip")} were matched by an older version.` : ""}</p>
          ${sys.outdated_trips > 0 && html`<div class="actions"><button onClick=${() => startJob("rematch", { scope: "outdated" })}>Rematch them</button></div>`}`}
      </div>
    </section>
  </div>`;
}
