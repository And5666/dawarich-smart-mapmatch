// App shell: connect screen, top bar, router, toasts.
import { html, render, useState, useEffect, api, store, useStore, setTheme, Icon, Menu, ago } from "./lib.js";
import { loadMe, watchJobs, startJob } from "./session.js";
import { Trips } from "./views/trips.js";
import { TripView } from "./views/trip.js";
import { Activity } from "./views/activity.js";
import { Settings } from "./views/settings.js";

function useRoute() {
  const parse = () => (location.hash.replace(/^#\/?/, "") || "").split("/");
  const [route, setRoute] = useState(parse);
  useEffect(() => { const f = () => setRoute(parse()); addEventListener("hashchange", f); return () => removeEventListener("hashchange", f); }, []);
  return route;
}

const Mark = () => html`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
  <path d="M3 18c3.5-.5 4.5-11 8.5-11S15 14 21 11" /><circle cx="3" cy="18" r="1.4" fill="currentColor" stroke="none" />
  <circle cx="21" cy="11" r="1.4" fill="currentColor" stroke="none" /></svg>`;

function Connect() {
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [server, setServer] = useState("");
  useEffect(() => { api("/public").then((x) => setServer(x.dawarich)).catch(() => {}); }, []);
  async function submit(e) {
    e.preventDefault();
    setBusy(true); setErr("");
    try { location.hash = "#/"; await api("/connect", { method: "POST", body: { api_key: key } }); await loadMe(); watchJobs(); }
    catch (x) { setErr(x.message); }
    finally { setBusy(false); }
  }
  return html`<div class="connect"><form onSubmit=${submit}>
    <div class="mark"><${Mark} /> mapmatch</div>
    <h1>Connect your Dawarich</h1>
    <p>mapmatch reads the GPS points in your Dawarich account, turns them into trips and snaps them to roads, paths and railways. Nothing in Dawarich changes until you push a trip.</p>
    <label class="field"><span>API key</span>
      <input value=${key} onInput=${(e) => setKey(e.target.value)} autocomplete="off" spellcheck="false" required autofocus
        placeholder="Paste your key" style="font-family: ui-monospace, SFMono-Regular, Menlo, monospace" /></label>
    ${err && html`<p class="err" role="alert">${err}</p>`}
    <div class="row"><button class="go" type="submit" disabled=${busy || !key.trim()}>${busy ? "Checking…" : "Connect"}</button>
      <span class="grow"></span></div>
    <p class="help">You'll find the key in Dawarich under your account settings. ${server ? html`This server talks to <b>${server.replace(/^https?:\/\//, "")}</b>.` : ""}</p>
  </form></div>`;
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
  const tab = (href, name, label) => html`<a href=${href} class=${page === name || (name === "" && page === "trip") ? "on" : ""}><span class="lbl">${label}</span></a>`;
  return html`<header class="bar">
    <a class="mark" href="#/"><${Mark} /><span class="name">mapmatch</span></a>
    <nav class="nav">${tab("#/", "", "Trips")}${tab("#/activity", "activity", "Activity")}${tab("#/settings", "settings", "Settings")}</nav>
    <span class="grow"></span>
    <${SyncStatus} />
    <${Menu} cls="bare" title="Account" label=${html`<span class="ell" style="max-width:180px">${me?.name}</span><${Icon} name="down" small />`}>
      <div class="label">Signed in with Dawarich</div>
      <button onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}><${Icon} name=${theme === "dark" ? "sun" : "moon"} small />${theme === "dark" ? "Light" : "Dark"} theme</button>
      <div class="sep"></div>
      <button onClick=${async () => { await api("/logout", { method: "POST" }).catch(() => {}); store.set({ me: null }); }}>Sign out</button>
    <//>
  </header>`;
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
  if (me === null) return html`<${Connect} /><${Toasts} />`;
  const [page, arg] = route;
  let view;
  if (page === "trip" && arg) view = html`<${TripView} id=${Number(arg)} key=${arg} />`;
  else if (page === "activity") view = html`<${Activity} />`;
  else if (page === "settings") view = html`<${Settings} />`;
  else view = html`<${Trips} />`;
  return html`<div class="shell"><${Bar} route=${route} /><main class="page">${view}</main></div><${Toasts} />`;
}

render(html`<${App} />`, document.getElementById("app"));
