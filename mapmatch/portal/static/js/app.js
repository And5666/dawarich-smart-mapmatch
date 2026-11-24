// App shell: login, top bar, router, job status, toasts.
import { html, render, useState, useEffect, api, store, useStore, setTheme, Icon, ago } from "./lib.js";
import { loadMe, watchJobs, startJob } from "./session.js";
import { Overview } from "./views/overview.js";
import { TripEditor } from "./views/trip.js";
import { Activity } from "./views/activity.js";
import { Settings } from "./views/settings.js";
import { Admin } from "./views/admin.js";

function useRoute() {
  const parse = () => (location.hash.replace(/^#\/?/, "") || "").split("/");
  const [route, setRoute] = useState(parse);
  useEffect(() => { const f = () => setRoute(parse()); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return route;
}

// ------------------------------------------------------------------ login
function Login() {
  const [mode, setMode] = useState("login");
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [signup, setSignup] = useState(false);
  useEffect(() => { api("/public").then((x) => setSignup(x.signup)).catch(() => {}); }, []);
  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try { await api("/" + mode, { method: "POST", body: { username: u, password: p } }); await loadMe(); watchJobs(); }
    catch (x) { setErr(x.message); }
    finally { setBusy(false); }
  }
  return html`<div class="login"><form class="card" onSubmit=${submit}>
    <header><div class="row"><span class="brand-mark"><${Icon} name="map" size="sm" /></span><h1>Mapmatch</h1></div>
      <p>Clean, map-matched routes for your Dawarich account.</p></header>
    <div class="body">
      <label class="field"><span>Username</span><input value=${u} onInput=${(e) => setU(e.target.value)} autocomplete="username" required /></label>
      <label class="field"><span>Password</span><input type="password" value=${p} onInput=${(e) => setP(e.target.value)}
        autocomplete=${mode === "login" ? "current-password" : "new-password"} required minlength=${mode === "signup" ? 8 : undefined} /></label>
      ${err && html`<p class="err" role="alert">${err}</p>`}
      <button class="primary" type="submit" disabled=${busy}>${mode === "login" ? "Sign in" : "Create account"}</button>
      ${signup && html`<p class="hint">${mode === "login" ? "No account yet? " : "Have an account? "}
        <a href="#" onClick=${(e) => { e.preventDefault(); setMode(mode === "login" ? "signup" : "login"); setErr(""); }}>
        ${mode === "login" ? "Create one" : "Sign in"}</a></p>`}
    </div></form></div>`;
}

// ------------------------------------------------------------------ shell
function Status() {
  const job = useStore((s) => s.job);
  const me = useStore((s) => s.me);
  const st = me?.settings || {};
  if (job) {
    return html`<div class="status" title=${job.message}><${Icon} name="sync" size="sm" cls="spin" />
      <span class="msg ellipsis">${job.message || job.kind}</span><span class="bar"><i style=${`width:${job.progress}%`}></i></span>
      <button class="sm ghost" onClick=${() => api(`/jobs/${job.id}/cancel`, { method: "POST" }).then(watchJobs)}>Cancel</button></div>`;
  }
  return html`<div class="status">
    ${st.last_error ? html`<span class="badge danger" title=${st.last_error}><${Icon} name="alert" size="sm" />Last sync failed</span>`
      : html`<span class="msg">${st.auto_enabled ? `Auto every ${st.interval_min} min · ` : ""}synced ${ago(st.last_run)}</span>`}
    <button class="sm" onClick=${() => startJob("sync")} disabled=${!st.api_key_set} title=${st.api_key_set ? "Fetch new points and match them" : "Add a Dawarich API key in Settings"}>
      <${Icon} name="sync" size="sm" />Sync now</button></div>`;
}

function TopBar({ route }) {
  const me = useStore((s) => s.me);
  const theme = useStore((s) => s.theme);
  const page = route[0] || "";
  const link = (href, name, icon, label) => html`<a href=${href} class=${page === name ? "on" : ""} title=${label}>
    <span class="row"><${Icon} name=${icon} size="sm" /><span class="lbl">${label}</span></span></a>`;
  return html`<header class="topbar">
    <a class="brand" href="#/"><span class="brand-mark"><${Icon} name="map" size="sm" /></span><span class="name">Mapmatch</span></a>
    <nav class="nav">
      ${link("#/", "", "map", "Trips")}
      ${link("#/activity", "activity", "activity", "Activity")}
      ${link("#/settings", "settings", "settings", "Settings")}
      ${me?.is_admin && link("#/admin", "admin", "shield", "Admin")}
    </nav>
    <span class="grow"></span>
    <${Status} />
    <button class="icon-btn ghost" title=${`Switch to ${theme === "dark" ? "light" : "dark"} theme`} onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}>
      <${Icon} name=${theme === "dark" ? "sun" : "moon"} /></button>
    <button class="icon-btn ghost" title=${`Sign out ${me?.username || ""}`} onClick=${async () => { await api("/logout", { method: "POST" }).catch(() => {}); store.set({ me: null }); }}>
      <${Icon} name="logout" /></button>
  </header>`;
}

function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return html`<div class="toasts" aria-live="polite">${toasts.map((t) => html`<div class=${`toast ${t.kind === "error" ? "error" : ""}`} key=${t.id}>
    <${Icon} name=${t.kind === "error" ? "alert" : "check"} size="sm" /><span>${t.message}</span></div>`)}</div>`;
}

function App() {
  const me = useStore((s) => s.me);
  const route = useRoute();
  useEffect(() => { loadMe().then(() => { if (store.get().me) watchJobs(); }); }, []);
  if (me === undefined) return html`<${Toasts} />`;
  if (me === null) return html`<${Login} /><${Toasts} />`;
  const [page, arg] = route;
  let view;
  if (page === "trip" && arg) view = html`<${TripEditor} id=${Number(arg)} key=${arg} />`;
  else if (page === "activity") view = html`<${Activity} />`;
  else if (page === "settings") view = html`<${Settings} />`;
  else if (page === "admin" && me.is_admin) view = html`<${Admin} />`;
  else view = html`<${Overview} />`;
  return html`<div class="shell"><${TopBar} route=${route} /><main class="page">${view}</main></div><${Toasts} />`;
}

render(html`<${App} />`, document.getElementById("app"));
