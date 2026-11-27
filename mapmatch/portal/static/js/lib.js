// Shared: rendering, API, store, formatting, modes, icons, small components.
export { html, render, useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect } from "../vendor/standalone.module.js";
import { html, useState, useEffect, useRef } from "../vendor/standalone.module.js";
import { mapStyles } from "./map.js";

// ------------------------------------------------------------------ API
const BASE = new URL(".", location.href).pathname;
export const apiUrl = (p) => BASE + "api" + p;

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") opts.headers["X-Portal"] = "1";
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let r;
  try { r = await fetch(apiUrl(path), opts); }
  catch (e) { throw new ApiError(0, "Can't reach the server."); }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/connect") store.set({ me: null });
  if (!r.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new ApiError(r.status, plain(msg || r.statusText || "Something went wrong"));
  }
  return data;
}

// Errors from the server side are written for a log. Say what happened and what it means.
export function plain(msg) {
  const m = String(msg || "");
  if (/Errno 111|Connection refused|Name or service not known|Temporary failure in name resolution|timed out|Errno 113/i.test(m)) {
    if (/valhalla/i.test(m)) return "The map server didn't answer, so nothing was matched. Try again in a minute.";
    return "Couldn't reach Dawarich, so nothing was changed. Try again in a minute.";
  }
  if (/^Valhalla:/i.test(m)) return "The map server couldn't match this trip. Nothing was changed.";
  if (/HTTP 401|HTTP 403|Unauthorized/i.test(m)) return "Dawarich refused the key. Check it in Settings. Nothing was changed.";
  return m;
}

// ------------------------------------------------------------------ store
function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get: () => state,
    set(patch) { state = { ...state, ...(typeof patch === "function" ? patch(state) : patch) }; subs.forEach((f) => f(state)); },
    subscribe(f) { subs.add(f); return () => subs.delete(f); },
  };
}
// What the maps show and how they look: kept in this browser.
const MAP_DEFAULTS = { style: "auto", width: "normal", routes: true, points: false };
function savedMapPrefs() { try { return { ...MAP_DEFAULTS, ...JSON.parse(localStorage.getItem("mm-map") || "{}") }; } catch (e) { return { ...MAP_DEFAULTS }; } }
export const LINE_WIDTHS = { thin: 0.65, normal: 1, bold: 1.5 };
export const store = createStore({ me: undefined, job: null, toasts: [], theme: currentTheme(), dataVersion: 0, mapPrefs: savedMapPrefs() });
export function setMapPrefs(patch) {
  const next = { ...store.get().mapPrefs, ...patch };
  try { localStorage.setItem("mm-map", JSON.stringify(next)); } catch (e) { /* private mode */ }
  store.set({ mapPrefs: next });
}
export function useStore(select) {
  const [v, setV] = useState(() => select(store.get()));
  useEffect(() => store.subscribe((s) => setV(select(s))), []);
  return v;
}
let toastId = 0;
export function toast(message, kind = "") {
  const id = ++toastId;
  if (kind === "bad") message = plain(message);
  store.set((s) => ({ toasts: [...s.toasts, { id, message, kind }] }));
  setTimeout(() => store.set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), kind === "bad" ? 6500 : 3200);
}
export const refreshData = () => store.set((s) => ({ dataVersion: s.dataVersion + 1 }));

// ------------------------------------------------------------------ theme
// Where the light/dark choice is kept. PORTAL_THEME_KEY lets the portal share it with a site it is part of.
const THEME_KEY = (window.MM_CONFIG && window.MM_CONFIG.themeKey) || "mm-theme";
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch (e) { /* private mode */ }
  store.set({ theme: t });
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (e) { /* ignore */ }
  if (!saved) store.set({ theme: currentTheme() });
});
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ------------------------------------------------------------------ modes
export const MODES = {
  walk: { label: "Walk", var: "--m-walk" },
  cycle: { label: "Cycle", var: "--m-cycle" },
  drive: { label: "Drive", var: "--m-drive" },
  train: { label: "Train", var: "--m-train" },
  stay: { label: "Stopped", var: "--m-stay" },
  raw: { label: "Keep GPS", var: "--m-raw" },
  none: { label: "Not matched", var: "--m-none" },
};
export const PICKABLE = ["walk", "cycle", "drive", "train", "stay", "raw"];
export const modeColor = (m) => cssVar((MODES[m] || MODES.none).var);
export const modeVar = (m) => `--c: var(${(MODES[m] || MODES.none).var})`;
export const colorExpr = () => ["match", ["get", "m"], ...Object.keys(MODES).flatMap((m) => [m, modeColor(m)]), modeColor("none")];

// ------------------------------------------------------------------ formatting
const dtf = (o) => new Intl.DateTimeFormat(undefined, o);
const fTime = dtf({ hour: "2-digit", minute: "2-digit" });
const fDay = dtf({ weekday: "short", day: "numeric", month: "short" });
const fDayYear = dtf({ weekday: "short", day: "numeric", month: "short", year: "numeric" });
const fFull = dtf({ dateStyle: "medium", timeStyle: "short" });
export const time = (ts) => fTime.format(ts * 1000);
export const day = (ts) => (new Date(ts * 1000).getFullYear() === new Date().getFullYear() ? fDay : fDayYear).format(ts * 1000);
export const full = (ts) => fFull.format(typeof ts === "number" ? ts * 1000 : new Date(ts));
export function km(v) {
  if (v == null) return "–";
  if (v < 1) return `${Math.round(v * 1000)} m`;
  return `${v < 10 ? v.toFixed(1) : Math.round(v).toLocaleString()} km`;
}
export function duration(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
}
export function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
export const pct = (v) => `${Math.round((v || 0) * 100)}%`;
export const dayKey = (ts) => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };
export const plural = (n, w, s = "s") => `${n.toLocaleString()} ${w}${n === 1 ? "" : s}`;

// ------------------------------------------------------------------ icons
const P = {
  drive: "M5 17h14M5 17v-4.5L7.2 7h9.6l2.2 5.5V17M7 17v2M17 17v2M5 12.5h14M8 14.8h.01M16 14.8h.01",
  cycle: "M5.5 17.5a3.2 3.2 0 1 0 0-.01M18.5 17.5a3.2 3.2 0 1 0 0-.01M5.5 17.5 9 10h6.5l3 7.5M9 10l3.5 7.5L15.5 10M8 7h3",
  walk: "M13.5 5a1.6 1.6 0 1 0 0-.01M10 21l2.2-6.2L14.5 17v4M8.5 12.5l2.5-4.5h2.8l2 3.8 2.2.8M11 8l-1.6 6",
  train: "M8 3h8a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3zM5 10h14M9 21l1.5-4M15 21l-1.5-4M9 13.5h.01M15 13.5h.01",
  stay: "M12 21s-6-5.4-6-10.2a6 6 0 0 1 12 0C18 15.6 12 21 12 21zM12 12.6a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6z",
  raw: "M5 9h.01M9 14h.01M12.5 8h.01M16 13h.01M19.5 9h.01",
  none: "M4 12h2M9 12h2M14 12h2M19 12h1",
  sync: "M20 11a8 8 0 0 0-14.3-4.3L4 9M4 4v5h5M4 13a8 8 0 0 0 14.3 4.3L20 15M20 20v-5h-5",
  back: "M15 18l-6-6 6-6",
  next: "M9 18l6-6-6-6",
  down: "M6 9l6 6 6-6",
  undo: "M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11",
  more: "M5 12h.01M12 12h.01M19 12h.01",
  check: "M5 12.5l4.2 4.2L19 7",
  x: "M6 6l12 12M18 6 6 18",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  move: "M12 3v18M3 12h18M12 3l-3 3M12 3l3 3M12 21l-3-3M12 21l3-3M3 12l3-3M3 12l3 3M21 12l-3-3M21 12l-3 3",
  mark: "M4 18c3.5-.5 4.5-10 8-10s3 6 8 4",
  route: "M6 19a2 2 0 1 0 0-.01M18 7a2 2 0 1 0 0-.01M6 17V9a3 3 0 0 1 3-3h1M18 9v6a3 3 0 0 1-3 3h-5",
  sliders: "M4 7h10M18 7h2M4 17h4M12 17h8M14 5v4M8 15v4",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
  gear: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 13.5l1.6 1.2-2 3.4-1.9-.7a7 7 0 0 1-2 1.2L14.8 21h-4l-.3-2.2a7 7 0 0 1-2-1.2l-1.9.7-2-3.4 1.6-1.2a7 7 0 0 1 0-3L4.6 9.3l2-3.4 1.9.7a7 7 0 0 1 2-1.2L10.8 3h4l.3 2.2a7 7 0 0 1 2 1.2l1.9-.7 2 3.4-1.6 1.2a7 7 0 0 1 0 3z",
  shield: "M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6l7-3z",
  push: "M12 16V4M7 9l5-5 5 5M5 20h14",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  filter: "M4 5h16l-6 7.5V19l-4 1.5v-8z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 8h.01",
  home: "M4 11l8-7 8 7M6 10v9h4v-5h4v5h4v-9",
  help: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.6 9.3a2.5 2.5 0 1 1 3.6 2.3c-.8.5-1.2 1-1.2 1.9M12 16.6h.01",
  lock: "M7 11V8a5 5 0 0 1 10 0v3M6 11h12v9H6z",
  flask: "M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4a2 2 0 0 0 1.8-3l-5-9V3M7.5 15h9",
  warn: "M12 4l9 15.5H3L12 4zM12 10v4.5M12 17h.01",
  layers: "M12 4l8 4.5-8 4.5-8-4.5L12 4zM4 12.5l8 4.5 8-4.5M4 16.5l8 4.5 8-4.5",
};
export function Icon({ name, small, cls = "" }) {
  return html`<svg class=${`i ${small ? "s" : ""} ${cls}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${P[name] || P.raw} /></svg>`;
}

// A thin bar of a trip's modes, each part sized by distance.
export function Ribbon({ modes }) {
  const total = (modes || []).reduce((a, m) => a + m.km, 0);
  if (!total) return html`<div class="ribbon none" title="Not matched yet"></div>`;
  return html`<div class="ribbon" title=${modes.map((m) => `${MODES[m.mode]?.label} ${km(m.km)}`).join(" · ")}>
    ${modes.map((m) => html`<i style=${`${modeVar(m.mode)};width:${(100 * m.km) / total}%`}></i>`)}</div>`;
}

// Click once to arm, again to confirm.
export function Confirm({ onConfirm, children, armedLabel = "Click again to confirm", cls = "", disabled }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (!armed) return; const t = setTimeout(() => setArmed(false), 4000); return () => clearTimeout(t); }, [armed]);
  return html`<button class=${`${cls} ${armed ? "armed" : ""}`} disabled=${disabled}
    onClick=${(e) => { e.stopPropagation(); if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}>${armed ? armedLabel : children}</button>`;
}

// A question in the middle of the page, for anything that changes data outside this app.
// Escape or a click outside cancels; focus starts on Cancel so Enter can't confirm by accident.
export function Dialog({ title, children, confirmLabel = "OK", cancelLabel = "Cancel", danger, onConfirm, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    addEventListener("keydown", esc, true);
    ref.current?.querySelector("button.cancel")?.focus();
    return () => removeEventListener("keydown", esc, true);
  }, []);
  return html`<div class="scrim" onPointerDown=${(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div class="dialog" role="dialog" aria-modal="true" aria-label=${title} ref=${ref}>
      <h2>${title}</h2>
      <div class="dialog-body">${children}</div>
      <div class="dialog-actions">
        <button class="cancel" onClick=${onClose}>${cancelLabel}</button>
        ${onConfirm && html`<button class=${danger ? "go danger-go" : "go"} onClick=${() => { onClose(); onConfirm(); }}>${confirmLabel}</button>`}
      </div>
    </div></div>`;
}

// The three things this app does, in the order they happen. Also shown on the Trips page until dismissed.
export function HowItWorks({ dry }) {
  return html`<ol class="how">
    <li><b>Get new points</b><span>Copies your recorded GPS from Dawarich. Your originals are kept here and never deleted.</span></li>
    <li><b>Match</b><span>Draws each trip along the roads, paths and railways you used. This happens only here: Dawarich doesn't change.</span></li>
    <li><b>Push</b><span>Replaces a trip's points in Dawarich with the corrected route. <b>Undo</b> puts your recorded points back, for one trip or all of them, at any time.${dry ? " In test mode a push changes nothing." : ""}</span></li>
  </ol>`;
}

// A button that opens a small menu; closes on outside click or Escape.
export function Menu({ label, children, cls = "", left = false, title }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    addEventListener("pointerdown", close);
    addEventListener("keydown", esc);
    return () => { removeEventListener("pointerdown", close); removeEventListener("keydown", esc); };
  }, [open]);
  return html`<div class="menu" ref=${ref}>
    <button class=${cls} title=${title} aria-haspopup="menu" aria-expanded=${open} onClick=${() => setOpen(!open)}>${label}</button>
    ${open && html`<div class=${`menu-pop ${left ? "left" : ""}`} role="menu" onClick=${() => setOpen(false)}>${children}</div>`}
  </div>`;
}

// The Map button on every map: what is drawn (the page passes its own switches as children),
// how thick the routes are, and which map is underneath. Stays open while you try things.
export function MapOptions({ children }) {
  const prefs = useStore((s) => s.mapPrefs);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    addEventListener("pointerdown", close);
    addEventListener("keydown", esc, true);
    return () => { removeEventListener("pointerdown", close); removeEventListener("keydown", esc, true); };
  }, [open]);
  return html`<div class="menu mapopt" ref=${ref}>
    <button class=${`sm ${open ? "on" : ""}`} aria-haspopup="dialog" aria-expanded=${open} onClick=${() => setOpen(!open)}
      title="What the map shows and how it looks"><${Icon} name="layers" small />Map</button>
    ${open && html`<div class="menu-pop left opts" role="dialog" aria-label="Map options">
      ${children && html`<div class="label">Show</div>${children}`}
      <div class="label">Route lines</div>
      <${Seg} value=${prefs.width} options=${[["thin", "Thin"], ["normal", "Normal"], ["bold", "Bold"]]} onChange=${(v) => setMapPrefs({ width: v })} />
      <div class="label">Map style</div>
      <${Seg} value=${mapStyles().some(([k]) => k === prefs.style) ? prefs.style : "auto"} options=${mapStyles()} onChange=${(v) => setMapPrefs({ style: v })} />
    </div>`}
  </div>`;
}
// One switch inside the Map popover.
export function MapSwitch({ on, onChange, disabled, hint, children }) {
  return html`<label class="optrow" style=${disabled ? "opacity:.5;pointer-events:none" : ""}>
    <input type="checkbox" class="switch" checked=${on} disabled=${disabled} onChange=${(e) => onChange(e.target.checked)} />
    <span>${children}${hint && html`<small>${hint}</small>`}</span></label>`;
}

export function navigate(hash) { if (location.hash !== hash) location.hash = hash; }

// A row of mutually exclusive buttons.
export function Seg({ value, options, onChange, disabled }) {
  return html`<div class="seg" role="radiogroup">${options.map(([k, label]) => html`<button role="radio" aria-checked=${value === k}
    class=${value === k ? "on" : ""} disabled=${disabled} onClick=${() => onChange(k)}>${label}</button>`)}</div>`;
}

// Larger radio choices, each with a line saying what it does.
export function Choices({ name, value, options, onChange }) {
  return html`<div class="choices" role="radiogroup">${options.map(([k, label, help]) => html`<label class=${`choice ${value === k ? "on" : ""}`}>
    <input type="radio" name=${name} checked=${value === k} onChange=${() => onChange(k)} />
    <span><b>${label}</b>${help && html`<span class="help">${help}</span>`}</span></label>`)}</div>`;
}
