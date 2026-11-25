// One trip: the map, a timeline to pick any stretch and set its mode, and tabs
// for the route, its gaps, the GPS points and every earlier version.
import {
  html, useState, useEffect, useRef, useMemo, api, apiUrl, useStore, toast, refreshData, Icon, Menu, Confirm,
  MODES, PICKABLE, modeVar, cssVar, colorExpr, km, duration, time, day, full, ago, pct, plural, navigate,
} from "../lib.js";
import { createMap, setMapTheme, setOverlay, setData, fc, fitCoords } from "../map.js";

const R = 6371008.8, RAD = Math.PI / 180;
const dist = (a, b) => {
  const x = Math.sin(((b.lat - a.lat) * RAD) / 2) ** 2 + Math.cos(a.lat * RAD) * Math.cos(b.lat * RAD) * Math.sin(((b.lon - a.lon) * RAD) / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
};
const line = (coords, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: coords.map((c) => [c[1], c[0]]) } });
const point = (lat, lon, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: [lon, lat] } });

function speeds(raw) {
  const v = raw.map((p, i) => {
    if (p.velocity > 0) return p.velocity * 3.6;
    const a = raw[Math.max(0, i - 1)], b = raw[Math.min(raw.length - 1, i + 1)];
    const dt = b.timestamp - a.timestamp;
    return dt > 0 ? (dist(a, b) / dt) * 3.6 : 0;
  });
  return v.map((_, i) => { const w = v.slice(Math.max(0, i - 1), i + 2).sort((x, y) => x - y); return w[Math.floor(w.length / 2)]; });
}

function positionAt(segments, ts) {
  let prev = null;
  for (const s of segments) for (const p of s.pieces) for (const c of p.coords) {
    if (c[2] >= ts) {
      if (!prev || c[2] === prev[2]) return [c[0], c[1]];
      const f = (ts - prev[2]) / (c[2] - prev[2]);
      return [prev[0] + (c[0] - prev[0]) * f, prev[1] + (c[1] - prev[1]) * f];
    }
    prev = c;
  }
  return prev ? [prev[0], prev[1]] : null;
}

function between(segments, t0, t1) {
  const out = [];
  for (const s of segments) {
    if (s.mode === "stay") continue;
    for (const p of s.pieces) {
      const cs = p.coords.filter((c) => c[2] >= t0 && c[2] <= t1);
      if (cs.length > 1) out.push(cs);
    }
  }
  return out;
}

// Every hole the filler bridged (or kept), one entry per stretch of time.
function listGaps(segs) {
  const out = [];
  const gate = (why) => (why || []).filter((w) => w === "too_fast" || w === "too_long");
  for (const s of segs) for (const p of s.pieces || []) {
    if (!p.gap) continue;
    const [g0, g1] = p.gap;
    let g = out.find((x) => x.t0 <= g1 && g0 <= x.t1);
    if (!g) { g = { t0: g0, t1: g1, km: 0, routed: false, overrode: false, kept: false, why: [], mode: s.mode }; out.push(g); }
    g.km += p.km || 0;
    if (p.kind === "filled") { g.routed = true; g.overrode = g.overrode || !!p.overrode_gate; }
    if (p.kept) g.kept = true;
    g.why = [...new Set([...g.why, ...gate(p.why)])];
  }
  return out.sort((a, b) => a.t0 - b.t0);
}

// Click or drag on the timeline / speed chart to choose a stretch of time.
function useScrub(t0, t1, onPick, onHover) {
  const drag = useRef(null);
  const tsAt = (el, x) => { const r = el.getBoundingClientRect(); return t0 + Math.max(0, Math.min(1, (x - r.left) / r.width)) * (t1 - t0); };
  return {
    onPointerDown: (e) => { e.currentTarget.setPointerCapture(e.pointerId); drag.current = { x: e.clientX, a: tsAt(e.currentTarget, e.clientX) }; },
    onPointerMove: (e) => {
      const ts = tsAt(e.currentTarget, e.clientX);
      onHover(ts);
      if (drag.current && Math.abs(e.clientX - drag.current.x) > 4) onPick({ a: drag.current.a, b: ts, live: true });
    },
    onPointerUp: (e) => {
      if (!drag.current) return;
      const ts = tsAt(e.currentTarget, e.clientX);
      const moved = Math.abs(e.clientX - drag.current.x) > 4;
      onPick(moved ? { a: drag.current.a, b: ts } : { click: ts });
      drag.current = null;
    },
    onPointerLeave: () => { if (!drag.current) onHover(null); },
  };
}

function Chooser({ range, segments, onChoose, onCancel, left }) {
  const t0 = Math.round(Math.min(range.a, range.b)), t1 = Math.round(Math.max(range.a, range.b));
  const len = between(segments, t0, t1).reduce((s, cs) => s + cs.slice(1).reduce((a, c, i) => a + dist({ lat: cs[i][0], lon: cs[i][1] }, { lat: c[0], lon: c[1] }), 0), 0);
  return html`<div class="chooser" style=${`left:${Math.max(170, Math.min(left, innerWidth - 190))}px`} role="dialog" aria-label="Set mode">
    <div class="row small"><b class="num">${time(t0)}–${time(t1)}</b><span class="faint">${duration(t1 - t0)}${len ? ` · ${km(len / 1000)}` : ""}</span>
      <span class="grow"></span><button class="icon sm bare" onClick=${onCancel} title="Cancel (Esc)"><${Icon} name="x" small /></button></div>
    <div class="modes">${PICKABLE.map((m) => html`<button style=${modeVar(m)} onClick=${() => onChoose(t0, t1, m)}>
      <${Icon} name=${m} />${MODES[m].label}</button>`)}</div>
  </div>`;
}

function Toggle({ on, onChange, disabled, children }) {
  return html`<label class=${`toggle ${on ? "on" : ""}`} style=${disabled ? "opacity:.45;pointer-events:none" : ""}>
    <input type="checkbox" checked=${on} onChange=${(e) => onChange(e.target.checked)} />${children}</label>`;
}

export function TripView({ id }) {
  const theme = useStore((s) => s.theme);
  const me = useStore((s) => s.me);
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [sel, setSel] = useState(null);          // {a, b} time range, or null
  const [cursor, setCursor] = useState(null);
  const [layers, setLayers] = useState({ matched: true, track: false, points: true, ignored: false });
  const [editing, setEditing] = useState(false);
  const [pending, setPending] = useState({});     // point id -> [lat, lon] | "x" | null (undo a correction)
  const [tab, setTab] = useState("route");
  const [preview, setPreview] = useState(null);   // an earlier version being looked at
  const [focusGap, setFocusGap] = useState(null);
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const stripRef = useRef(null);
  const editRef = useRef({ editing: false, raw: [] });
  const gapRef = useRef(null);

  const load = () => api(`/trips/${id}`).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id]);

  async function act(label, path, body, method = "POST") {
    setBusy(label);
    try {
      const x = await api(`/trips/${id}${path}`, { method, body });
      setD(x); setSel(null); setPreview(null); refreshData();
      const r = x.result;
      if (r) {
        if (r.dry_run) toast("Dry run: Dawarich was not changed");
        else if (path === "/push") toast(`In Dawarich now: ${plural(r.matched_points, "matched point")}`);
        else toast(`Original GPS is back in Dawarich (${plural(r.original_points, "point")})`);
      }
      return x;
    } catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }

  const t = d?.trip;
  const segs = d?.segments || [];
  const shown = preview ? preview.segments : segs;
  const hasRecorded = segs.some((s) => s.profile === "recorded");
  const raw = d?.raw || [];
  const T0 = d ? Math.min(t.start, ...segs.map((s) => s.t0)) : 0;
  const T1 = d ? Math.max(t.end, ...segs.map((s) => s.t1)) : 1;
  const span = Math.max(1, T1 - T0);
  const edits = useMemo(() => {
    const e = {};
    for (const p of raw) if (p.edit != null) e[p.id] = p.edit;
    for (const [k, v] of Object.entries(pending)) { if (v == null) delete e[k]; else e[k] = v; }
    return e;
  }, [raw, pending]);
  const gaps = useMemo(() => listGaps(shown), [shown]);
  const pendingCount = Object.keys(pending).length;
  editRef.current = { editing, raw, edits };

  // ---------------------------------------------------------------- map
  useEffect(() => {
    if (!d || mapRef.current) return;
    const map = createMap(mapEl.current, theme);
    mapRef.current = map;
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    for (const lid of ["m-solid", "m-dash", "m-dot", "m-kept"]) {
      map.on("click", lid, (e) => {
        if (editRef.current.editing) return;
        const s = e.features[0].properties;
        if (s.gap) { gapRef.current?.(s.gap); return; }
        setSel({ a: s.t0, b: s.t1, seg: s.sid });
      });
      map.on("mouseenter", lid, () => { if (!editRef.current.editing) map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", lid, () => { map.getCanvas().style.cursor = ""; });
    }
    map.on("mousemove", "pts", (e) => {
      const p = e.features[0].properties;
      if (editRef.current.editing) { map.getCanvas().style.cursor = "grab"; return; }
      pop.setLngLat(e.lngLat).setHTML(`<b>${time(p.ts)}</b> · ±${Math.round(p.acc || 0)} m${p.v > 0 ? ` · ${Math.round(p.v * 3.6)} km/h` : ""}${p.moved ? " · moved by you" : ""}${p.out ? " · left out" : ""}`).addTo(map);
      setCursor(p.ts);
    });
    map.on("mouseleave", "pts", () => { pop.remove(); setCursor(null); map.getCanvas().style.cursor = ""; });
    // Edit mode: drag a point to move it; click it to leave it out (again to bring it back).
    let drag = null;
    const move = (e) => {
      if (!drag) return;
      if (!drag.moved && Math.hypot(e.point.x - drag.x, e.point.y - drag.y) < 3) return;
      drag.moved = true;
      setPending((p) => ({ ...p, [drag.id]: [e.lngLat.lat, e.lngLat.lng] }));
    };
    const end = () => {
      map.off("mousemove", move); map.off("touchmove", move);
      if (drag && !drag.moved) {
        const pid = drag.id;
        const cur = editRef.current.edits[pid];
        setPending((p) => ({ ...p, [pid]: cur === "x" ? null : "x" }));
      }
      drag = null;
      map.dragPan.enable();
      map.getCanvas().style.cursor = "";
    };
    const start = (e) => {
      if (!editRef.current.editing || !e.features?.length) return;
      e.preventDefault();
      map.dragPan.disable();
      drag = { id: e.features[0].properties.id, moved: false, x: e.point.x, y: e.point.y };
      map.getCanvas().style.cursor = "grabbing";
      map.on("mousemove", move); map.on("touchmove", move);
      map.once("mouseup", end); map.once("touchend", end);
    };
    map.on("mousedown", "pts", start);
    map.on("touchstart", "pts", (e) => { if (e.originalEvent.touches.length === 1) start(e); });
    map.once("load", () => fitCoords(map, d.raw.map((p) => [p.lat, p.lon]), 60, false));
    return () => { map.remove(); mapRef.current = null; };
  }, [d && d.trip.id]);
  useEffect(() => setMapTheme(mapRef.current, theme), [theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    const vis = (on) => ({ visibility: on ? "visible" : "none" });
    const showTrack = layers.track || !shown.length;
    // the original track, exactly as recorded
    setOverlay(map, "track", { data: fc(raw.length > 1 ? [line(raw.map((p) => [p.lat, p.lon]), {})] : []), layers: () => [
      { id: "track-case", type: "line", layout: { ...vis(showTrack), "line-join": "round", "line-cap": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 5.5, "line-opacity": 0.9 } },
      { id: "track", type: "line", layout: { ...vis(showTrack), "line-join": "round", "line-cap": "round" },
        paint: { "line-color": cssVar("--m-none"), "line-width": 2.6, "line-dasharray": [2.2, 1.4] } },
    ] });
    const parts = [];
    for (const s of shown) {
      if (s.mode === "stay") continue;
      for (const p of s.pieces) if (p.coords.length > 1) parts.push(line(p.coords,
        { sid: s.id, m: s.mode, k: p.kind, kept: p.kept === true, t0: s.t0, t1: s.t1, gap: p.gap ? p.gap[0] : null }));
    }
    const kinds = (lid, ks, dash, w = 4) => ({ id: lid, type: "line", filter: ["all", ["in", ["get", "k"], ["literal", ks]], ["!=", ["get", "kept"], true]],
      layout: { ...vis(layers.matched), "line-cap": dash ? "butt" : "round", "line-join": "round" },
      paint: { "line-color": colorExpr(), "line-width": w, ...(dash ? { "line-dasharray": dash } : {}) } });
    setOverlay(map, "matched", { data: fc(parts), layers: () => [
      { id: "m-case", type: "line", layout: { ...vis(layers.matched), "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 7, "line-opacity": 0.85 } },
      kinds("m-solid", ["matched", "join", "recorded"], null), kinds("m-dash", ["filled"], [2, 1.4]), kinds("m-dot", ["raw", "straight"], [0.6, 1.7], 3.5),
      // A gap you chose to keep looks different from one nothing could route.
      { id: "m-kept", type: "line", filter: ["==", ["get", "kept"], true],
        layout: { ...vis(layers.matched), "line-cap": "butt", "line-join": "round" },
        paint: { "line-color": cssVar("--warn"), "line-width": 4, "line-dasharray": [0.6, 1.7] } },
    ] });
    setOverlay(map, "selected", { data: fc([]), layers: () => [
      { id: "selected", type: "line", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": cssVar("--ink"), "line-width": 10, "line-opacity": 0.28 } },
    ] });
    const stays = shown.filter((s) => s.mode === "stay" && s.pieces[0]?.coords[0]).map((s) => point(s.pieces[0].coords[0][0], s.pieces[0].coords[0][1], {}));
    setOverlay(map, "stays", { data: fc(stays), layers: () => [
      { id: "stays", type: "circle", layout: vis(layers.matched), paint: { "circle-radius": 6, "circle-color": cssVar("--panel"), "circle-stroke-color": cssVar("--m-stay"), "circle-stroke-width": 3 } },
    ] });
    // a faint line from each moved point back to where it was recorded
    const moved = raw.filter((p) => Array.isArray(edits[p.id])).map((p) => line([[p.lat, p.lon], edits[p.id]], {}));
    setOverlay(map, "moves", { data: fc(moved), layers: () => [
      { id: "moves", type: "line", paint: { "line-color": cssVar("--ink-2"), "line-width": 1.2, "line-dasharray": [1, 1.5] } },
    ] });
    const pts = raw.map((p) => {
      const e = edits[p.id];
      const [lat, lon] = Array.isArray(e) ? e : [p.lat, p.lon];
      return point(lat, lon, { id: p.id, ts: p.timestamp, acc: p.accuracy, v: p.velocity, moved: Array.isArray(e), out: e === "x" });
    });
    setOverlay(map, "pts", { data: fc(pts), layers: () => [
      { id: "pts", type: "circle", layout: vis(layers.points || editing),
        paint: {
          "circle-radius": editing ? ["interpolate", ["linear"], ["zoom"], 10, 4, 17, 7] : ["interpolate", ["linear"], ["zoom"], 9, 1.8, 17, 4],
          "circle-color": ["case", ["get", "out"], "rgba(0,0,0,0)", ["get", "moved"], cssVar("--warn"), cssVar("--ink")],
          "circle-opacity": editing ? 0.95 : (shown.length ? 0.5 : 0.85),
          "circle-stroke-color": ["case", ["get", "out"], cssVar("--bad"), cssVar("--panel")],
          "circle-stroke-width": ["case", ["get", "out"], 2, editing ? 1.5 : 0.8],
        } },
    ] });
    setOverlay(map, "ignored", { data: fc((d.dropped || []).map((p) => point(p.lat, p.lon, {}))), layers: () => [
      { id: "ignored", type: "circle", layout: vis(layers.ignored), paint: { "circle-radius": 5, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": cssVar("--m-raw"), "circle-stroke-width": 2 } },
    ] });
    setOverlay(map, "cursor", { data: fc([]), layers: () => [
      { id: "cursor", type: "circle", paint: { "circle-radius": 6, "circle-color": cssVar("--ink"), "circle-stroke-color": cssVar("--panel"), "circle-stroke-width": 2.5 } },
    ] });
  }, [d, layers, editing, edits, preview]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    const lo = sel ? Math.min(sel.a, sel.b) : 0, hi = sel ? Math.max(sel.a, sel.b) : -1;
    setData(map, "selected", fc(sel ? between(shown, lo, hi).map((cs) => line(cs, {})) : []));
  }, [sel, d, preview]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    let pos = null;
    if (cursor != null) {
      if (shown.length) pos = positionAt(shown, cursor);
      else if (raw.length) { const p = raw.reduce((b, x) => (Math.abs(x.timestamp - cursor) < Math.abs(b.timestamp - cursor) ? x : b), raw[0]); pos = [p.lat, p.lon]; }
    }
    setData(map, "cursor", fc(pos ? [point(pos[0], pos[1], {})] : []));
  }, [cursor, d, preview]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest("input, select, textarea")) return;
      if (e.key === "Escape") {
        if (sel) setSel(null); else if (preview) setPreview(null);
        else if (editing) { setEditing(false); setPending({}); } else navigate("#/");
      }
      if (e.key === "j" && d?.next) navigate(`#/trip/${d.next}`);
      if (e.key === "k" && d?.prev) navigate(`#/trip/${d.prev}`);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [d, sel, editing, preview]);

  // ---------------------------------------------------------------- actions
  const zoomTo = (a, b) => {
    const cs = between(shown, a, b).flat();
    if (mapRef.current && cs.length) fitCoords(mapRef.current, cs, 80);
  };
  const pick = (p) => {
    if (!d || !shown.length) return;
    if (p.click != null) {
      const s = shown.find((x) => p.click >= x.t0 && p.click <= x.t1);
      if (s) { setSel({ a: s.t0, b: s.t1, seg: s.id }); zoomTo(s.t0, s.t1); } else setSel(null);
      return;
    }
    setSel({ a: p.a, b: p.b, live: p.live });
  };
  const scrub = useScrub(T0, T1, pick, setCursor);
  const choose = (a, b, mode) => act("Rematching", "/range", { t0: a, t1: b, mode });
  const showGap = (g) => { setFocusGap(g.t0); setSel({ a: g.t0, b: g.t1 }); zoomTo(g.t0 - 30, g.t1 + 30); };
  gapRef.current = (t0) => {
    const g = gaps.find((x) => x.t0 === t0);
    setTab("gaps");
    if (g) showGap(g);
  };
  const setGap = (g, route, force = false) => act(route ? "Routing that gap" : "Keeping that gap", "/gaps", { t0: g.t0, t1: g.t1, route, force });
  const setAllGaps = (route, force = false) => act("Rematching", "/gaps", { route, force });
  async function openVersion(v) {
    if (v === t.version) { setPreview(null); return; }
    try {
      const x = await api(`/trips/${id}/versions/${v}`);
      setPreview(x); setSel(null);
    } catch (e) { toast(e.message, "bad"); }
  }

  if (err) return html`<div class="doc"><div class="note bad">${err}</div><p><a href="#/">Back to trips</a></p></div>`;
  if (!d) return html`<div class="doc faint">Loading…</div>`;

  const q = (preview ? preview.quality : t.quality) || {};
  const sp = speeds(raw);
  const vmax = Math.max(20, Math.ceil(([...sp].sort((x, y) => x - y)[Math.floor(sp.length * 0.98)] || 20) / 10) * 10);
  const X = (ts) => ((ts - T0) / span) * 100;
  const stripW = stripRef.current?.clientWidth || 600;
  const selLeft = sel ? 14 + (X((sel.a + sel.b) / 2) / 100) * (stripW - 28) : 0;
  const state = t.state === "new" ? ["", "Not matched yet"] : t.state === "failed" ? ["bad", "Couldn't be matched"]
    : t.state === "review" ? ["warn", "Needs a look"] : t.state === "locked" ? ["", "Locked"] : ["good", "Matched"];
  const pushChip = t.push === "pushed" ? html`<span class="chip good"><span class="d"></span>In Dawarich</span>`
    : t.push === "outdated" ? html`<span class="chip warn"><span class="d"></span>Changed since push</span>` : null;
  const gapMode = t.gap_mode || "";
  const accountGaps = me?.settings?.strength === "strict" ? "keep" : "route";
  const refused = gaps.filter((g) => !g.routed && !g.kept && g.why.length);
  const canEdit = !busy && !preview;
  const area = `0,72 ${raw.map((p, i) => `${(X(p.timestamp) * 10).toFixed(1)},${(70 - (Math.min(sp[i], vmax) / vmax) * 60).toFixed(1)}`).join(" ")} 1000,72`;

  return html`<div class="tripv">
    <header class="tv-head">
      <button class="icon bare" title="All trips (Esc)" onClick=${() => navigate("#/")}><${Icon} name="back" /></button>
      <div class="tv-title grow">
        <h1>${day(t.start)}, ${time(t.start)}–${time(t.end)}</h1>
        <div class="num">${km(t.km)} · ${duration(t.end - t.start)} · ${plural(t.n_points, "GPS point")}</div>
      </div>
      <span class=${`chip ${state[0]}`}><span class="d"></span>${state[1]}</span>
      ${pushChip}
      <div class="row" style="gap:4px">
        <button class="icon" title="Earlier trip (k)" disabled=${!d.prev} onClick=${() => navigate(`#/trip/${d.prev}`)}><${Icon} name="back" small /></button>
        <button class="icon" title="Later trip (j)" disabled=${!d.next} onClick=${() => navigate(`#/trip/${d.next}`)}><${Icon} name="next" small /></button>
      </div>
      ${!t.version ? html`<button class="go" disabled=${!!busy} onClick=${() => act("Matching", "/recalculate")}>Match this trip</button>`
        : t.push === "pushed" ? html`<${Confirm} disabled=${!!busy} armedLabel="Put the original GPS back?" onConfirm=${() => act("Reverting", "/revert")}>Revert in Dawarich<//>`
        : html`<button class="go" disabled=${!!busy || !t.closed || t.locked} onClick=${() => act("Pushing", "/push")}
            title=${!t.closed ? "This trip is still going" : t.locked ? "Unlock the trip first" : "Replace this trip's points in Dawarich with the matched route"}>
            ${busy === "Pushing" ? "Pushing…" : t.push === "outdated" ? "Push changes" : "Push to Dawarich"}</button>`}
      <${Menu} cls="icon" title="More" label=${html`<${Icon} name="more" />`}>
        <button disabled=${!t.version || d.history.length < 2} onClick=${() => act("Undoing", "/undo")}><${Icon} name="undo" small />Undo last change</button>
        <button onClick=${() => act("Rematching", "/recalculate")}><${Icon} name="sync" small />Match again from scratch</button>
        <button onClick=${() => act(t.locked ? "Unlocking" : "Locking", "/lock", { locked: !t.locked })}>${t.locked ? "Unlock trip" : "Lock trip (skip automatic runs)"}</button>
        ${t.push === "outdated" && html`<button onClick=${() => act("Reverting", "/revert")}>Revert in Dawarich</button>`}
        ${t.version > 0 && html`<div class="sep"></div>
          <a href=${apiUrl(`/trips/${id}/export.gpx`)} download>Download GPX</a>
          <a href=${apiUrl(`/trips/${id}/export.geojson`)} download>Download GeoJSON</a>`}
      <//>
    </header>
    <div class=${`tv-body ${busy ? "busy" : ""}`} aria-busy=${!!busy}>
      <div class="tv-main">
        <div class="tv-map">
          <div class="map" ref=${mapEl}></div>
          <div class="float tl">
            <${Toggle} on=${layers.matched} disabled=${!shown.length} onChange=${(v) => setLayers({ ...layers, matched: v })}><i class="k"></i>Matched</${Toggle}>
            <${Toggle} on=${layers.track || !shown.length} disabled=${!shown.length} onChange=${(v) => setLayers({ ...layers, track: v })}><i class="k dash"></i>Original</${Toggle}>
            <${Toggle} on=${layers.points || editing} disabled=${editing} onChange=${(v) => setLayers({ ...layers, points: v })}><i class="k pt"></i>Points</${Toggle}>
          </div>
          ${preview && html`<div class="float tc" role="status">
            <span><b>Version ${preview.version}</b> <span class="dim">· ${preview.reason} · ${ago(preview.created_at)}</span></span>
            <button class="sm go" disabled=${!!busy} onClick=${() => act("Restoring", "/restore", { version: preview.version })}>Go back to this</button>
            <button class="sm" onClick=${() => setPreview(null)}>Show current</button></div>`}
          ${editing && html`<div class="float bc">
            <span class="small">${pendingCount ? plural(pendingCount, "change") : "Drag a point to move it, click one to leave it out"}</span>
            <button class="go sm" disabled=${!pendingCount} onClick=${async () => {
              const x = await act("Rematching", "/points", { edits }, "PUT");
              if (x) { setPending({}); setEditing(false); }
            }}>Apply and rematch</button>
            <button class="sm" onClick=${() => { setPending({}); setEditing(false); }}>${pendingCount ? "Discard" : "Done"}</button></div>`}
        </div>
        <div class="strip" ref=${stripRef}>
          ${sel && !sel.live && canEdit && tab !== "gaps" && html`<${Chooser} range=${sel} segments=${shown} left=${selLeft} onChoose=${choose} onCancel=${() => setSel(null)} />`}
          <div class="track" ...${scrub} role="group" aria-label="Trip timeline: click a part, or drag across a stretch, to change its mode">
            ${shown.map((s) => html`<div class=${`s ${s.mode === "stay" ? "stay" : ""} ${sel?.seg === s.id ? "on" : ""}`}
              style=${`${modeVar(s.mode)};left:${X(s.t0)}%;width:${Math.max(0.2, X(s.t1) - X(s.t0))}%`}
              title=${`${MODES[s.mode]?.label} ${time(s.t0)}–${time(s.t1)} · ${km(s.km)}`}>
              ${(s.t1 - s.t0) / span > 0.05 && html`<${Icon} name=${s.mode} />`}</div>`)}
            ${!shown.length && html`<div class="s small" style=${`${modeVar("none")};left:0;width:100%`}>Not matched yet</div>`}
            ${sel && html`<div class="band" style=${`left:${X(Math.min(sel.a, sel.b))}%;width:${Math.abs(X(sel.b) - X(sel.a))}%`}></div>`}
            ${cursor != null && html`<div class="cur" style=${`left:${X(cursor)}%`}></div>`}
          </div>
          <div class="chart" ...${scrub}>
            <span class="max num">${vmax} km/h</span>
            <svg viewBox="0 0 1000 72" preserveAspectRatio="none" aria-hidden="true">
              <line class="grid" x1="0" x2="1000" y1="36" y2="36" vector-effect="non-scaling-stroke" />
              <polygon class="area" points=${area} />
              <polyline class="ln" vector-effect="non-scaling-stroke" points=${raw.map((p, i) => `${(X(p.timestamp) * 10).toFixed(1)},${(70 - (Math.min(sp[i], vmax) / vmax) * 60).toFixed(1)}`).join(" ")} />
            </svg>
            ${sel && html`<div style=${`position:absolute;top:0;bottom:0;left:${X(Math.min(sel.a, sel.b))}%;width:${Math.abs(X(sel.b) - X(sel.a))}%;background:color-mix(in srgb, var(--ink) 9%, transparent);pointer-events:none`}></div>`}
            ${cursor != null && html`<div class="cur" style=${`left:${X(cursor)}%`}></div>`}
          </div>
          <div class="axis num"><span>${time(T0)}</span><span>${cursor != null ? time(cursor) : preview ? "Looking at an earlier version" : shown.length ? "Click a part, or drag across a stretch, to change its mode" : ""}</span><span>${time(T1)}</span></div>
        </div>
      </div>
      <aside class="side2">
        <nav class="tabs" role="tablist">
          ${[["route", "Route", shown.filter((s) => s.mode !== "stay").length], ["gaps", "Gaps", gaps.length], ["points", "Points", raw.length], ["history", "History", d.history.length]]
            .map(([k, l, n]) => html`<button role="tab" aria-selected=${tab === k} class=${tab === k ? "on" : ""} onClick=${() => setTab(k)}>${l}${n ? html` <span class="n num">${n}</span>` : ""}</button>`)}
        </nav>
        <div class="pane">
          ${me?.dry_run && html`<section><div class="note warn">Dry run is on: pushing shows what would change but leaves Dawarich alone.</div></section>`}
          ${t.error && html`<section><div class="note bad">${t.error}</div></section>`}
          ${tab === "route" && html`
            ${(preview ? preview.version : t.version) > 0 && html`<section>
              <div class="quality" title="detailed enough to keep · on the map · routed across a gap · GPS kept">
                <i class="recorded" style=${`width:${pct(q.recorded_frac || 0)}`}></i><i class="matched" style=${`width:${pct(q.matched_frac)}`}></i><i class="filled" style=${`width:${pct(q.filled_frac)}`}></i><i class="raw" style=${`width:${pct(q.raw_frac)}`}></i></div>
              <div class="qlegend num">
                ${q.recorded_frac > 0 && html`<span><i style="background:var(--rec)"></i>${pct(q.recorded_frac)} as recorded</span>`}
                ${(!q.recorded_frac || q.matched_frac > 0) && html`<span><i style="background:var(--good)"></i>${pct(q.matched_frac)} on the map</span>`}
                ${(!q.recorded_frac || q.filled_frac > 0) && html`<span><i style="background:color-mix(in srgb, var(--good) 45%, var(--sunk))"></i>${pct(q.filled_frac)} routed gaps</span>`}
                ${(!q.recorded_frac || q.raw_frac > 0) && html`<span><i style="background:var(--m-raw)"></i>${pct(q.raw_frac)} GPS kept</span>`}
              </div>
              ${q.matched_frac > 0 && html`<p class="small faint">Your GPS sits ${Math.round(q.p50 || 0)} m from the line on average.</p>`}
              ${!preview && (hasRecorded || t.snap_recorded) && html`<div class="note"><div>
                ${t.snap_recorded ? "This trip was recorded in fine detail, but you chose to snap it to the map."
                  : "Recorded in fine detail (a point every second or two, very smooth), so those parts are kept exactly as recorded rather than snapped to the map."}
                <div class="actions" style="margin-top:8px"><button class="sm" disabled=${!canEdit || !!busy}
                  onClick=${() => act(t.snap_recorded ? "keep" : "snap", "/recorded", { snap: !t.snap_recorded }, "PUT")}>
                  ${t.snap_recorded ? "Keep it as recorded" : "Snap to the map instead"}</button></div></div></div>`}
              ${t.state === "review" && !preview && html`<div class="note warn">Usually one part has the wrong mode, or a few points are off. Drag across that part on the timeline to fix it.</div>`}
            </section>`}
            <section>
              ${!shown.length && html`<p class="small dim">Still raw GPS. Matching snaps it to roads, paths and railways; you can change any part afterwards.</p>`}
              <div class="segs">${shown.map((s) => html`<div class=${`sg ${s.mode === "stay" ? "stay" : ""} ${sel?.seg === s.id ? "on" : ""}`} style=${modeVar(s.mode)}
                  onClick=${() => { setSel({ a: s.t0, b: s.t1, seg: s.id }); zoomTo(s.t0, s.t1); }}>
                <span class="bar4"></span>
                <div><div class="t"><b>${MODES[s.mode]?.label}</b> <span class="num dim">${time(s.t0)}–${time(s.t1)}</span></div>
                  <div class="m num">${duration(s.t1 - s.t0)}${s.km > 0 ? ` · ${km(s.km)}` : ""}${s.km > 0 && s.t1 > s.t0 ? ` · ${Math.round((s.km * 3600) / (s.t1 - s.t0))} km/h` : ""}${s.forced ? " · set by you" : ""}</div></div>
                <select aria-label="Mode" value=${s.mode} disabled=${!canEdit} onClick=${(e) => e.stopPropagation()} onChange=${(e) => choose(s.t0, s.t1, e.target.value)}>
                  ${PICKABLE.map((m) => html`<option value=${m}>${MODES[m].label}</option>`)}</select>
              </div>`)}</div>
              ${shown.length > 0 && html`<p class="help">Pick a mode per part here, or drag across the timeline to set one for any stretch.</p>`}
            </section>`}
          ${tab === "gaps" && html`
            <section>
              <div class="row wrap" style="justify-content:space-between">
                <span class="small dim">Gaps in this trip</span>
                <div class="seg sm" role="radiogroup" aria-label="Every gap in this trip">
                  <button class=${gapMode === "fill" ? "on" : ""} disabled=${!canEdit || !t.closed} onClick=${() => setAllGaps(true)}>Route all</button>
                  <button class=${gapMode === "strict" ? "on" : ""} disabled=${!canEdit || !t.closed} onClick=${() => setAllGaps(false)}>Keep all</button>
                  <button class=${gapMode === "" ? "on" : ""} disabled=${!canEdit || !t.closed} onClick=${() => setAllGaps(null)} title="Your account setting">Default (${accountGaps})</button>
                </div>
              </div>
              <p class="help">Where the GPS dropped out, mapmatch either routes along the road or path, or keeps the line as recorded. A gap is only routed when the timing fits; you can overrule that.</p>
            </section>
            <section>
              ${!t.version ? html`<p class="small faint">Match the trip to see where its gaps are.</p>`
                : !gaps.length ? html`<p class="small faint">No gaps: the signal never dropped long enough to leave one.</p>`
                : html`<div class="gaplist">${gaps.map((g) => html`<div key=${g.t0} class=${`gaprow ${focusGap === g.t0 ? "on" : ""}`} onClick=${() => showGap(g)}>
                    <span class="num"><b>${time(g.t0)}–${time(g.t1)}</b></span>
                    <div class="seg sm" onClick=${(e) => e.stopPropagation()}>
                      <button class=${g.routed ? "on" : ""} disabled=${!canEdit || !t.closed}
                        title=${!g.routed && g.why.length ? "The timing says this is faster or longer than the mode allows; route it anyway" : "Route along the road or path"}
                        onClick=${() => !g.routed && setGap(g, true, g.why.length > 0)}>${!g.routed && g.why.length ? "Route anyway" : "Route"}</button>
                      <button class=${!g.routed ? "on" : ""} disabled=${!canEdit || !t.closed} onClick=${() => g.routed && setGap(g, false)}>Keep GPS</button>
                    </div>
                    <div class="meta num">
                      <span>${duration(g.t1 - g.t0)} · ${km(g.km)}</span>
                      ${g.routed && g.overrode && html`<span class="chip warn">routed over the speed check</span>`}
                      ${g.kept && html`<span class="chip">kept by you</span>`}
                      ${!g.routed && !g.kept && g.why.length > 0 && html`<span class="chip warn">${g.why.includes("too_fast") ? "too fast to route" : "route too long"}</span>`}
                    </div>
                  </div>`)}</div>
                  ${refused.length > 1 && html`<button class="sm" disabled=${!canEdit || !t.closed} onClick=${() => setAllGaps(true, true)}>Route all ${refused.length} refused gaps anyway</button>`}
                  <p class="help">Click a gap, or its dashed line on the map, to see it.</p>`}
            </section>`}
          ${tab === "points" && html`
            <section>
              <p class="small dim">${plural(raw.length, "point")} recorded${t.point_edits ? `, ${plural(t.point_edits, "correction")} by you` : ""}. Your originals are always kept; corrections only change the match.</p>
              <div class="row wrap">
                <button class=${editing ? "go" : ""} disabled=${!!preview} onClick=${() => { setEditing(!editing); setPending({}); }}><${Icon} name="move" small />${editing ? "Stop moving points" : "Move points"}</button>
                ${t.point_edits > 0 && !editing && html`<${Confirm} armedLabel="Drop all corrections?" onConfirm=${() => act("Rematching", "/points", { edits: {} }, "PUT")}>Reset corrections<//>`}
              </div>
            </section>
            ${d.dropped?.length > 0 && html`<section>
              <label class="check small"><input type="checkbox" checked=${layers.ignored} onChange=${(e) => setLayers({ ...layers, ignored: e.target.checked })} />
                Show the ${plural(d.dropped.length, "point")} the matcher ignored</label>
              <p class="help">Duplicates, jumps and fixes too inaccurate to use. They stay in your archive.</p>
            </section>`}`}
          ${tab === "history" && html`
            <section>
              <p class="help">Every change makes a new version. Click one to see it on the map, then go back to it if you prefer it.</p>
              <div class="hist">${d.history.map((h) => {
                const cur = h.version === t.version;
                return html`<div key=${h.version} class=${`hrow ${(preview?.version ?? t.version) === h.version ? "on" : ""}`} onClick=${() => openVersion(h.version)}>
                  <span class="v num">v${h.version}</span>
                  <span class="ell" title=${h.reason}>${h.reason}</span>
                  <span class="faint small" title=${full(h.created_at)}>${ago(h.created_at)}</span>
                  <div class="meta num">
                    ${cur && html`<span class="chip ink">current</span>`}
                    ${t.pushed_version === h.version && html`<span class="chip good">in Dawarich</span>`}
                    <span>${pct(h.quality?.matched_frac + (h.quality?.filled_frac || 0))} on roads and paths</span>
                    ${h.overrides?.length > 0 && html`<span>· ${plural(h.overrides.length, "mode choice")}</span>`}
                  </div>
                </div>`;
              })}</div>
              ${t.pushed_at && html`<p class="small faint">Pushed v${t.pushed_version} ${ago(t.pushed_at)}.</p>`}
            </section>`}
        </div>
      </aside>
    </div>
  </div>`;
}
