// Settings: your Dawarich and what is downloaded from it, the original GPS kept
// here, undoing changes, and the server mapmatch runs on.
import { html, useState, useEffect, useRef, api, useStore, store, toast, Confirm, plural, Choices } from "../lib.js";
import { loadMe, startJob, watchJobs } from "../session.js";
import { LinkDawarich, LinkedAccount } from "./link.js";

const DOWNLOAD_KEYS = ["start_date", "trip_gap_min"];
const UNDO = {
  dawarich: { label: "Put the original GPS back in Dawarich",
    help: "Every trip you pushed gets its recorded points back in Dawarich. The matches stay here, so you can push them again later.",
    button: "Revert pushed trips", armed: "This changes Dawarich. Sure?", job: ["revert_all", {}] },
  here: { label: "Clear the matches here",
    help: "Every trip here loses its match and your choices on it, and shows the GPS as recorded. Dawarich isn't changed.",
    button: "Clear every match", armed: "Clear every match?", job: ["reset", {}] },
  both: { label: "Both: start again from the recorded GPS",
    help: "Dawarich gets the original GPS back first, then every match here is cleared.",
    button: "Revert and clear everything", armed: "This changes Dawarich. Sure?", job: ["reset", { dawarich: true }] },
};

export function Settings() {
  const me = useStore((s) => s.me);
  const job = useStore((s) => s.job);
  const s = me?.settings || {};
  const [f, setF] = useState(null);
  const [saving, setSaving] = useState("");
  const [undo, setUndo] = useState("dawarich");
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
      if ("trip_gap_min" in body) body.trip_gap_min = Number(body.trip_gap_min);
      await api("/settings", { method: "PUT", body });
      await loadMe();
      setF({ ...f, ...Object.fromEntries(keys.map((k) => [k, store.get().me.settings[k]])) });
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
  const running = !!job;
  const u = UNDO[undo];
  const needsDawarich = undo !== "here";

  return html`<div class="doc">
    <h1>Settings</h1>
    <p class="lede">Signed in as <b>${me.name}</b>${me.username && me.username !== me.name ? html` <span class="faint">(${me.username})</span>` : ""}.
      What gets matched, and when, is on the <a href="#/match">Match</a> page.</p>

    <section>
      <header><h2>Your Dawarich</h2><p>Where your trips come from, and where pushed trips go.</p></header>
      <div class="body">
        ${me.dawarich_linked ? html`<${LinkedAccount} settings=${s} />` : html`<${LinkDawarich} intro=${false} />`}
        <div class="formgrid" style="max-width:520px">
          <label class="field"><span>Download points from</span><input type="date" value=${f.start_date} onInput=${upd("start_date")} />
            <p class="help">Empty: everything. Older points are left alone.</p></label>
          <label class="field"><span>A trip ends after a stop or gap of</span>
            <div class="row"><input type="number" min="5" max="1440" style="width:96px" value=${f.trip_gap_min} onInput=${upd("trip_gap_min")} /><span class="dim">minutes</span></div></label>
        </div>
        <div class="actions"><button class="go" disabled=${saving === "download" || !dirty(DOWNLOAD_KEYS)} onClick=${() => save("download", DOWNLOAD_KEYS)}>Save</button></div>
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
      <header><h2>Undo</h2><p>Go back to the GPS you recorded, here, in Dawarich, or both. Your original points are never lost, and each trip's
        earlier versions stay in its History. Locked trips are left alone.</p></header>
      <div class="body">
        <${Choices} name="undo" value=${undo} onChange=${(v) => { setUndo(v); setTyped(""); }}
          options=${Object.entries(UNDO).map(([k, x]) => [k, x.label, x.help])} />
        <div class="row wrap" style="gap:10px;align-items:flex-end">
          <label class="field"><span>Type <b>UNDO</b> to confirm</span><input value=${typed} onInput=${(e) => setTyped(e.target.value)} style="width:150px" /></label>
          <${Confirm} cls="danger" disabled=${typed !== "UNDO" || running || (needsDawarich && !me.dawarich_linked)} armedLabel=${u.armed}
            onConfirm=${() => { setTyped(""); startJob(...u.job); }}>${u.button}<//>
        </div>
        <details>
          <summary class="small dim">Troubleshooting: repair tracks in Dawarich</summary>
          <div class="stack" style="margin-top:10px">
            <p class="help">You shouldn't need this: every push keeps Dawarich's tracks clean. Use it if Dawarich still draws straight lines or spikes
              around trips, for example after pushes made with a version of mapmatch before 2.4. Pushed trips stay pushed.</p>
            <div class="actions"><${Confirm} disabled=${running || !me.dawarich_linked} armedLabel="Rewrite those trips in Dawarich?" onConfirm=${() => startJob("repair")}>Repair tracks<//></div>
          </div>
        </details>
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
          ${sys.dry_run && html`<div class="note warn">Dry run is on (PUSH_DRY_RUN): pushes are planned and logged, but Dawarich isn't changed.</div>`}
          <p class="help">mapmatch ${sys.engine}.${sys.outdated_trips ? html`${" "}${plural(sys.outdated_trips, "trip")} were matched by an older version: <a href="#/match">match them again</a>.` : ""}</p>`}
      </div>
    </section>
  </div>`;
}
