// Settings: your Dawarich link, automation, matching, the original GPS, putting
// Dawarich right, and what this server runs on.
import { html, useState, useEffect, useRef, api, useStore, store, toast, Confirm, ago, plural } from "../lib.js";
import { loadMe, startJob, watchJobs } from "../session.js";
import { LinkDawarich, LinkedAccount } from "./link.js";

export function Settings() {
  const me = useStore((s) => s.me);
  const job = useStore((s) => s.job);
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
  const autoKeys = ["auto_enabled", "interval_min", "start_date", "auto_push"];
  const running = !!job;

  return html`<div class="doc">
    <h1>Settings</h1>
    <p class="lede">Signed in as <b>${me.name}</b>${me.username && me.username !== me.name ? html` <span class="faint">(${me.username})</span>` : ""}.</p>

    <section>
      <header><h2>Your Dawarich</h2><p>Where your trips come from, and where pushed trips go.</p></header>
      <div class="body">
        ${me.dawarich_linked ? html`<${LinkedAccount} settings=${s} />` : html`<${LinkDawarich} intro=${false} />`}
      </div>
    </section>

    <section>
      <header><h2>Automatic runs</h2><p>Off until you turn them on. Pushing is a separate switch.</p></header>
      <div class="body">
        <label class="check"><input type="checkbox" class="switch" checked=${f.auto_enabled} onChange=${upd("auto_enabled")} />Download and match new trips on a schedule</label>
        <div class="formgrid" style="max-width:420px">
          <label class="field"><span>Every</span><div class="row"><input type="number" min="15" max="10080" style="width:96px" value=${f.interval_min} onInput=${upd("interval_min")} /><span class="dim">minutes</span></div></label>
          <label class="field"><span>Ignore data before</span><input type="date" value=${f.start_date} onInput=${upd("start_date")} /></label>
        </div>
        <label class="check"><input type="checkbox" class="switch" checked=${f.auto_push} onChange=${upd("auto_push")} />Push finished trips to Dawarich too</label>
        <p class="help">${f.auto_push ? "Each finished trip's points in Dawarich are swapped for its matched route. Locked trips are skipped, and your original GPS stays here, so any trip can be put back."
          : "Matched trips wait here until you push them."}</p>
        ${s.last_error && html`<div class="note bad">The last run failed: ${s.last_error}</div>`}
        <div class="actions"><button class="go" disabled=${saving === "auto" || !dirty(autoKeys)} onClick=${() => save("auto", autoKeys)}>Save</button>
          <span class="help">Last run ${ago(s.last_run)}</span></div>
      </div>
    </section>

    <section>
      <header><h2>Matching</h2><p>Used for trips matched from now on.</p></header>
      <div class="body">
        <label class="field"><span>A trip ends after a stop or a gap of</span>
          <div class="row"><input type="number" min="5" max="1440" style="width:96px" value=${f.trip_gap_min} onInput=${upd("trip_gap_min")} /><span class="dim">minutes</span></div></label>
        <div class="field"><span>Where the GPS drops out</span>
          <div class="seg" role="radiogroup">
            <button class=${f.strength === "fill" ? "on" : ""} role="radio" aria-checked=${f.strength === "fill"} onClick=${() => setF({ ...f, strength: "fill" })}>Route across it</button>
            <button class=${f.strength === "strict" ? "on" : ""} role="radio" aria-checked=${f.strength === "strict"} onClick=${() => setF({ ...f, strength: "strict" })}>Keep the GPS</button>
          </div>
          <p class="help">${f.strength === "fill" ? "Missing stretches follow the road or path when the timing and the points around them agree."
            : "Gaps are never routed; the line runs straight between the points either side."} Any trip can choose differently in its Gaps tab.</p></div>
        <p class="help">Walks and rides only snap to a path when they clearly follow it; off-trail stretches keep your GPS. Bikes ignore one-way streets.</p>
        <div class="actions"><button class="go" disabled=${saving === "match" || !dirty(["trip_gap_min", "strength"])} onClick=${() => save("match", ["trip_gap_min", "strength"])}>Save</button>
          <button onClick=${() => startJob("rematch", { scope: "all" })}>Match every trip again</button></div>
      </div>
    </section>

    <section>
      <header><h2>Your original GPS</h2><p>Every point read from Dawarich is kept here unchanged. Nothing in mapmatch deletes it.</p></header>
      <div class="body">
        <div class="stat"><div><b class="num">${(arch.points || 0).toLocaleString()}</b><span>points kept</span></div>
          <div><b class="num">${arch.first ? new Date(arch.first * 1000).toLocaleDateString() : "–"}</b><span>oldest</span></div>
          <div><b class="num">${arch.last ? new Date(arch.last * 1000).toLocaleDateString() : "–"}</b><span>newest</span></div></div>
        <label class="field"><span>Import a CSV export</span><input type="file" accept=".csv,text/csv" ref=${fileRef} />
          <p class="help">Columns: timestamp, lat, lon, and optionally accuracy, velocity, altitude. Points already here are skipped.</p></label>
        <div class="actions"><button disabled=${saving === "csv"} onClick=${upload}>Import</button></div>
      </div>
    </section>

    <section class="danger-zone">
      <header><h2>Put Dawarich right</h2><p>Both rewrite points in your Dawarich account from the copy kept here.</p></header>
      <div class="body">
        <div class="stack">
          <b>Repair tracks</b>
          <p class="help">Rewrites every trip mapmatch has changed so Dawarich draws it cleanly: matched points on the same device as your GPS, the stops beside pushed trips tidied, and no straight lines left from old tracks. Pushed trips stay pushed.</p>
          <div class="actions"><${Confirm} disabled=${running || !me.dawarich_linked} armedLabel="Rewrite those trips in Dawarich?" onConfirm=${() => startJob("repair")}>Repair tracks<//></div>
        </div>
        <div class="stack" style="border-top:1px solid var(--line);padding-top:14px">
          <b>Revert everything</b>
          <p class="help">Puts the original GPS back in Dawarich for every trip you pushed. Matches stay here, so you can push again later.</p>
          <label class="field"><span>Type <b>REVERT</b> to enable</span><input value=${typed} onInput=${(e) => setTyped(e.target.value)} style="max-width:160px" /></label>
          <div class="actions"><${Confirm} cls="danger" disabled=${typed !== "REVERT" || running} armedLabel="This changes Dawarich. Sure?"
            onConfirm=${() => { setTyped(""); startJob("revert_all"); }}>Revert all pushed trips<//></div>
        </div>
      </div>
    </section>

    <section>
      <header><h2>This server</h2><p>The services mapmatch uses.</p></header>
      <div class="body">
        ${!sys ? html`<p class="help">Checking…</p>` : html`
          ${!sys.valhalla_url ? html`<div class="note warn">No map server is set up yet. ${me.is_admin ? html`Add one under <a href="#/admin">Admin</a>.` : "Ask whoever runs this server to add one."}</div>`
          : sys.valhalla?.error ? html`<div class="note bad">The map server (Valhalla) isn't answering: ${sys.valhalla.error}</div>`
            : html`<div class="note good">Map server up: Valhalla ${sys.valhalla?.version}${sys.valhalla?.tileset_last_modified ? `, map data from ${new Date(sys.valhalla.tileset_last_modified * 1000).toLocaleDateString()}` : ""}.</div>`}
          ${sys.regions?.length > 0 && html`<p class="help">Map data covers ${sys.regions.map((r) => r.name).join(", ")}.</p>`}
          ${sys.dry_run && html`<div class="note warn">Dry run is on (PUSH_DRY_RUN): pushes are planned and logged, but Dawarich is not changed.</div>`}
          <p class="help">Matcher version ${sys.engine}.${sys.outdated_trips ? ` ${plural(sys.outdated_trips, "trip")} were matched by an older version.` : ""}</p>
          ${sys.outdated_trips > 0 && html`<div class="actions"><button onClick=${() => startJob("rematch", { scope: "outdated" })}>Match them again</button></div>`}`}
      </div>
    </section>
  </div>`;
}
