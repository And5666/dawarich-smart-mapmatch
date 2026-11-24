// Trip editor: map, segment timeline, speed chart, segment list, quality, history, push.
import {
  html, useState, useEffect, useRef, useMemo, useCallback, api, apiUrl, useStore, toast, Icon, StateBadge, PushBadge,
  ConfirmButton, MODES, EDITABLE_MODES, modeColor, modeStyle, cssVar, km, duration, time, day, full, ago, pct, navigate, refreshData,
} from "../lib.js";
import { createMap, setMapTheme, setOverlay, setData, fc, lineFeature, pointFeature, fitCoords } from "../map.js";

const KIND_LABEL = { matched: "on the map", filled: "routed across a gap", raw: "original GPS", straight: "straight line", join: "join" };

function haversine(a, b) {
  const R = 6371008.8, r = Math.PI / 180;
  const dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(x)));
}

function speedsKmh(raw) {
  const v = raw.map((p, i) => {
    if (p.velocity > 0) return p.velocity * 3.6;
    const a = raw[Math.max(0, i - 1)], b = raw[Math.min(raw.length - 1, i + 1)];
    const dt = b.timestamp - a.timestamp;
    return dt > 0 ? (haversine(a, b) / dt) * 3.6 : 0;
  });
  return v.map((_, i) => { const w = v.slice(Math.max(0, i - 1), i + 2).sort((x, y) => x - y); return w[Math.floor(w.length / 2)]; });
}

// Position on the matched line at a given time.
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

function Provenance({ prov }) {
  const order = ["matched", "filled", "raw", "straight"];
  return html`<div class="prov" title=${order.filter((k) => prov[k]).map((k) => `${pct(prov[k])} ${KIND_LABEL[k]}`).join(" · ")}>
    ${order.map((k) => prov[k] ? html`<i class=${k} style=${`width:${prov[k] * 100}%`}></i>` : null)}</div>`;
}

function SpeedChart({ raw, segments, t0, t1, cursor, onHover, onPick, splitting }) {
  const W = 1000, H = 84, PAD = 4;
  const sp = useMemo(() => speedsKmh(raw), [raw]);
  const vmax = useMemo(() => { const s = [...sp].sort((a, b) => a - b); return Math.max(20, Math.ceil((s[Math.floor(s.length * 0.98)] || 20) / 10) * 10); }, [sp]);
  const T = Math.max(1, t1 - t0);
  const x = (ts) => ((ts - t0) / T) * W;
  const y = (v) => H - PAD - (Math.min(v, vmax) / vmax) * (H - 2 * PAD - 10);
  const pts = raw.map((p, i) => `${x(p.timestamp).toFixed(1)},${y(sp[i]).toFixed(1)}`).join(" ");
  const ref = useRef(null);
  const tsAt = (e) => { const r = ref.current.getBoundingClientRect(); return t0 + ((e.clientX - r.left) / r.width) * T; };
  return html`<div class="speed-wrap"><span class="speed-max num">${vmax} km/h</span><svg class="speed" ref=${ref} viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Speed over time"
      style=${splitting ? "cursor:crosshair" : ""}
      onMouseMove=${(e) => onHover(tsAt(e))} onMouseLeave=${() => onHover(null)} onClick=${(e) => onPick(tsAt(e))}>
    ${segments.map((s) => html`<rect x=${x(s.t0)} y="0" width=${Math.max(0.5, x(s.t1) - x(s.t0))} height=${H}
      fill=${modeColor(s.mode)} opacity=${s.mode === "stay" ? 0.05 : 0.1} />`)}
    ${[0.5, 1].map((f) => html`<line class="grid" x1="0" x2=${W} y1=${y(vmax * f)} y2=${y(vmax * f)} vector-effect="non-scaling-stroke" />`)}
    <polyline class="line" points=${pts} vector-effect="non-scaling-stroke" />
    ${cursor != null && html`<line class="cursor" x1=${x(cursor)} x2=${x(cursor)} y1="0" y2=${H} vector-effect="non-scaling-stroke" />`}
  </svg></div>`;
}

export function TripEditor({ id }) {
  const me = useStore((s) => s.me);
  const theme = useStore((s) => s.theme);
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [sel, setSel] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [splitting, setSplitting] = useState(false);
  const [show, setShow] = useState({ raw: true, dropped: false });
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const ready = useRef(false);

  const load = useCallback(() => api(`/trips/${id}`).then((x) => { setD(x); setErr(""); }).catch((e) => setErr(e.message)), [id]);
  useEffect(() => { load(); }, [load]);

  async function act(label, path, body) {
    setBusy(label);
    try {
      const x = await api(`/trips/${id}${path}`, { method: "POST", body });
      setD(x);
      refreshData();
      if (x.result) {
        const r = x.result;
        toast(r.dry_run ? `Dry run: would ${r.post != null ? `write ${r.post} points and remove ${r.delete_originals + r.delete_previous_mapmatch}` : `restore ${r.restore} originals`}`
          : r.post != null ? `Pushed ${r.post} matched points to Dawarich` : `Restored ${r.restore} original points`);
      }
      return x;
    } catch (e) { toast(e.message, "error"); }
    finally { setBusy(""); }
  }

  // ---- map setup
  useEffect(() => {
    if (!d || mapRef.current) return;
    const map = createMap(mapEl.current, theme);
    mapRef.current = map;
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    for (const lid of ["trip-solid", "trip-dash", "trip-dot"]) {
      map.on("click", lid, (e) => setSel(e.features[0].properties.sid));
      map.on("mouseenter", lid, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", lid, () => { map.getCanvas().style.cursor = ""; });
    }
    map.on("click", "stays", (e) => setSel(e.features[0].properties.sid));
    map.on("mousemove", "raw", (e) => {
      const p = e.features[0].properties;
      pop.setLngLat(e.lngLat).setHTML(`<b>${time(p.ts)}</b> · ±${Math.round(p.acc || 0)} m${p.v > 0 ? ` · ${Math.round(p.v * 3.6)} km/h` : ""}`).addTo(map);
      setCursor(p.ts);
    });
    map.on("mouseleave", "raw", () => { pop.remove(); setCursor(null); });
    map.on("mousemove", "dropped", (e) => {
      const p = e.features[0].properties;
      pop.setLngLat(e.lngLat).setHTML(`<b>Ignored fix</b> · ${time(p.ts)} · ${p.why.replace("_", " ")}`).addTo(map);
    });
    map.on("mouseleave", "dropped", () => pop.remove());
    map.once("load", () => { ready.current = true; fitCoords(map, d.raw.map((p) => [p.lat, p.lon]), 60, false); });
    return () => { map.remove(); mapRef.current = null; };
  }, [d && d.trip.id]);
  useEffect(() => setMapTheme(mapRef.current, theme), [theme]);

  // ---- overlays
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    const lines = [], stays = [];
    for (const s of d.segments) {
      if (s.mode === "stay") {
        const c = s.pieces[0]?.coords[0];
        if (c) stays.push(pointFeature(c[0], c[1], { sid: s.id, mode: s.mode }));
        continue;
      }
      for (const p of s.pieces) if (p.coords.length > 1) lines.push(lineFeature(p.coords, { sid: s.id, mode: s.mode, kind: p.kind }));
    }
    const unmatched = !d.segments.length;
    setOverlay(map, "rawline", { data: fc(unmatched && d.raw.length > 1 ? [lineFeature(d.raw.map((p) => [p.lat, p.lon]), {})] : []), layers: () => [
      { id: "rawline", type: "line", layout: { visibility: show.raw ? "visible" : "none", "line-join": "round", "line-cap": "round" },
        paint: { "line-color": cssVar("--muted"), "line-width": 2.5, "line-opacity": 0.85 } },
    ] });
    const color = ["match", ["get", "mode"], ...Object.keys(MODES).flatMap((m) => [m, modeColor(m)]), modeColor("raw")];
    const lineLayer = (lid, kinds, dash, width = 4) => ({
      id: lid, type: "line", filter: ["in", ["get", "kind"], ["literal", kinds]],
      layout: { "line-cap": dash ? "butt" : "round", "line-join": "round" },
      paint: { "line-color": color, "line-width": width, ...(dash ? { "line-dasharray": dash } : {}) },
    });
    setOverlay(map, "trip", { data: fc(lines), layers: () => [
      { id: "trip-casing", type: "line", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 7, "line-opacity": 0.85 } },
      { id: "trip-sel", type: "line", filter: ["==", ["get", "sid"], -1], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--text"), "line-width": 10, "line-opacity": 0.25 } },
      lineLayer("trip-solid", ["matched", "join"], null),
      lineLayer("trip-dash", ["filled"], [2, 1.4]),
      lineLayer("trip-dot", ["raw", "straight"], [0.6, 1.6], 3.5),
    ] });
    setOverlay(map, "stays", { data: fc(stays), layers: () => [
      { id: "stays", type: "circle", paint: { "circle-radius": 7, "circle-color": cssVar("--surface"), "circle-stroke-color": modeColor("stay"), "circle-stroke-width": 3 } },
    ] });
    setOverlay(map, "raw", { data: fc(d.raw.map((p) => pointFeature(p.lat, p.lon, { ts: p.timestamp, acc: p.accuracy, v: p.velocity }))), layers: () => [
      { id: "raw", type: "circle", minzoom: unmatched ? 0 : 11, layout: { visibility: show.raw ? "visible" : "none" },
        paint: { "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, unmatched ? 2 : 1.5, 17, 4], "circle-color": cssVar("--text"),
          "circle-opacity": unmatched ? 0.8 : ["interpolate", ["linear"], ["zoom"], 11, 0.25, 15, 0.6],
          "circle-stroke-color": cssVar("--surface"), "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 13, 0, 16, 1] } },
    ] });
    setOverlay(map, "dropped", { data: fc(d.dropped.map((p) => pointFeature(p.lat, p.lon, { ts: p.timestamp, why: p._drop || "" }))), layers: () => [
      { id: "dropped", type: "circle", layout: { visibility: show.dropped ? "visible" : "none" },
        paint: { "circle-radius": 5, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": modeColor("raw"), "circle-stroke-width": 2 } },
    ] });
    setOverlay(map, "cursor", { data: fc([]), layers: () => [
      { id: "cursor", type: "circle", paint: { "circle-radius": 7, "circle-color": cssVar("--accent"), "circle-stroke-color": "#fff", "circle-stroke-width": 2.5 } },
    ] });
  }, [d, show]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getLayer("trip-sel")) return;
    map.setFilter("trip-sel", ["==", ["get", "sid"], sel ?? -1]);
  }, [sel, d]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !d) return;
    const pos = cursor != null ? positionAt(d.segments, cursor) : null;
    setData(map, "cursor", fc(pos ? [pointFeature(pos[0], pos[1], {})] : []));
  }, [cursor, d]);

  // ---- keyboard
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.closest("input, select, textarea")) return;
      if (e.key === "Escape") { if (splitting) setSplitting(false); else navigate("#/"); }
      if (e.key === "j" && d?.next) navigate(`#/trip/${d.next}`);
      if (e.key === "k" && d?.prev) navigate(`#/trip/${d.prev}`);
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [d, splitting]);

  if (err) return html`<div class="content"><div class="callout danger"><${Icon} name="alert" />${err}</div><p><a href="#/">Back to trips</a></p></div>`;
  if (!d) return html`<div class="content muted">Loading trip…</div>`;

  const t = d.trip;
  const q = t.quality || {};
  const moving = d.segments.filter((s) => s.mode !== "stay");
  const T0 = Math.min(t.start, ...d.segments.map((s) => s.t0));
  const T1 = Math.max(t.end, ...d.segments.map((s) => s.t1));
  const totalT = Math.max(1, T1 - T0);
  const zoomTo = (s) => {
    const c = s.pieces.flatMap((p) => p.coords);
    if (mapRef.current && c.length) fitCoords(mapRef.current, c, 80);
  };
  const pick = (ts) => {
    if (!splitting) return;
    setSplitting(false);
    act("Splitting", "/split", { ts: Math.round(ts) });
  };
  const canPush = t.closed && !t.locked && t.version > 0;

  return html`<div class="editor">
    <div class="ed-head">
      <button class="icon-btn ghost" title="Back to trips (Esc)" onClick=${() => navigate("#/")}><${Icon} name="back" /></button>
      <div class="ed-title">
        <h1>${day(t.start)} · ${time(t.start)}–${time(t.end)}</h1>
        <div class="meta num"><span>${km(t.km)}</span><span>${duration(t.end - t.start)}</span><span>${t.n_points} GPS fixes</span>
          <${StateBadge} state=${t.state} /><${PushBadge} push=${t.push} />${!t.closed && html`<span class="badge accent">In progress</span>`}</div>
      </div>
      <div class="ed-actions">
        <button class="icon-btn" title="Earlier trip (k)" disabled=${!d.prev} onClick=${() => navigate(`#/trip/${d.prev}`)}><${Icon} name="back" size="sm" /></button>
        <button class="icon-btn" title="Later trip (j)" disabled=${!d.next} onClick=${() => navigate(`#/trip/${d.next}`)}><${Icon} name="next" size="sm" /></button>
        <button disabled=${!!busy || d.history.length < 2} onClick=${() => act("Undoing", "/undo")} title="Go back to the previous version">
          <${Icon} name="undo" size="sm" />Undo</button>
        ${!t.version
          ? html`<button class="primary" disabled=${!!busy} onClick=${() => act("Matching", "/recalculate")} title="Snap this trip's GPS to roads, paths and railways">
              <${Icon} name="map" size="sm" />Map match this trip</button>`
          : t.overrides.length
          ? html`<${ConfirmButton} disabled=${!!busy} confirmLabel="Discard your edits?" onConfirm=${() => act("Recalculating", "/recalculate")}>
              <${Icon} name="recalc" size="sm" />Recalculate<//>`
          : html`<button disabled=${!!busy} onClick=${() => act("Recalculating", "/recalculate")} title="Run the matcher again from the original GPS">
              <${Icon} name="recalc" size="sm" />Recalculate</button>`}
        <button disabled=${!!busy} onClick=${() => act(t.locked ? "Unlocking" : "Locking", "/lock", { locked: !t.locked })}
          title=${t.locked ? "Allow automatic matching and pushing again" : "Keep this trip exactly as it is in Dawarich"}>
          <${Icon} name=${t.locked ? "unlock" : "lock"} size="sm" />${t.locked ? "Unlock" : "Lock"}</button>
        ${t.push !== "original" && html`<${ConfirmButton} cls="danger" disabled=${!!busy} confirmLabel="Restore original GPS?"
            onConfirm=${() => act("Reverting", "/revert")}><${Icon} name="undo" size="sm" />Revert to original<//>`}
        ${t.version > 0 && t.push !== "pushed" && html`<button class="primary" disabled=${!!busy || !canPush} onClick=${() => act("Pushing", "/push")}
            title=${!t.closed ? "Wait until the trip has finished" : t.locked ? "Unlock the trip first" : "Replace this trip's points in Dawarich with the matched route"}>
            <${Icon} name="push" size="sm" />${t.push === "outdated" ? "Push update" : "Push to Dawarich"}</button>`}
      </div>
    </div>
    <div class=${`ed-body ${busy ? "busy" : ""}`} aria-busy=${!!busy}>
      <div class="ed-main">
        <div class="ed-map">
          <div class="map" ref=${mapEl}></div>
          <div class="map-tools"><div class="card">
            <label><input type="checkbox" checked=${show.raw} onChange=${(e) => setShow({ ...show, raw: e.target.checked })} />Original GPS fixes</label>
            <label class="opt"><input type="checkbox" checked=${show.dropped} onChange=${(e) => setShow({ ...show, dropped: e.target.checked })} />Ignored fixes (${d.dropped.length})</label>
            <div class="key opt" hidden=${!t.version}>
              <span><svg viewBox="0 0 26 8"><line x1="1" y1="4" x2="25" y2="4" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>on the map</span>
              <span><svg viewBox="0 0 26 8"><line x1="1" y1="4" x2="25" y2="4" stroke="currentColor" stroke-width="3" stroke-dasharray="6 4"/></svg>routed across a gap</span>
              <span><svg viewBox="0 0 26 8"><line x1="2" y1="4" x2="25" y2="4" stroke="currentColor" stroke-width="3" stroke-dasharray="1.5 5" stroke-linecap="round"/></svg>original GPS kept</span>
            </div>
            <button class="sm opt" onClick=${() => fitCoords(mapRef.current, d.raw.map((p) => [p.lat, p.lon]), 60)}><${Icon} name="fit" size="sm" />Fit trip</button>
          </div></div>
        </div>
        <div class="ed-bottom">
          ${splitting && html`<div class="callout info"><${Icon} name="split" size="sm" />Click the timeline or the speed chart where the new segment should start. Esc cancels.</div>`}
          <div class=${`timeline ${splitting ? "splitting" : ""}`} style="position:relative"
            onClick=${(e) => { if (!splitting) return; const r = e.currentTarget.getBoundingClientRect(); pick(T0 + ((e.clientX - r.left) / r.width) * totalT); }}
            onMouseMove=${(e) => { const r = e.currentTarget.getBoundingClientRect(); setCursor(T0 + ((e.clientX - r.left) / r.width) * totalT); }}
            onMouseLeave=${() => setCursor(null)}>
            ${d.segments.map((s) => html`<div class=${`tl-seg ${s.mode === "stay" ? "stay" : ""} ${sel === s.id ? "sel" : ""}`}
              style=${`${modeStyle(s.mode)};position:absolute;top:0;bottom:0;left:${((s.t0 - T0) / totalT) * 100}%;width:${((s.t1 - s.t0) / totalT) * 100}%`}
              title=${`${MODES[s.mode]?.label} · ${time(s.t0)}–${time(s.t1)} · ${km(s.km)}`}
              onClick=${(e) => { if (splitting) return; e.stopPropagation(); setSel(s.id); zoomTo(s); }}>
              ${(s.t1 - s.t0) / totalT > 0.06 && html`<${Icon} name=${s.mode} size="sm" />`}</div>`)}
          </div>
          <${SpeedChart} raw=${d.raw} segments=${d.segments} t0=${T0} t1=${T1} cursor=${cursor} onHover=${setCursor} onPick=${pick} splitting=${splitting} />
          <div class="tl-axis num"><span>${time(T0)}</span>${cursor != null ? html`<span class="muted">${time(cursor)}</span>` : ""}<span>${time(T1)}</span></div>
        </div>
      </div>
      <aside class="ed-side">
        ${me?.dry_run && html`<section><div class="callout info"><${Icon} name="shield" size="sm" />Dry-run mode: pushes are simulated and Dawarich is not changed.</div></section>`}
        ${t.error && html`<section><div class="callout danger"><${Icon} name="alert" size="sm" />${t.error}</div></section>`}
        <section>
          <div class="row"><h2 class="panel-title grow">Segments</h2>
            <button class="sm" disabled=${!!busy || !moving.length} onClick=${() => setSplitting(!splitting)}
              title="Cut a segment in two, then change one half's mode"><${Icon} name="split" size="sm" />${splitting ? "Cancel split" : "Split"}</button></div>
          ${d.segments.map((s) => s.mode === "stay" && !s.forced
            ? html`<div class=${`stay-card ${sel === s.id ? "sel" : ""}`} onClick=${() => { setSel(s.id); zoomTo(s); }}>
                <${Icon} name="stay" size="sm" /><span class="grow">Stopped ${duration(s.t1 - s.t0)} <span class="faint">${time(s.t0)}</span></span>
                <select aria-label="Change this stop's mode" value="stay" onClick=${(e) => e.stopPropagation()}
                  onChange=${(e) => act("Rematching", `/segments/${s.id}/mode`, { mode: e.target.value })}>
                  ${EDITABLE_MODES.map((m) => html`<option value=${m}>${MODES[m].label}</option>`)}</select></div>`
            : html`<div class=${`seg-card ${sel === s.id ? "sel" : ""}`} style=${modeStyle(s.mode)} tabindex="0"
                onClick=${() => { setSel(s.id); zoomTo(s); }} onKeyDown=${(e) => e.key === "Enter" && (setSel(s.id), zoomTo(s))}>
                <div class="seg-top"><${Icon} name=${s.mode} />
                  <select aria-label="Travel mode" value=${s.mode} onClick=${(e) => e.stopPropagation()}
                    onChange=${(e) => act("Rematching", `/segments/${s.id}/mode`, { mode: e.target.value })}>
                    ${EDITABLE_MODES.map((m) => html`<option value=${m}>${MODES[m].label}</option>`)}</select>
                  <span class="grow"></span>${s.forced && html`<span class="badge accent" title="You set this mode; recalculating clears it">Set by you</span>`}</div>
                <div class="stats num"><span>${time(s.t0)}–${time(s.t1)}</span><span>${duration(s.t1 - s.t0)}</span><span>${km(s.km)}</span>
                  ${s.t1 > s.t0 && s.km > 0 && html`<span>${Math.round((s.km * 3600) / (s.t1 - s.t0))} km/h</span>`}</div>
                ${s.mode !== "raw" && html`<${Provenance} prov=${s.provenance || {}} />`}
              </div>`)}
          ${!d.segments.length && html`<p class="muted">${t.version ? "Nothing to show." : html`Not map matched yet. The grey line and dots are your original GPS: ${t.n_points} points, ${km(t.km)}.`}</p>`}
        </section>
        ${t.version > 0 && html`<section>
          <h2 class="panel-title">Match quality</h2>
          ${t.state === "review" && html`<div class="callout warn"><${Icon} name="alert" size="sm" /><span>
            ${q.raw_frac > 0.15 ? `${pct(q.raw_frac)} of this trip could not be placed on the map and keeps the original GPS.` : `Some fixes sit far from the matched line (95th percentile ${Math.round(q.p95)} m).`}
            Check the segment modes; a wrong mode is the usual cause.</span></div>`}
          <div class="quality">
            <div class="kpi"><b class="num">${pct(q.matched_frac)}</b><span>on the map</span></div>
            <div class="kpi"><b class="num">${pct(q.filled_frac)}</b><span>routed gaps</span></div>
            <div class="kpi"><b class="num">${pct(q.raw_frac)}</b><span>original GPS</span></div>
          </div>
          <p class="hint num">Typical distance from your GPS to the line: ${Math.round(q.p50 || 0)} m (95% within ${Math.round(q.p95 || 0)} m).
            ${d.dropped.length ? ` ${d.dropped.length} fixes were ignored as duplicates, spikes or inaccurate; they stay in the archive.` : ""}</p>
        </section>
        <section>
          <h2 class="panel-title">Export</h2>
          <div class="row"><a class="btn sm" href=${apiUrl(`/trips/${id}/export.gpx`)} download><${Icon} name="download" size="sm" />GPX</a>
            <a class="btn sm" href=${apiUrl(`/trips/${id}/export.geojson`)} download><${Icon} name="download" size="sm" />GeoJSON</a></div>
        </section>`}
        <section>
          <h2 class="panel-title">History</h2>
          <div class="hist">${d.history.map((h) => html`<div key=${h.version}>
            <span class=${`num ${h.version === t.version ? "" : "faint"}`}>v${h.version}</span><span class="grow ellipsis" title=${h.reason}>${h.reason}</span>
            <span class="faint num" title=${full(h.created_at)}>${ago(h.created_at)}</span></div>`)}</div>
          ${t.pushed_at && html`<p class="hint">Pushed v${t.pushed_version} ${ago(t.pushed_at)}.</p>`}
        </section>
      </aside>
    </div>
  </div>`;
}
