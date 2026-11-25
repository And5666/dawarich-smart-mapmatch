// Admin: the people on this server, the one map server everyone matches
// against, and the regions its map data covers.
import { html, useState, useEffect, useRef, api, apiUrl, useStore, toast, Icon, Menu, Confirm, ago, full, plural } from "../lib.js";
import { regionMap, parseBounds, boundsText } from "../map.js";

const bytes = (n) => n == null ? "" : n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;
const initials = (u) => (u.display_name || u.username).split(/[\s@._-]+/).filter(Boolean).slice(0, 2).map((x) => x[0].toUpperCase()).join("");
const hue = (s) => [...s].reduce((a, c) => (a * 31 + c.charCodeAt(0)) % 360, 7);

// A password someone can read out or paste: four short words' worth of letters and digits.
function generatePassword() {
  const abc = "abcdefghjkmnpqrstuvwxyz23456789";
  const a = new Uint32Array(16);
  crypto.getRandomValues(a);
  const s = [...a].map((x) => abc[x % abc.length]).join("");
  return `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8, 12)}-${s.slice(12, 16)}`;
}

export function Admin() {
  const [s, setS] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api("/admin/site").then(setS).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  if (err) return html`<div class="doc"><h1>Admin</h1><div class="note bad">${err}</div></div>`;
  if (!s) return html`<div class="doc faint">Loading…</div>`;
  return html`<div class="doc">
    <h1>Admin</h1>
    <p class="lede">Settings for everyone on this server.</p>
    <${People} s=${s} reload=${load} />
    <${Valhalla} s=${s} reload=${load} />
    <${Regions} s=${s} reload=${load} />
    <${Railways} />
    <${MapData} />
  </div>`;
}

// ------------------------------------------------------------------ people
function PasswordField({ value, onInput, label = "Password" }) {
  const [shown, setShown] = useState(false);
  return html`<label class="field"><span>${label}</span>
    <div class="row">
      <input type=${shown ? "text" : "password"} value=${value} onInput=${(e) => onInput(e.target.value)} autocomplete="new-password" class=${shown ? "mono" : ""} />
      <button type="button" class="sm" onClick=${() => { onInput(generatePassword()); setShown(true); }}>Generate</button>
      <button type="button" class="sm bare" onClick=${() => setShown(!shown)}>${shown ? "Hide" : "Show"}</button>
    </div>
    <span class="help">At least 8 characters. Copy it now; it isn't shown again.</span></label>`;
}

function People({ s, reload }) {
  const me = useStore((x) => x.me);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ username: "", password: "", display_name: "", is_admin: false });
  const [resetFor, setResetFor] = useState(null);
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(fn, ok) {
    setBusy(true);
    try { await fn(); await reload(); if (ok) toast(ok); return true; }
    catch (x) { toast(x.message, "bad"); return false; }
    finally { setBusy(false); }
  }
  const patch = (u, body, ok) => run(() => api(`/admin/users/${u.id}`, { method: "PATCH", body }), ok);
  async function add(e) {
    e.preventDefault();
    if (await run(() => api("/admin/users", { method: "POST", body: form }), `Account made for ${form.username}`)) {
      setForm({ username: "", password: "", display_name: "", is_admin: false }); setAdding(false);
    }
  }
  const admins = s.users.filter((u) => u.is_admin && !u.disabled).length;

  return html`<section>
    <header><h2>People</h2><p>${plural(s.users.length, "account")} on this server. Each person connects their own Dawarich.</p></header>
    <div class="body">
      <label class="check"><input type="checkbox" class="switch" checked=${s.signup} disabled=${busy}
        onChange=${(e) => run(() => api("/admin/site", { method: "PUT", body: { signup: e.target.checked } }), e.target.checked ? "Anyone can sign up now" : "Sign-up is closed")} />
        Let people make their own accounts</label>
      <p class="help" style="margin-top:-8px">${s.signup ? "Anyone who can reach this page can sign up." : "Only the accounts you make here can sign in."}</p>

      <div class="people">${s.users.map((u) => {
        const self = me?.username === u.username;
        const lastAdmin = u.is_admin && admins <= 1;
        return html`<div key=${u.id} class=${`person ${u.disabled ? "off" : ""}`}>
          <span class="avatar" style=${`background:hsl(${hue(u.username)} 32% 42%)`}>${initials(u)}</span>
          <div class="who">
            <div class="row wrap" style="gap:6px"><b class="ell">${u.display_name || u.username}</b>
              ${self && html`<span class="chip">you</span>`}
              ${!!u.is_admin && html`<span class="chip ink">admin</span>`}
              ${!!u.disabled && html`<span class="chip bad">disabled</span>`}</div>
            <div class="meta num"><span>@${u.username}</span>
              <span>${u.dawarich_linked ? `Dawarich key ${u.api_key_hint}` : "no Dawarich yet"}</span>
              <span>${plural(u.trips, "trip")}</span>
              ${u.created_at && html`<span title=${full(u.created_at)}>joined ${ago(u.created_at)}</span>`}</div>
          </div>
          <${Menu} cls="icon sm" title="Manage" label=${html`<${Icon} name="more" small />`}>
            <button onClick=${() => { setResetFor(u.id); setPw(""); }}>Set a new password…</button>
            <button disabled=${self || (u.is_admin && lastAdmin)} onClick=${() => patch(u, { is_admin: !u.is_admin }, u.is_admin ? `${u.username} is a member now` : `${u.username} is an admin now`)}>
              ${u.is_admin ? "Make a member" : "Make an admin"}</button>
            <div class="sep"></div>
            <button disabled=${self || (!u.disabled && lastAdmin)} onClick=${() => patch(u, { disabled: !u.disabled }, u.disabled ? `${u.username} can sign in again` : `${u.username} can't sign in now`)}>
              ${u.disabled ? "Let them sign in again" : "Stop them signing in"}</button>
          <//>
          ${resetFor === u.id && html`<div class="edit">
            <${PasswordField} label=${`New password for ${u.username}`} value=${pw} onInput=${setPw} />
            <div class="actions">
              <button class="go sm" disabled=${busy || pw.length < 8} onClick=${async () => { if (await patch(u, { password: pw }, "Password changed")) setResetFor(null); }}>Save password</button>
              <button class="sm" onClick=${() => setResetFor(null)}>Cancel</button></div>
          </div>`}
        </div>`;
      })}</div>

      ${!adding ? html`<div class="actions"><button onClick=${() => setAdding(true)}>Add a person</button></div>`
        : html`<form class="inline-form" onSubmit=${add}>
          <b>New account</b>
          <div class="formgrid">
            <label class="field"><span>Username</span><input value=${form.username} onInput=${(e) => setForm({ ...form, username: e.target.value })} autocomplete="off" spellcheck="false" required autofocus /></label>
            <label class="field"><span>Name <span class="faint">(optional)</span></span><input value=${form.display_name} onInput=${(e) => setForm({ ...form, display_name: e.target.value })} /></label>
          </div>
          <${PasswordField} value=${form.password} onInput=${(v) => setForm({ ...form, password: v })} />
          <label class="check"><input type="checkbox" checked=${form.is_admin} onChange=${(e) => setForm({ ...form, is_admin: e.target.checked })} />Admin: can change everything on this page</label>
          <div class="actions">
            <button class="go" disabled=${busy || !form.username.trim() || form.password.length < 8}>Make account</button>
            <button type="button" onClick=${() => setAdding(false)}>Cancel</button>
            <span class="help">Give them the username and password; they connect their own Dawarich after signing in.</span></div>
        </form>`}
    </div>
  </section>`;
}

// ------------------------------------------------------------------ valhalla
function Valhalla({ s, reload }) {
  const [url, setUrl] = useState(s.valhalla_url || "");
  const [busy, setBusy] = useState("");
  const [hit, setHit] = useState(null);
  const v = s.valhalla || {};
  async function save() {
    setBusy("save");
    try { await api("/admin/site", { method: "PUT", body: { valhalla_url: url.trim() } }); await reload(); setHit(null); toast("Saved. Everyone matches against this server now."); }
    catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }
  async function test() {
    setBusy("test"); setHit(null);
    try { setHit(await api("/admin/valhalla/test", { method: "POST", body: { url: url.trim() } })); }
    catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }
  const date = (x) => new Date(x * 1000).toLocaleDateString();
  return html`<section>
    <header><h2>Map server</h2><p>The Valhalla server every account matches against.</p></header>
    <div class="body">
      <label class="field"><span>Valhalla address</span>
        <div class="row"><input value=${url} onInput=${(e) => setUrl(e.target.value)} placeholder="http://valhalla:8002" spellcheck="false" class="mono" />
          <button disabled=${busy === "test" || !url.trim()} onClick=${test}>${busy === "test" ? "Testing…" : "Test"}</button></div></label>
      ${hit && !hit.error && html`<div class="note good">Valhalla ${hit.version} answered${hit.tileset_last_modified ? `, map data from ${date(hit.tileset_last_modified)}` : ""}.</div>`}
      ${hit?.error && html`<div class="note bad">${hit.url || url} didn't answer: ${hit.error}</div>`}
      ${!hit && s.configured && (v.error
        ? html`<div class="note bad">${s.valhalla_url} isn't answering: ${v.error}</div>`
        : html`<div class="note good">In use: Valhalla ${v.version || ""}${v.tileset_last_modified ? `, map data from ${date(v.tileset_last_modified)}` : ""}.</div>`)}
      ${!s.configured && html`<div class="note warn">No map server yet. Nothing can be matched until one is saved here.</div>`}
      <div class="actions"><button class="go" disabled=${busy === "save" || url.trim() === (s.valhalla_url || "")} onClick=${save}>Save</button></div>
    </div>
  </section>`;
}

// ------------------------------------------------------------------ regions
// Common Geofabrik extracts, so a region can be added without looking anything up.
const PRESETS = [
  ["Great Britain", "europe/great-britain", "49.80,-8.70,60.90,1.80"],
  ["England", "europe/united-kingdom/england", "49.80,-6.50,55.90,1.80"],
  ["Wales", "europe/united-kingdom/wales", "51.30,-5.40,53.50,-2.60"],
  ["Scotland", "europe/united-kingdom/scotland", "54.60,-8.70,60.90,-0.70"],
  ["Ireland and Northern Ireland", "europe/ireland-and-northern-ireland", "51.20,-10.70,55.50,-5.30"],
  ["France", "europe/france", "41.30,-5.20,51.10,9.60"],
  ["Germany", "europe/germany", "47.20,5.80,55.10,15.10"],
  ["Netherlands", "europe/netherlands", "50.70,3.30,53.60,7.30"],
  ["Belgium", "europe/belgium", "49.40,2.50,51.60,6.50"],
  ["Spain", "europe/spain", "35.90,-9.40,43.80,4.40"],
  ["Italy", "europe/italy", "36.60,6.60,47.10,18.60"],
  ["Switzerland", "europe/switzerland", "45.80,5.90,47.90,10.60"],
].map(([name, path, bounds]) => ({ name, pbf_url: `https://download.geofabrik.de/${path}-latest.osm.pbf`, bounds }));

function MiniMap({ bounds }) {
  const el = useRef(null);
  const theme = useStore((x) => x.theme);
  useEffect(() => {
    const b = parseBounds(bounds);
    if (!b) return;
    const map = regionMap(el.current, theme, b);
    return () => map.remove();
  }, [bounds, theme]);
  return html`<div class="mini" ref=${el}>${!parseBounds(bounds) && html`<div class="empty small faint">No area set</div>`}</div>`;
}

function RegionEditor({ start, onSave, onCancel, busy }) {
  const [r, setR] = useState({ name: "", pbf_url: "", bounds: "", built_at: "", notes: "", ...start });
  const el = useRef(null);
  const mapRef = useRef(null);
  const theme = useStore((x) => x.theme);
  useEffect(() => {
    const map = regionMap(el.current, theme, parseBounds(r.bounds), { interactive: true });
    mapRef.current = map;
    return () => map.remove();
  }, []);
  useEffect(() => { mapRef.current?.loaded() && mapRef.current.showBounds(parseBounds(r.bounds), false); }, [r.bounds]);
  const upd = (k) => (e) => setR({ ...r, [k]: e.target.value });
  const fromView = () => {
    const m = mapRef.current;
    if (!m) return;
    const b = m.getBounds();
    setR({ ...r, bounds: boundsText({ s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() }) });
  };
  const preset = (name) => {
    const p = PRESETS.find((x) => x.name === name);
    if (!p) return;
    setR({ ...r, name: r.name || p.name, pbf_url: p.pbf_url, bounds: p.bounds });
    const b = parseBounds(p.bounds);
    if (b && mapRef.current) mapRef.current.showBounds(b, true);
  };
  const valid = !r.bounds.trim() || parseBounds(r.bounds);
  return html`<div class="region-edit">
    <div class="fields">
      ${!start.id && html`<label class="field"><span>Start from a common extract</span>
        <select onChange=${(e) => preset(e.target.value)} style="width:100%"><option value="">Choose…</option>
          ${PRESETS.map((p) => html`<option value=${p.name}>${p.name}</option>`)}</select></label>`}
      <label class="field"><span>Name</span><input value=${r.name} onInput=${upd("name")} placeholder="England" required /></label>
      <label class="field"><span>OSM extract it was built from</span><input value=${r.pbf_url} onInput=${upd("pbf_url")} class="mono" placeholder="https://download.geofabrik.de/…-latest.osm.pbf" /></label>
      <label class="field"><span>Area (south, west, north, east)</span><input value=${r.bounds} onInput=${upd("bounds")} class="mono" placeholder="Pan the map, then Use this view" />
        ${!valid && html`<span class="help" style="color:var(--bad)">Four numbers: south, west, north, east.</span>`}</label>
      <div class="formgrid">
        <label class="field"><span>Built on</span><input type="date" value=${r.built_at} onInput=${upd("built_at")} /></label>
      </div>
      <label class="field"><span>Note <span class="faint">(optional)</span></span><input value=${r.notes} onInput=${upd("notes")} placeholder="Rebuilt monthly" /></label>
      <div class="actions">
        <button class="go" disabled=${busy || !r.name.trim() || !valid} onClick=${() => onSave(r)}>${start.id ? "Save region" : "Add region"}</button>
        <button onClick=${onCancel}>Cancel</button></div>
    </div>
    <div class="stack">
      <div class="mapbox"><div class="map" ref=${el}></div></div>
      <div class="row"><button class="sm" onClick=${fromView}>Use this view</button><span class="help">Pan and zoom to the area the tiles cover.</span></div>
    </div>
  </div>`;
}

function Regions({ s, reload }) {
  const [edit, setEdit] = useState(null);   // a region, or {} for a new one
  const [busy, setBusy] = useState(false);
  async function save(r) {
    setBusy(true);
    try {
      const body = { name: r.name, pbf_url: r.pbf_url, bounds: r.bounds, built_at: r.built_at, notes: r.notes };
      if (r.id) await api(`/admin/regions/${r.id}`, { method: "PATCH", body });
      else await api("/admin/regions", { method: "POST", body });
      await reload(); setEdit(null); toast(r.id ? "Region saved" : "Region added");
    } catch (x) { toast(x.message, "bad"); }
    finally { setBusy(false); }
  }
  return html`<section>
    <header><h2>Regions</h2><p>Where the map server has data. Everyone sees this list, so they know where matching works.</p></header>
    <div class="body">
      ${!s.regions.length && !edit && html`<p class="help">No regions listed yet.</p>`}
      <div class="regions">${s.regions.map((reg) => edit?.id === reg.id
        ? html`<${RegionEditor} key=${reg.id} start=${reg} busy=${busy} onSave=${save} onCancel=${() => setEdit(null)} />`
        : html`<div class="region" key=${reg.id}>
          <${MiniMap} bounds=${reg.bounds} />
          <div class="info">
            <div class="row"><b class="grow ell">${reg.name}</b>
              <button class="sm" onClick=${() => setEdit({ ...reg })}>Edit</button>
              <${Confirm} cls="sm danger" armedLabel="Remove?" onConfirm=${async () => {
                try { await api(`/admin/regions/${reg.id}`, { method: "DELETE" }); await reload(); toast("Region removed"); }
                catch (x) { toast(x.message, "bad"); }
              }}>Remove<//></div>
            <div class="meta num">
              <span>${reg.built_at ? `built ${new Date(reg.built_at).toLocaleDateString()}` : "build date not set"}</span>
              ${reg.pbf_url && html`<a href=${reg.pbf_url} target="_blank" rel="noreferrer">extract</a>`}
            </div>
            ${reg.notes && html`<p class="small dim">${reg.notes}</p>`}
          </div>
        </div>`)}</div>
      ${edit && !edit.id && html`<${RegionEditor} start=${{}} busy=${busy} onSave=${save} onCancel=${() => setEdit(null)} />`}
      ${!edit && html`<div class="actions"><button onClick=${() => setEdit({})}>Add a region</button></div>`}
    </div>
  </section>`;
}

// ------------------------------------------------------------------ railways
function Railways() {
  const [d, setD] = useState(null);
  const load = () => api("/admin/railways").then(setD).catch((e) => toast(e.message, "bad"));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!d?.status?.running) return;
    const t = setTimeout(load, 2000);
    return () => clearTimeout(t);
  }, [d]);
  const build = (source) => api("/admin/railways/build", { method: "POST", body: { source } }).then(setD).catch((e) => toast(e.message, "bad"));
  const remove = (file) => api(`/admin/railways/${encodeURIComponent(file)}`, { method: "DELETE" }).then((x) => { setD(x); toast("Removed"); }).catch((e) => toast(e.message, "bad"));
  const st = d?.status || {};
  return html`<section>
    <header><h2>Railways</h2><p>What trains are matched on. Great Britain's are built in; anywhere else needs a railway network made from map data.</p></header>
    <div class="body">
      ${!d ? html`<p class="help">Checking…</p>` : html`
        <div class="scroll-x"><table class="t"><thead><tr><th>Network</th><th>Track</th><th>Stations</th><th>Built</th><th></th></tr></thead><tbody>
          <tr><td>Great Britain <span class="faint">(national timetable, built in)</span></td><td class="num dim">–</td><td class="num dim">–</td><td></td><td></td></tr>
          ${d.networks.map((n) => html`<tr key=${n.file}>
            <td>${n.name}</td><td class="num dim" style="white-space:nowrap">${n.km.toLocaleString()} km</td><td class="num dim">${n.stations.toLocaleString()}</td>
            <td class="num dim" style="white-space:nowrap">${ago(n.built)}</td>
            <td>${n.removable && html`<${Confirm} cls="sm bare" armedLabel="Remove it?" onConfirm=${() => remove(n.file)}>Remove<//>`}</td></tr>`)}
        </tbody></table></div>
        ${st.running && html`<div class="note"><div>Building from ${st.source}: ${st.message}…</div></div>`}
        ${!st.running && st.error && html`<div class="note bad"><div>Building from ${st.source} failed: ${st.error}</div></div>`}
        ${!st.running && st.finished && !st.error && html`<div class="note good"><div>Built from ${st.source}: ${st.message}. Match trips there again to use it.</div></div>`}
        ${!d.can_build ? html`<p class="help">This server can't read map extracts yet: it needs pyosmium (<code>pip install osmium</code>; the mapmatch Docker image has it).</p>`
          : !d.mounted ? html`<p class="help">Mount the Valhalla data directory (see Map files below) to build a network from the extracts in it, or run
            <code>python -m mapmatch.tools.railways your-region.osm.pbf</code> in this container.</p>`
          : !d.sources.length ? html`<p class="help">No <code>.osm.pbf</code> extracts in the map data directory.</p>`
          : html`<p class="help">Build a network from an OpenStreetMap extract in the map data directory. Big countries take a few minutes.</p>
            <div class="stack" style="gap:6px">${d.sources.map((x) => html`<div class="row" key=${x.name}>
              <span class="grow">${x.name} <span class="faint num">${bytes(x.size)}</span></span>
              <button class="sm" disabled=${st.running} onClick=${() => build(x.name)}>
                ${d.networks.some((n) => n.name === x.network) ? "Build again" : "Build railways"}</button></div>`)}</div>`}`}
    </div>
  </section>`;
}

// ------------------------------------------------------------------ map data
function MapData() {
  const [data, setData] = useState(null);
  useEffect(() => { api("/admin/valhalla/data").then(setData).catch(() => setData({ mounted: false, files: [] })); }, []);
  return html`<section>
    <header><h2>Map files</h2><p>The extracts and tiles behind the map server, if you mounted them.</p></header>
    <div class="body">
      ${!data ? html`<p class="help">Checking…</p>` : !data.mounted ? html`
        <p class="help">Mount the Valhalla data directory read-only and set <code>VALHALLA_DATA_DIR</code> to list and download its files here, for example
          <code>/var/lib/docker/volumes/valhalla/_data:/valhalla:ro</code> with <code>VALHALLA_DATA_DIR=/valhalla</code>.
          Building tiles happens on the Valhalla side, not in this container.</p>`
      : html`
        <p class="help">${data.root}: ${plural(data.files.length, "file")}.</p>
        <div class="scroll-x"><table class="t"><thead><tr><th>File</th><th>Size</th><th>Changed</th><th></th></tr></thead><tbody>
        ${data.files.map((f) => html`<tr key=${f.name}>
          <td>${f.dir ? "▸ " : ""}${f.name}</td>
          <td class="num dim">${f.dir ? "" : bytes(f.size)}</td>
          <td class="num dim">${ago(f.modified)}</td>
          <td>${!f.dir && html`<a href=${apiUrl("/admin/valhalla/data/" + encodeURIComponent(f.name))}>Download</a>`}</td>
        </tr>`)}</tbody></table></div>`}
    </div>
  </section>`;
}
