// App shell: sign-in, top bar, router, toasts.
import { html, render, useState, useEffect, api, store, useStore, setTheme, Icon, Menu, ago } from "./lib.js";
import { loadMe, watchJobs, startJob } from "./session.js";
import { Trips } from "./views/trips.js";
import { TripView } from "./views/trip.js";
import { Activity } from "./views/activity.js";
import { Match } from "./views/match.js";
import { Settings } from "./views/settings.js";
import { Admin } from "./views/admin.js";
import { LinkDawarich } from "./views/link.js";

function useRoute() {
  const parse = () => (location.hash.replace(/^#\/?/, "") || "").split("/");
  const [route, setRoute] = useState(parse);
  useEffect(() => { const f = () => setRoute(parse()); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return route;
}

const Mark = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
  <path d="M3 18c3.5-.5 4.5-11 8.5-11S15 14 21 11" /><circle cx="3" cy="18" r="1.4" fill="currentColor" stroke="none" />
  <circle cx="21" cy="11" r="1.4" fill="currentColor" stroke="none" /></svg>`;

function Gate() {
  const [info, setInfo] = useState(null);
  const [mode, setMode] = useState("login");
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  useEffect(() => { api("/public").then((x) => { setInfo(x); if (x.first_run) setMode("register"); }).catch(() => setInfo({})); }, []);
  const open = !!(info?.signup || info?.first_run);
  const register = mode === "register";
  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try {
      const body = register ? { username: u.trim(), password: p, display_name: name } : { username: u.trim(), password: p };
      await api(register ? "/register" : "/login", { method: "POST", body });
      location.hash = "#/";
      await loadMe();
      watchJobs();
    } catch (x) { setErr(x.message); }
    finally { setBusy(false); }
  }
  return html`<div class="connect"><div class="panel">
    <div class="mark"><${Mark} /> mapmatch</div>
    <div class="stack" style="gap:6px">
      <h1>${register ? (info?.first_run ? "Set up mapmatch" : "Make an account") : "Sign in"}</h1>
      <p>${info?.first_run ? "This is a fresh install. The account you make now runs it: you'll set the map server and can add other people."
        : "Your Dawarich trips, snapped to the roads, paths and railways you actually used."}</p>
    </div>
    <form onSubmit=${submit}>
      <label class="field"><span>Username</span>
        <input value=${u} onInput=${(e) => setU(e.target.value)} autocomplete="username" spellcheck="false" required autofocus /></label>
      <label class="field"><span>Password</span>
        <input type="password" value=${p} onInput=${(e) => setP(e.target.value)} autocomplete=${register ? "new-password" : "current-password"} required /></label>
      ${register && html`<label class="field"><span>Name <span class="faint">(optional)</span></span>
        <input value=${name} onInput=${(e) => setName(e.target.value)} autocomplete="nickname" /></label>`}
      ${err && html`<p class="err" role="alert">${err}</p>`}
      <button class="go" type="submit" disabled=${busy || !u.trim() || !p}>${busy ? "Checking…" : register ? "Create account" : "Sign in"}</button>
    </form>
    ${open && !info?.first_run && html`<p class="help">${register ? "Already have an account? " : "New here? "}<button class="link" type="button"
      onClick=${() => { setMode(register ? "login" : "register"); setErr(""); }}>${register ? "Sign in" : "Create an account"}</button></p>`}
    ${!open && !info?.first_run && html`<p class="help">Accounts are made by whoever runs this server. Ask them for a username and password.</p>`}
  </div></div>`;
}

function ConnectDawarich() {
  const me = useStore((s) => s.me);
  return html`<div class="connect"><div class="panel">
    <div class="mark"><${Mark} /> mapmatch</div>
    <div class="stack" style="gap:6px">
      <h1>Connect your Dawarich</h1>
      <p>Paste the API key from your Dawarich account page. mapmatch then downloads your GPS points; nothing in Dawarich changes until you push a trip.</p>
    </div>
    <${LinkDawarich} intro=${false} onDone=${() => { location.hash = "#/"; }} />
    <div class="row" style="gap:10px;flex-wrap:wrap">
      ${me?.is_admin && html`<button class="sm" onClick=${() => { location.hash = "#/admin"; }}>Admin settings</button>`}
      <span class="grow"></span>
      <button class="sm bare" onClick=${async () => { await api("/logout", { method: "POST" }).catch(() => {}); store.set({ me: null }); }}>Sign out</button>
    </div>
  </div></div>`;
}

function SyncStatus() {
  const job = useStore((s) => s.job);
  const me = useStore((s) => s.me);
  const st = me?.settings || {};
  if (job) {
    return html`<div class="sync" title=${job.message}><${Icon} name="sync" small cls="spin" />
      <span class="msg ell">${job.message || "Working"}</span><span class="meter"><i style=${`width:${job.progress}%`}></i></span>
      <button class="sm bare" onClick=${() => api(`/jobs/${job.id}/cancel`, { method: "POST" }).then(watchJobs)}>Stop</button></div>`;
  }
  return html`<div class="sync">
    ${st.last_error ? html`<span class="msg" style="color:var(--bad)" title=${st.last_error}>Last sync failed</span>`
      : html`<span class="msg">Synced ${ago(st.last_run)}</span>`}
    <button class="sm" onClick=${() => startJob("sync")} title="Download new GPS points from Dawarich. Nothing is matched yet.">
      <${Icon} name="sync" small />Sync</button></div>`;
}

function Bar({ route }) {
  const me = useStore((s) => s.me);
  const theme = useStore((s) => s.theme);
  const page = route[0] || "";
  const tab = (href, name, label, icon) => html`<a href=${href} class=${page === name || (name === "" && page === "trip") ? "on" : ""}
    aria-current=${page === name || (name === "" && page === "trip") ? "page" : undefined}><${Icon} name=${icon} small /><span class="lbl">${label}</span></a>`;
  return html`<header class="bar">
    <a class="mark" href="#/"><${Mark} /><span class="name">mapmatch</span></a>
    <nav class="nav">${tab("#/", "", "Trips", "route")}${tab("#/match", "match", "Match", "sliders")}${tab("#/activity", "activity", "Activity", "clock")}${tab("#/settings", "settings", "Settings", "gear")}${me?.is_admin ? tab("#/admin", "admin", "Admin", "shield") : ""}</nav>
    <span class="grow"></span>
    <${SyncStatus} />
    <${Menu} cls="bare" title="Account" label=${html`<span class="ell" style="max-width:180px">${me?.name}</span><${Icon} name="down" small />`}>
      <div class="label">${me?.is_admin ? "Admin" : "Member"}${me?.username ? html` · @${me.username}` : ""}</div>
      <button onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}><${Icon} name=${theme === "dark" ? "sun" : "moon"} small />${theme === "dark" ? "Light" : "Dark"} theme</button>
      <div class="sep"></div>
      <button onClick=${async () => { await api("/logout", { method: "POST" }).catch(() => {}); store.set({ me: null }); }}>Sign out</button>
    <//>
  </header>`;
}

function Banner() {
  const me = useStore((s) => s.me);
  const job = useStore((s) => s.job);
  if (!me?.dawarich_needs_repair || job?.kind === "repair") return null;
  return html`<div class="banner bad" role="alert">
    <span class="grow">A change to Dawarich stopped halfway, so some of your points are missing there. Every one of them is safe here.</span>
    <button class="sm go" onClick=${() => startJob("repair")}>Put them back</button></div>`;
}

function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return html`<div class="toasts" aria-live="polite">${toasts.map((t) => html`<div class=${`toast ${t.kind}`} key=${t.id}>${t.message}</div>`)}</div>`;
}

function App() {
  const me = useStore((s) => s.me);
  const route = useRoute();
  useEffect(() => { loadMe().then(() => { if (store.get().me) watchJobs(); }); }, []);
  if (me === undefined) return html`<${Toasts} />`;
  if (me === null) return html`<${Gate} /><${Toasts} />`;
  const [page, arg] = route;
  // A signed-in account with no Dawarich key can still reach Settings and Admin,
  // so an admin can set up the server before anyone has linked a key.
  const allowed = page === "settings" || page === "admin";
  if (!me.dawarich_linked && !allowed) return html`<${ConnectDawarich} /><${Toasts} />`;
  if (page === "admin" && !me.is_admin) return html`<div class="shell"><${Bar} route=${route} /><main class="page">
    <div class="doc"><h1>Admin</h1><p class="lede">Only an admin can see this page.</p></div></main></div><${Toasts} />`;
  let view;
  if (page === "trip" && arg) view = html`<${TripView} id=${Number(arg)} key=${arg} />`;
  else if (page === "activity") view = html`<${Activity} />`;
  else if (page === "match") view = html`<${Match} />`;
  else if (page === "settings") view = html`<${Settings} />`;
  else if (page === "admin") view = html`<${Admin} />`;
  else view = html`<${Trips} />`;
  return html`<div class="shell"><div><${Bar} route=${route} /><${Banner} /></div><main class="page">${view}</main></div><${Toasts} />`;
}

render(html`<${App} />`, document.getElementById("app"));
