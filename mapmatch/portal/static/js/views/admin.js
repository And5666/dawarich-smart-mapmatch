// Admin: the one Valhalla for the whole instance, the region catalogue that goes
// with it, who may register, and the accounts themselves.
import { html, useState, useEffect, api, apiUrl, toast, Icon, Menu, Confirm, ago, full, plural } from "../lib.js";

const bytes = (n) => n == null ? "" : n > 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} kB`;

export function Admin() {
  const [s, setS] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api("/admin/site").then(setS).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);
  if (err) return html`<div class="doc"><h1>Admin</h1><div class="note bad">${err}</div></div>`;
  if (!s) return html`<div class="doc faint">Loading…</div>`;
  return html`<div class="doc">
    <h1>Admin</h1>
    <p class="lede">Settings that apply to everyone on this server.</p>
    <${SignUp} s=${s} reload=${load} />
    <${Valhalla} s=${s} reload=${load} />
    <${MapData} s=${s} />
    <${Regions} s=${s} reload=${load} />
    <${Users} s=${s} reload=${load} />
  </div>`;
}

function SignUp({ s, reload }) {
  const [busy, setBusy] = useState(false);
  async function toggle(on) {
    setBusy(true);
    try { await api("/admin/site", { method: "PUT", body: { signup: on } }); await reload(); toast(on ? "Sign-up is open" : "Sign-up is closed"); }
    catch (e) { toast(e.message, "bad"); }
    finally { setBusy(false); }
  }
  return html`<section>
    <header><h2>Accounts</h2><p>Who can make an account here.</p></header>
    <div class="body">
      <label class="check"><input type="checkbox" checked=${s.signup} disabled=${busy} onChange=${(e) => toggle(e.target.checked)} />
        Let people create their own accounts</label>
      <p class="help">${s.signup
        ? "Anyone who can reach this page can sign up, then connect their own Dawarich key."
        : "Only accounts you create below can sign in. People sign in with a username and password you give them, then paste their own Dawarich key."}</p>
    </div>
  </section>`;
}

function Valhalla({ s, reload }) {
  const [url, setUrl] = useState(s.valhalla_url || "");
  const [busy, setBusy] = useState("");
  const [hit, setHit] = useState(null);
  const v = s.valhalla || {};
  async function save() {
    setBusy("save");
    try {
      await api("/admin/site", { method: "PUT", body: { valhalla_url: url.trim() } });
      await reload(); toast("Saved. Every account matches against this server now.");
    } catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }
  async function test() {
    setBusy("test"); setHit(null);
    try { setHit(await api("/admin/valhalla/test", { method: "POST", body: { url: url.trim() } })); }
    catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }
  return html`<section>
    <header><h2>Map matching</h2><p>One Valhalla for everyone here.</p></header>
    <div class="body">
      <label class="field"><span>Valhalla address</span>
        <input value=${url} onInput=${(e) => setUrl(e.target.value)} placeholder="http://valhalla:8002" spellcheck="false"
          style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace" />
        <p class="help">Mapmatch asks this server to snap trips to roads, paths and railways. It needs tiles for the regions below; nothing is matched until it is set.</p></label>
      <div class="actions">
        <button class="go" disabled=${busy === "save" || url.trim() === (s.valhalla_url || "")} onClick=${save}>Save</button>
        <button disabled=${busy === "test" || !url.trim()} onClick=${test}>${busy === "test" ? "Testing…" : "Test"}</button>
      </div>
      ${hit && !hit.error && html`<div class="note good">Valhalla ${hit.version} answered${hit.tileset_last_modified ? `; map data from ${new Date(hit.tileset_last_modified * 1000).toLocaleDateString()}` : ""}.</div>`}
      ${hit?.error && html`<div class="note bad">${hit.url || url} did not answer: ${hit.error}</div>`}
      ${!hit && s.configured && (v.error
        ? html`<div class="note bad">${s.valhalla_url} is unreachable: ${v.error}</div>`
        : html`<div class="note good">Using <b>${s.valhalla_url}</b>${v.version ? `, Valhalla ${v.version}` : ""}${v.tileset_last_modified ? `, map data from ${new Date(v.tileset_last_modified * 1000).toLocaleDateString()}` : ""}.</div>`)}
      ${!s.configured && html`<div class="note warn">No map-matching server yet. Matching will fail until an address is saved here.</div>`}
    </div>
  </section>`;
}

function MapData({ s }) {
  const [data, setData] = useState(null);
  useEffect(() => { api("/admin/valhalla/data").then(setData).catch(() => setData({ mounted: false, files: [] })); }, []);
  return html`<section>
    <header><h2>Map data</h2><p>The files behind the Valhalla server.</p></header>
    <div class="body">
      ${!data ? html`<p class="help">Checking…</p>` : !data.mounted ? html`
        <div class="note warn">The Valhalla data directory is not mounted in this container, so its files cannot be listed or downloaded from here.</div>
        <p class="help">Mount it read-only at <code>VALHALLA_DATA_DIR</code> (for example
          <code>- /var/lib/docker/volumes/valhalla/_data:/valhalla:ro</code> with <code>VALHALLA_DATA_DIR=/valhalla</code>) to see the extracts and built tiles behind this server, and to download them.</p>
        <p class="help">Building or replacing regions needs the Valhalla toolchain on the machine that runs it, not in this container.</p>`
      : html`
        <p class="help">${data.root} — ${plural(data.files.length, "file")}.</p>
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

const blank = { name: "", pbf_url: "", bounds: "", built_at: "", notes: "" };

function Regions({ s, reload }) {
  const [form, setForm] = useState(blank);
  const [edit, setEdit] = useState(null);
  const [busy, setBusy] = useState(false);
  const upd = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  async function add(e) {
    e.preventDefault();
    setBusy(true);
    try { await api("/admin/regions", { method: "POST", body: form }); setForm(blank); await reload(); toast("Region added"); }
    catch (x) { toast(x.message, "bad"); }
    finally { setBusy(false); }
  }
  async function saveEdit() {
    try { await api(`/admin/regions/${edit.id}`, { method: "PATCH", body: edit }); setEdit(null); await reload(); toast("Region saved"); }
    catch (x) { toast(x.message, "bad"); }
  }
  return html`<section>
    <header><h2>Regions</h2><p>Which areas the map data covers.</p></header>
    <div class="body">
      <p class="help">Everyone here sees this list, so they know where matching works. A trip outside these areas matches only as far as the tiles go.</p>
      ${!s.regions.length ? html`<p class="help">No regions yet.</p>` : html`
        <div class="scroll-x"><table class="t"><thead><tr><th>Region</th><th>Covers</th><th>Built</th><th></th></tr></thead><tbody>
        ${s.regions.map((reg) => edit?.id === reg.id ? html`<tr key=${reg.id}>
          <td><input value=${edit.name} onInput=${(e) => setEdit({ ...edit, name: e.target.value })} style="width:100%" /></td>
          <td><input value=${edit.bounds} onInput=${(e) => setEdit({ ...edit, bounds: e.target.value })} placeholder="south,west,north,east" style="width:100%" /></td>
          <td><input type="date" value=${edit.built_at} onInput=${(e) => setEdit({ ...edit, built_at: e.target.value })} /></td>
          <td class="row" style="gap:6px"><button class="sm go" onClick=${saveEdit}>Save</button><button class="sm" onClick=${() => setEdit(null)}>Cancel</button></td>
        </tr>` : html`<tr key=${reg.id}>
          <td>${reg.name}${reg.pbf_url && html`<div class="faint small"><a href=${reg.pbf_url} target="_blank" rel="noreferrer">extract</a></div>`}</td>
          <td class="dim num">${reg.bounds || "–"}</td>
          <td class="num dim">${reg.built_at || "not built"}</td>
          <td class="row" style="gap:6px"><button class="sm" onClick=${() => setEdit({ ...reg })}>Edit</button>
            <${Confirm} cls="sm danger" armedLabel="Delete?" onConfirm=${async () => {
              try { await api(`/admin/regions/${reg.id}`, { method: "DELETE" }); await reload(); toast("Region removed"); }
              catch (x) { toast(x.message, "bad"); }
            }}>Delete<//></td>
        </tr>`)}</tbody></table></div>`}
      <form onSubmit=${add} class="region-add">
        <div class="row" style="flex-wrap:wrap;gap:10px">
          <label class="field"><span>Name</span><input value=${form.name} onInput=${upd("name")} placeholder="England and Wales" required /></label>
          <label class="field"><span>Covers</span><input value=${form.bounds} onInput=${upd("bounds")} placeholder="50.0,-5.8,55.8,1.8" /></label>
          <label class="field"><span>Built</span><input type="date" value=${form.built_at} onInput=${upd("built_at")} /></label>
        </div>
        <label class="field"><span>OSM extract</span><input value=${form.pbf_url} onInput=${upd("pbf_url")} placeholder="https://download.geofabrik.de/europe/great-britain-latest.osm.pbf" /></label>
        <label class="field"><span>Note</span><input value=${form.notes} onInput=${upd("notes")} placeholder="Optional" /></label>
        <div class="actions"><button disabled=${busy || !form.name.trim()}>Add region</button></div>
      </form>
    </div>
  </section>`;
}

const newUser = { username: "", password: "", display_name: "", is_admin: false };

function Users({ s, reload }) {
  const [form, setForm] = useState(newUser);
  const [busy, setBusy] = useState(false);
  const [reset, setReset] = useState(null);   // {id, password}
  const upd = (k) => (e) => setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  async function add(e) {
    e.preventDefault();
    setBusy(true);
    try { await api("/admin/users", { method: "POST", body: form }); setForm(newUser); await reload(); toast("Account created"); }
    catch (x) { toast(x.message, "bad"); }
    finally { setBusy(false); }
  }
  async function patch(u, body) {
    try { await api(`/admin/users/${u.id}`, { method: "PATCH", body }); await reload(); toast("Saved"); }
    catch (x) { toast(x.message, "bad"); }
  }
  async function saveReset() {
    try { await api(`/admin/users/${reset.id}`, { method: "PATCH", body: { password: reset.password } }); setReset(null); toast("Password set"); }
    catch (x) { toast(x.message, "bad"); }
  }
  return html`<section>
    <header><h2>People</h2><p>Accounts on this server.</p></header>
    <div class="body">
      <div class="scroll-x"><table class="t"><thead><tr><th>Account</th><th>Role</th><th>Dawarich</th><th>Trips</th><th>Since</th><th></th></tr></thead><tbody>
      ${s.users.map((u) => html`<tr key=${u.id} class=${u.disabled ? "off" : ""}>
        <td>${u.display_name || u.username}<div class="faint small">${u.username}</div></td>
        <td>${u.is_admin ? "Admin" : "Member"}${u.disabled ? html` <span class="flag bad">disabled</span>` : ""}</td>
        <td class="dim">${u.dawarich_linked ? `key ${u.api_key_hint}` : html`<span class="faint">not linked</span>`}</td>
        <td class="num dim">${u.trips.toLocaleString()}</td>
        <td class="num dim">${u.created_at ? full(u.created_at) : ""}</td>
        <td class="row" style="gap:6px">
          <${Menu} cls="sm" label=${html`…`}>
            <button onClick=${() => setReset({ id: u.id, password: "" })}>Set a password…</button>
            <button onClick=${() => patch(u, { is_admin: !u.is_admin })}>${u.is_admin ? "Make a member" : "Make an admin"}</button>
            <div class="sep"></div>
            <button onClick=${() => patch(u, { disabled: !u.disabled })}>${u.disabled ? "Enable" : "Disable"}</button>
          <//>
        </td>
      </tr>`)}</tbody></table></div>
      ${reset && html`<div class="row" style="gap:8px;flex-wrap:wrap">
        <label class="field"><span>New password (8+ characters)</span>
          <input type="password" value=${reset.password} autocomplete="new-password" onInput=${(e) => setReset({ ...reset, password: e.target.value })} /></label>
        <div class="actions" style="align-self:end"><button class="go" disabled=${reset.password.length < 8} onClick=${saveReset}>Set</button>
          <button onClick=${() => setReset(null)}>Cancel</button></div>
      </div>`}
      <form onSubmit=${add}>
        <p class="help" style="margin:0 0 8px">Make an account, then hand over the username and password. They paste their own Dawarich key after signing in.</p>
        <div class="row" style="flex-wrap:wrap;gap:10px">
          <label class="field"><span>Username</span><input value=${form.username} onInput=${upd("username")} autocomplete="off" required /></label>
          <label class="field"><span>Password</span><input type="password" value=${form.password} onInput=${upd("password")} autocomplete="new-password" required /></label>
          <label class="field"><span>Name</span><input value=${form.display_name} onInput=${upd("display_name")} placeholder="Optional" /></label>
        </div>
        <label class="check"><input type="checkbox" checked=${form.is_admin} onChange=${upd("is_admin")} />Admin</label>
        <div class="actions"><button disabled=${busy || !form.username.trim() || form.password.length < 8}>Create account</button></div>
      </form>
    </div>
  </section>`;
}
