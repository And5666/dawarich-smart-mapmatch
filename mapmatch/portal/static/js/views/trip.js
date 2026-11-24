// One trip: the map, a timeline to pick any stretch and set its mode, segments, points, history.
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
  return html`<div class="chooser" style=${`left:${Math.max(160, Math.min(left, innerWidth - 180))}px`} role="dialog" aria-label="Set mode">
    <div class="row small"><b class="num">${time(t0)}–${time(t1)}</b><span class="faint">${duration(t1 - t0)}${len ? ` · ${km(len / 1000)}` : ""}</span>
      <span class="grow"></span><button class="sm bare" onClick=${onCancel} title="Cancel (Esc)"><${Icon} name="x" small /></button></div>
    <div class="modes">${PICKABLE.map((m) => html`<button style=${modeVar(m)} onClick=${() => onChoose(t0, t1, m)}>
      <${Icon} name=${m} />${MODES[m].label}</button>`)}</div>
  </div>`;
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
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const stripRef = useRef(null);
  const editRef = useRef({ editing: false, raw: [] });

  const load = () => api(`/trips/${id}`).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id]);

  async function act(label, path, body, method = "POST") {
    setBusy(label);
    try {
      const x = await api(`/trips/${id}${path}`, { method, body });
      setD(x); setSel(null); refreshData();
      const r = x.result;
      if (r) toast(r.dry_run ? "Dry run: Dawarich was not changed" : r.post != null ? `Pushed to Dawarich (${plural(r.post, "point")})` : `Original GPS restored in Dawarich (${plural(r.restore, "point")})`);
      return x;
    } catch (e) { toast(e.message, "bad"); }
    finally { setBusy(""); }
  }

  const t = d?.trip;
  const segs = d?.segments || [];
  const T0 = d ? Math.min(t.start, ...segs.map((s) => s.t0)) : 0;
  const T1 = d ? Math.max(t.end, ...segs.map((s) => s.t1)) : 1;
  const span = Math.max(1, T1 - T0);
  const raw = d?.raw || [];
  const edits = useMemo(() => {
    const e = {};
    for (const p of raw) if (p.edit != null) e[p.id] = p.edit;
    for (const [k, v] of Object.entries(pending)) { if (v == null) delete e[k]; else e[k] = v; }
    return e;
  }, [raw, pending]);
  const pendingCount = Object.keys(pending).length;
  editRef.current = { editing, raw, edits };

  // ---------------------------------------------------------------- map
  useEffect(() => {
    if (!d || mapRef.current) return;
    const map = createMap(mapEl.current, theme);
    mapRef.current = map;
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    for (const lid of ["m-solid", "m-dash", "m-dot"]) {
      map.on("click", lid, (e) => { if (!editRef.current.editing) { const s = e.features[0].properties; setSel({ a: s.t0, b: s.t1, seg: s.sid }); } });
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
    // the original track, exactly as recorded
    setOverlay(map, "track", { data: fc(raw.length > 1 ? [line(raw.map((p) => [p.lat, p.lon]), {})] : []), layers: () => [
      { id: "track-case", type: "line", layout: { ...vis(layers.track || !segs.length), "line-join": "round", "line-cap": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 5.5, "line-opacity": 0.9 } },
      { id: "track", type: "line", layout: { ...vis(layers.track || !segs.length), "line-join": "round", "line-cap": "round" },
        paint: { "line-color": cssVar("--m-none"), "line-width": 2.6, "line-dasharray": [2.2, 1.4] } },
    ] });
    const parts = [];
    for (const s of segs) {
      if (s.mode === "stay") continue;
      for (const p of s.pieces) if (p.coords.length > 1) parts.push(line(p.coords, { sid: s.id, m: s.mode, k: p.kind, t0: s.t0, t1: s.t1 }));
    }
    const kinds = (lid, ks, dash, w = 4) => ({ id: lid, type: "line", filter: ["in", ["get", "k"], ["literal", ks]],
      layout: { ...vis(layers.matched), "line-cap": dash ? "butt" : "round", "line-join": "round" },
      paint: { "line-color": colorExpr(), "line-width": w, ...(dash ? { "line-dasharray": dash } : {}) } });
    setOverlay(map, "matched", { data: fc(parts), layers: () => [
      { id: "m-case", type: "line", layout: { ...vis(layers.matched), "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 7, "line-opacity": 0.85 } },
      kinds("m-solid", ["matched", "join"], null), kinds("m-dash", ["filled"], [2, 1.4]), kinds("m-dot", ["raw", "straight"], [0.6, 1.7], 3.5),
    ] });
    setOverlay(map, "selected", { data: fc([]), layers: () => [
      { id: "selected", type: "line", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": cssVar("--ink"), "line-width": 9, "line-opacity": 0.3 } },
    ] });
    const stays = segs.filter((s) => s.mode === "stay" && s.pieces[0]?.coords[0]).map((s) => point(s.pieces[0].coords[0][0], s.pieces[0].coords[0][1], {}));
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
          "circle-opacity": editing ? 0.95 : (segs.length ? 0.5 : 0.85),
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
  }, [d, layers, editing, edits]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    const lo = sel ? Math.min(sel.a, sel.b) : 0, hi = sel ? Math.max(sel.a, sel.b) : -1;
    setData(map, "selected", fc(sel ? between(segs, lo, hi).map((cs) => line(cs, {})) : []));
  }, [sel, d]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    let pos = null;
    if (cursor != null) {
      if (segs.length) pos = positionAt(segs, cursor);
      else if (raw.length) { const p = raw.reduce((b, x) => (Math.abs(x.timestamp - cursor) < Math.abs(b.timestamp - cursor) ? x : b), raw[0]); pos = [p.lat, p.lon]; }
    }
    setData(map, "cursor", fc(pos ? [point(pos[0], pos[1], {})] : []));
  }, [cursor, d]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest("input, select, textarea")) return;
      if (e.key === "Escape") { if (sel) setSel(null); else if (editing) { setEditing(false); setPending({}); } else navigate("#/"); }
      if (e.key === "j" && d?.next) navigate(`#/trip/${d.next}`);
      if (e.key === "k" && d?.prev) navigate(`#/trip/${d.prev}`);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [d, sel, editing]);

  // ---------------------------------------------------------------- timeline
  const zoomTo = (a, b) => {
    const cs = between(segs, a, b).flat();
    if (mapRef.current && cs.length) fitCoords(mapRef.current, cs, 80);
  };
  const pick = (p) => {
    if (!d || !segs.length) return;
    if (p.click != null) {
      const s = segs.find((x) => p.click >= x.t0 && p.click <= x.t1);
      if (s) { setSel({ a: s.t0, b: s.t1, seg: s.id }); zoomTo(s.t0, s.t1); } else setSel(null);
      return;
    }
    setSel({ a: p.a, b: p.b, live: p.live });
  };
  const scrub = useScrub(T0, T1, pick, setCursor);
  const choose = (a, b, mode) => act("Rematching", "/range", { t0: a, t1: b, mode });

  if (err) return html`<div class="doc"><div class="note bad">${err}</div><p><a href="#/">Back to trips</a></p></div>`;
  if (!d) return html`<div class="doc faint">Loading…</div>`;

  const q = t.quality || {};
  const sp = speeds(raw);
  const vmax = Math.max(20, Math.ceil(([...sp].sort((x, y) => x - y)[Math.floor(sp.length * 0.98)] || 20) / 10) * 10);
  const X = (ts) => ((ts - T0) / span) * 100;
  const stripW = stripRef.current?.clientWidth || 600;
  const selLeft = sel ? 14 + (X((sel.a + sel.b) / 2) / 100) * (stripW - 28) : 0;
  const state = t.state === "new" ? ["", "Not matched yet"] : t.state === "failed" ? ["bad", "Couldn't be matched"]
    : t.state === "review" ? ["warn", "Needs a look"] : t.state === "locked" ? ["", "Locked"] : ["good", "Matched"];
  const pushState = t.push === "pushed" ? "in Dawarich" : t.push === "outdated" ? "changed since pushed" : "not in Dawarich yet";

  return html`<div class="tripv">
    <header class="tv-head">
      <button class="icon bare" title="All trips (Esc)" onClick=${() => navigate("#/")}><${Icon} name="back" /></button>
      <div class="tv-title grow">
        <h1>${day(t.start)}, ${time(t.start)}–${time(t.end)}</h1>
        <div class="num">${km(t.km)} · ${duration(t.end - t.start)} · ${plural(t.n_points, "GPS point")}</div>
      </div>
      <div class="tv-state"><span class=${`dot ${state[0]}`}></span>${state[1]}${t.version ? html`<span class="faint">· ${pushState}</span>` : ""}</div>
      <button class="icon" title="Earlier trip (k)" disabled=${!d.prev} onClick=${() => navigate(`#/trip/${d.prev}`)}><${Icon} name="back" small /></button>
      <button class="icon" title="Later trip (j)" disabled=${!d.next} onClick=${() => navigate(`#/trip/${d.next}`)}><${Icon} name="next" small /></button>
      ${!t.version ? html`<button class="go" disabled=${!!busy} onClick=${() => act("Matching", "/recalculate")}>Match this trip</button>`
        : t.push === "pushed" ? html`<${Confirm} disabled=${!!busy} armedLabel="Put the original GPS back?" onConfirm=${() => act("Reverting", "/revert")}>Revert in Dawarich<//>`
        : html`<button class="go" disabled=${!!busy || !t.closed || t.locked} onClick=${() => act("Pushing", "/push")}
            title=${!t.closed ? "This trip is still going" : t.locked ? "Unlock the trip first" : "Replace this trip's points in Dawarich with the matched route"}>
            ${t.push === "outdated" ? "Push changes" : "Push to Dawarich"}</button>`}
      <${Menu} cls="icon" title="More" label=${html`<${Icon} name="more" />`}>
        <button disabled=${!t.version || d.history.length < 2} onClick=${() => act("Undoing", "/undo")}>Undo last change</button>
        <button onClick=${() => act("Rematching", "/recalculate")}>Rematch from scratch${t.overrides.length ? " (drops your mode choices)" : ""}</button>
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
          <div class="float tl layers">
            <label class="check"><input type="checkbox" checked=${layers.matched} disabled=${!segs.length} onChange=${(e) => setLayers({ ...layers, matched: e.target.checked })} />Matched route</label>
            <label class="check"><input type="checkbox" checked=${layers.track || !segs.length} disabled=${!segs.length} onChange=${(e) => setLayers({ ...layers, track: e.target.checked })} />Original track</label>
            <label class="check"><input type="checkbox" checked=${layers.points || editing} disabled=${editing} onChange=${(e) => setLayers({ ...layers, points: e.target.checked })} />GPS points</label>
            ${d.dropped?.length > 0 && html`<label class="check opt"><input type="checkbox" checked=${layers.ignored} onChange=${(e) => setLayers({ ...layers, ignored: e.target.checked })} />Ignored points (${d.dropped.length})</label>`}
            ${segs.length > 0 && html`<div class="opt small faint row" style="gap:10px;padding-top:2px">
              <span class="row" style="gap:4px"><i class="k"></i>on map</span><span class="row" style="gap:4px"><i class="k dash"></i>routed gap</span>
              <span class="row" style="gap:4px"><i class="k dot"></i>GPS kept</span></div>`}
          </div>
          ${editing && html`<div class="float bc">
            <span class="small">${pendingCount ? plural(pendingCount, "change") : "Drag a point to move it · click one to leave it out"}</span>
            <button class="go sm" disabled=${!pendingCount} onClick=${async () => {
              const x = await act("Rematching", "/points", { edits }, "PUT");
              if (x) { setPending({}); setEditing(false); }
            }}>Apply and rematch</button>
            <button class="sm" onClick=${() => { setPending({}); setEditing(false); }}>${pendingCount ? "Discard" : "Done"}</button></div>`}
        </div>
        <div class="strip" ref=${stripRef}>
          ${sel && !sel.live && html`<${Chooser} range=${sel} segments=${segs} left=${selLeft} onChoose=${choose} onCancel=${() => setSel(null)} />`}
          <div class="track" ...${scrub} role="group" aria-label="Trip timeline: click a part, or drag across a stretch, to change its mode">
            ${segs.map((s) => html`<div class=${`s ${s.mode === "stay" ? "stay" : ""} ${sel?.seg === s.id ? "on" : ""}`}
              style=${`${modeVar(s.mode)};left:${X(s.t0)}%;width:${Math.max(0.2, X(s.t1) - X(s.t0))}%`}
              title=${`${MODES[s.mode]?.label} ${time(s.t0)}–${time(s.t1)} · ${km(s.km)}`}>
              ${(s.t1 - s.t0) / span > 0.05 && html`<${Icon} name=${s.mode} />`}</div>`)}
            ${!segs.length && html`<div class="s small" style=${`${modeVar("none")};left:0;width:100%`}>Not matched yet</div>`}
            ${sel && html`<div class="band" style=${`left:${X(Math.min(sel.a, sel.b))}%;width:${Math.abs(X(sel.b) - X(sel.a))}%`}></div>`}
            ${cursor != null && html`<div class="cur" style=${`left:${X(cursor)}%`}></div>`}
          </div>
          <div class="chart" ...${scrub}>
            <span class="max num">${vmax} km/h</span>
            <svg viewBox="0 0 1000 72" preserveAspectRatio="none" aria-hidden="true">
              <line class="grid" x1="0" x2="1000" y1="36" y2="36" vector-effect="non-scaling-stroke" />
              <polyline class="ln" vector-effect="non-scaling-stroke" points=${raw.map((p, i) => `${(X(p.timestamp) * 10).toFixed(1)},${(70 - (Math.min(sp[i], vmax) / vmax) * 60).toFixed(1)}`).join(" ")} />
            </svg>
            ${sel && html`<div style=${`position:absolute;top:0;bottom:0;left:${X(Math.min(sel.a, sel.b))}%;width:${Math.abs(X(sel.b) - X(sel.a))}%;background:color-mix(in srgb, var(--ink) 10%, transparent);pointer-events:none`}></div>`}
            ${cursor != null && html`<div class="cur" style=${`left:${X(cursor)}%`}></div>`}
          </div>
          <div class="axis num"><span>${time(T0)}</span><span>${cursor != null ? time(cursor) : segs.length ? "Click a part, or drag across a stretch, to change its mode" : ""}</span><span>${time(T1)}</span></div>
        </div>
      </div>
      <aside class="side2">
        ${me?.dry_run && html`<section><div class="note warn">Dry run is on: pushing shows what would change but leaves Dawarich alone.</div></section>`}
        ${t.error && html`<section><div class="note bad">${t.error}</div></section>`}
        <section>
          <h2>Segments</h2>
          ${!segs.length && html`<p class="small dim" style="margin:0">Still raw GPS. Matching snaps it to roads, paths and railways; you can change any part afterwards.</p>`}
          <div class="segs">${segs.map((s) => html`<div class=${`sg ${s.mode === "stay" ? "stay" : ""} ${sel?.seg === s.id ? "on" : ""}`} style=${modeVar(s.mode)}
              onClick=${() => { setSel({ a: s.t0, b: s.t1, seg: s.id }); zoomTo(s.t0, s.t1); }}>
            <span class="bar4"></span>
            <div><div class="t"><b>${MODES[s.mode]?.label}</b> <span class="num dim">${time(s.t0)}–${time(s.t1)}</span></div>
              <div class="m num">${duration(s.t1 - s.t0)}${s.km > 0 ? ` · ${km(s.km)}` : ""}${s.km > 0 && s.t1 > s.t0 ? ` · ${Math.round((s.km * 3600) / (s.t1 - s.t0))} km/h` : ""}${s.forced ? " · set by you" : ""}</div></div>
            <select aria-label="Mode" value=${s.mode} onClick=${(e) => e.stopPropagation()} onChange=${(e) => choose(s.t0, s.t1, e.target.value)}>
              ${PICKABLE.map((m) => html`<option value=${m}>${MODES[m].label}</option>`)}</select>
          </div>`)}</div>
        </section>
        ${t.version > 0 && html`<section>
          <h2>How well it fits</h2>
          <div class="quality" title="green: on the map · pale green: routed across a gap · red: original GPS kept">
            <i class="matched" style=${`width:${pct(q.matched_frac)}`}></i><i class="filled" style=${`width:${pct(q.filled_frac)}`}></i><i class="raw" style=${`width:${pct(q.raw_frac)}`}></i></div>
          <p class="small dim" style="margin:0">${pct(q.matched_frac)} on the map, ${pct(q.filled_frac)} routed across gaps, ${pct(q.raw_frac)} kept as recorded.
            Your GPS sits ${Math.round(q.p50 || 0)} m from the line on average.</p>
          ${t.state === "review" && html`<p class="small" style="margin:0;color:var(--warn)">Worth a look: usually one part has the wrong mode, or a few points are off. Drag across that part on the timeline, or move the points.</p>`}
        </section>`}
        <section>
          <h2>GPS points</h2>
          <p class="small dim" style="margin:0">${plural(raw.length, "point")} recorded${t.point_edits ? `, ${plural(t.point_edits, "correction")} by you` : ""}. The originals are always kept; corrections only change the match.</p>
          <div class="row">
            <button class=${editing ? "go" : ""} onClick=${() => { setEditing(!editing); setPending({}); }}><${Icon} name="move" small />${editing ? "Stop moving points" : "Move points"}</button>
            ${t.point_edits > 0 && !editing && html`<${Confirm} armedLabel="Drop all corrections?" onConfirm=${() => act("Rematching", "/points", { edits: {} }, "PUT")}>Reset corrections<//>`}
          </div>
        </section>
        ${d.history.length > 0 && html`<section>
          <h2>History</h2>
          <div class="hist">${d.history.slice(0, 8).map((h) => html`<div key=${h.version}>
            <span class=${`num ${h.version === t.version ? "" : "faint"}`}>v${h.version}</span><span class="grow ell" title=${h.reason}>${h.reason}</span>
            <span class="faint" title=${full(h.created_at)}>${ago(h.created_at)}</span></div>`)}</div>
          ${t.pushed_at && html`<p class="small faint" style="margin:0">Pushed v${t.pushed_version} ${ago(t.pushed_at)}.</p>`}
        </section>`}
      </aside>
    </div>
  </div>`;
}
