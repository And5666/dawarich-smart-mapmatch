// Shared helpers: rendering, API, a tiny store, formatting, icons.
export { html, render, useState, useEffect, useRef, useMemo, useCallback, useLayoutEffect } from "../vendor/standalone.module.js";
import { html, useState, useEffect } from "../vendor/standalone.module.js";

// ------------------------------------------------------------------ API
const BASE = new URL(".", location.href).pathname; // the portal's base path, with trailing slash
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
  catch (e) { throw new ApiError(0, "Can't reach the portal server."); }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && path !== "/login") store.set({ me: null });
  if (!r.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
    throw new ApiError(r.status, msg || r.statusText || "Request failed");
  }
  return data;
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
export const store = createStore({ me: undefined, job: null, toasts: [], theme: currentTheme(), dataVersion: 0 });

export function useStore(select) {
  const [v, setV] = useState(() => select(store.get()));
  useEffect(() => store.subscribe((s) => setV(select(s))), []);
  return v;
}

let toastId = 0;
export function toast(message, kind = "info") {
  const id = ++toastId;
  store.set((s) => ({ toasts: [...s.toasts, { id, message, kind }] }));
  setTimeout(() => store.set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })), kind === "error" ? 6500 : 3500);
}
export const refreshData = () => store.set((s) => ({ dataVersion: s.dataVersion + 1 }));

// ------------------------------------------------------------------ theme
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  if (t) return t;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
export function setTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("mm-theme", t); } catch (e) { /* private mode */ }
  store.set({ theme: t });
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!localStorage.getItem("mm-theme")) store.set({ theme: currentTheme() });
});
export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ------------------------------------------------------------------ modes
export const MODES = {
  walk: { label: "Walk", var: "--m-walk" },
  cycle: { label: "Cycle", var: "--m-cycle" },
  drive: { label: "Drive", var: "--m-drive" },
  train: { label: "Train", var: "--m-train" },
  stay: { label: "Stay", var: "--m-stay" },
  raw: { label: "Keep GPS", var: "--m-raw" },
  none: { label: "Not matched", var: "--faint" },
};
export const EDITABLE_MODES = ["walk", "cycle", "drive", "train", "stay", "raw"];
export const modeColor = (m) => cssVar((MODES[m] || MODES.raw).var);
export const modeStyle = (m) => `--c: var(${(MODES[m] || MODES.raw).var})`;
export const dominantMode = (modes) => (modes || []).reduce((a, b) => (!a || b.km > a.km ? b : a), null)?.mode || "none";

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
  const h = Math.floor(m / 60);
  return `${h} h ${String(m % 60).padStart(2, "0")}`;
}
export function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}
export const pct = (v) => `${Math.round((v || 0) * 100)}%`;
export const dayKey = (ts) => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`; };

// ------------------------------------------------------------------ icons
const P = {
  drive: "M5 17h14M5 17v-4.5L7.2 7h9.6l2.2 5.5V17M7 17v2M17 17v2M5 12.5h14M8 14.8h.01M16 14.8h.01",
  cycle: "M5.5 17.5a3.2 3.2 0 1 0 0-.01M18.5 17.5a3.2 3.2 0 1 0 0-.01M5.5 17.5 9 10h6.5l3 7.5M9 10l3.5 7.5L15.5 10M8 7h3",
  walk: "M13.5 5a1.6 1.6 0 1 0 0-.01M10 21l2.2-6.2L14.5 17v4M8.5 12.5l2.5-4.5h2.8l2 3.8 2.2.8M11 8l-1.6 6",
  train: "M8 3h8a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3V6a3 3 0 0 1 3-3zM5 10h14M9 21l1.5-4M15 21l-1.5-4M9 13.5h.01M15 13.5h.01",
  stay: "M12 21s-6-5.4-6-10.2a6 6 0 0 1 12 0C18 15.6 12 21 12 21zM12 12.6a1.8 1.8 0 1 0 0-3.6 1.8 1.8 0 0 0 0 3.6z",
  raw: "M5 9h.01M9 14h.01M12.5 8h.01M16 13h.01M19.5 9h.01",
  sync: "M20 11a8 8 0 0 0-14.3-4.3L4 9M4 4v5h5M4 13a8 8 0 0 0 14.3 4.3L20 15M20 20v-5h-5",
  back: "M15 18l-6-6 6-6",
  next: "M9 18l6-6-6-6",
  undo: "M9 14L4 9l5-5M4 9h10.5a5.5 5.5 0 0 1 0 11H11",
  lock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3",
  unlock: "M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 6.8-1.2",
  upload: "M12 16V4M7 9l5-5 5 5M5 20h14",
  download: "M12 4v12M7 11l5 5 5-5M5 20h14",
  split: "M6 4v16M18 4v16M10 12h4M6 12h.01M18 12h.01",
  recalc: "M4 12a8 8 0 0 1 13.7-5.7L20 8.5M20 4v4.5h-4.5M20 12a8 8 0 0 1-13.7 5.7L4 15.5M4 20v-4.5h4.5",
  check: "M5 12.5l4.2 4.2L19 7",
  alert: "M12 9v4M12 17h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  x: "M6 6l12 12M18 6 6 18",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
  logout: "M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H4",
  push: "M12 20V9M7 14l5-5 5 5M5 4h14",
  map: "M9 4 3 6.5v13.5l6-2.5 6 2.5 6-2.5V4l-6 2.5zM9 4v13.5M15 6.5V20",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z",
  shield: "M12 21s7-3.5 7-9.5V5l-7-2.5L5 5v6.5C5 17.5 12 21 12 21z",
  layers: "M12 3 2 8.5l10 5.5 10-5.5zM2 15.5 12 21l10-5.5",
  fit: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
};
export function Icon({ name, size, cls = "" }) {
  return html`<svg class=${`icon ${size === "sm" ? "sm" : ""} ${cls}`} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d=${P[name] || P.raw} /></svg>`;
}
export function ModeChip({ mode, km: k }) {
  return html`<span class="chip" style=${modeStyle(mode)}><${Icon} name=${mode} size="sm" />${k != null ? km(k) : MODES[mode]?.label}</span>`;
}

// A button that asks once more before doing something destructive.
export function ConfirmButton({ onConfirm, children, confirmLabel = "Click again to confirm", cls = "", disabled }) {
  const [armed, setArmed] = useState(false);
  useEffect(() => { if (!armed) return; const t = setTimeout(() => setArmed(false), 4000); return () => clearTimeout(t); }, [armed]);
  return html`<button class=${`${cls} ${armed ? "confirming" : ""}`} disabled=${disabled}
    onClick=${() => { if (armed) { setArmed(false); onConfirm(); } else setArmed(true); }}>${armed ? confirmLabel : children}</button>`;
}

export const STATE_BADGE = {
  matched: ["ok", "Matched"], review: ["warn", "Needs review"], failed: ["danger", "Failed"],
  new: ["", "Not matched"], locked: ["", "Locked"],
};
export const PUSH_BADGE = { pushed: ["accent", "In Dawarich"], outdated: ["warn", "Edited since push"], original: ["", "Original in Dawarich"] };
export function StateBadge({ state }) { const [c, t] = STATE_BADGE[state] || ["", state]; return html`<span class=${`badge ${c}`}>${t}</span>`; }
export function PushBadge({ push }) { const [c, t] = PUSH_BADGE[push] || ["", push]; return html`<span class=${`badge ${c}`}>${t}</span>`; }

export function navigate(hash) { if (location.hash !== hash) location.hash = hash; }
