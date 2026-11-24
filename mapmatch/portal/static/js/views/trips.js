// Trips: a day-by-day list beside a map of every trip, segments in their own colours.
import {
  html, useState, useEffect, useRef, useMemo, api, useStore, toast, Icon, Ribbon, Menu,
  MODES, modeVar, cssVar, colorExpr, km, duration, time, day, dayKey, plural, navigate,
} from "../lib.js";
import { startJob } from "../session.js";
import { createMap, setMapTheme, setOverlay, setData, fc, fitCoords } from "../map.js";

const PERIODS = [["all", "All time", 0], ["7", "Last 7 days", 7], ["30", "Last 30 days", 30], ["90", "Last 90 days", 90], ["365", "Last 12 months", 365], ["custom", "Custom dates…", -1]];
const SHOW = [["all", "All trips"], ["new", "Not matched yet"], ["review", "Needs a look"], ["failed", "Couldn't be matched"],
  ["original", "Not pushed to Dawarich"], ["outdated", "Changed since pushed"]];
const DRAW_MS = matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 1200;
const pref = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };

function bounds(period, custom) {
  if (period === "custom") {
    const a = custom.from ? Date.parse(custom.from) / 1000 : null;
    const b = custom.to ? Date.parse(custom.to) / 1000 + 86399 : null;
    return [a, b];
  }
  const p = PERIODS.find((x) => x[0] === period);
  return p && p[2] > 0 ? [Math.floor(Date.now() / 1000) - p[2] * 86400, null] : [null, null];
}

const line = (coords, props) => ({ type: "Feature", properties: props, geometry: { type: "LineString", coordinates: coords.map((c) => [c[1], c[0]]) } });

function tripFeatures(t, label) {
  return t.preview.filter((p) => p.c.length > 1).map((p) => line(p.c, { tid: t.id, m: p.m, label }));
}

export function Trips() {
  const theme = useStore((s) => s.theme);
  const version = useStore((s) => s.dataVersion);
  const job = useStore((s) => s.job);
  const [period, setPeriod] = useState(() => pref("mm-period", "all"));
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [show, setShow] = useState("all");
  const [onlyMode, setOnlyMode] = useState("");
  const [data, setTrips] = useState(null);
  const [hot, setHot] = useState(null);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState(() => new Set());
  const [allPts, setAllPts] = useState(false);
  const [drawing, setDrawing] = useState(() => new Set());
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const fitted = useRef(false);
  const anims = useRef(new Map());
  const before = useRef(new Map());
  const lastStart = useRef(0);
  const dataRef = useRef(null);
  dataRef.current = data;

  const [a, b] = bounds(period, custom);
  useEffect(() => {
    const q = new URLSearchParams();
    if (a) q.set("start", a);
    if (b) q.set("end", b);
    api(`/trips?${q}`).then(setTrips).catch((e) => toast(e.message, "bad"));
  }, [a, b, version]);

  const trips = useMemo(() => (data?.trips || []).filter((t) =>
    (show === "all" || t.state === show || t.push === show) && (!onlyMode || t.modes.some((m) => m.mode === onlyMode))), [data, show, onlyMode]);
  const unmatched = useMemo(() => (data?.trips || []).filter((t) => !t.version && !t.locked), [data]);
  const totals = useMemo(() => {
    const by = {};
    let all = 0;
    for (const t of data?.trips || []) {
      if (!t.modes.length) { all += t.km; continue; }
      for (const m of t.modes) { by[m.mode] = (by[m.mode] || 0) + m.km; all += m.km; }
    }
    return { by: Object.entries(by).sort((x, y) => y[1] - x[1]).map(([mode, v]) => ({ mode, km: v })), all };
  }, [data]);

  // ---------------------------------------------------------------- map
  useEffect(() => {
    const map = createMap(mapEl.current, theme);
    mapRef.current = map;
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });
    for (const lid of ["trips-line", "trips-raw"]) {
      map.on("mousemove", lid, (e) => {
        const f = e.features[0];
        map.getCanvas().style.cursor = "pointer";
        setHot(f.properties.tid);
        pop.setLngLat(e.lngLat).setHTML(f.properties.label).addTo(map);
      });
      map.on("mouseleave", lid, () => { map.getCanvas().style.cursor = ""; setHot(null); pop.remove(); });
      map.on("click", lid, (e) => navigate(`#/trip/${e.features[0].properties.tid}`));
    }
    return () => map.remove();
  }, []);
  useEffect(() => setMapTheme(mapRef.current, theme), [theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const feats = trips.flatMap((t) => {
      const shown = (drawing.has(t.id) && before.current.get(t.id)) || t;
      const what = shown.modes.length ? shown.modes.map((m) => `${MODES[m.mode]?.label} ${km(m.km)}`).join(" · ") : "Not matched yet";
      return tripFeatures(shown, `<b>${day(t.start)}, ${time(t.start)}–${time(t.end)}</b><br>${what}`);
    });
    const data = fc(feats);
    if (map._mm.overlays.has("trips")) setData(map, "trips", data);
    else setOverlay(map, "trips", { data, layers: () => [
      { id: "trips-casing", type: "line", filter: ["!=", ["get", "m"], "none"], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--casing"), "line-width": 5.5, "line-opacity": 0.85 } },
      { id: "trips-hot", type: "line", filter: ["==", ["get", "tid"], -1], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": cssVar("--ink"), "line-width": 9, "line-opacity": 0.3 } },
      { id: "trips-raw", type: "line", filter: ["==", ["get", "m"], "none"], layout: { "line-join": "round" },
        paint: { "line-color": cssVar("--m-none"), "line-width": 2.2, "line-dasharray": [2, 1.6] } },
      { id: "trips-line", type: "line", filter: ["!=", ["get", "m"], "none"], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": colorExpr(), "line-width": 3 } },
    ] });
    if (!fitted.current && feats.length) {
      const go = () => { fitCoords(map, trips.flatMap((t) => t.preview.flatMap((p) => p.c)), 40, false); fitted.current = true; };
      map.loaded() ? go() : map.once("load", go);
    }
  }, [trips, drawing]);

  useEffect(() => {
    const map = mapRef.current;
    if (map?.getLayer("trips-hot")) map.setFilter("trips-hot", ["==", ["get", "tid"], hot ?? -1]);
  }, [hot, trips]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (!allPts) { if (map._mm.overlays.has("allpts")) setData(map, "allpts", fc([])); return; }
    const q = new URLSearchParams();
    if (a) q.set("start", a);
    if (b) q.set("end", b);
    api(`/points?${q}`).then((x) => {
      const d = fc(x.points.map(([lat, lon]) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lon, lat] } })));
      if (map._mm.overlays.has("allpts")) setData(map, "allpts", d);
      else setOverlay(map, "allpts", { data: d, layers: () => [{ id: "allpts", type: "circle", paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 1, 13, 2.2, 17, 4], "circle-color": cssVar("--ink-2"), "circle-opacity": 0.55 } }] });
      if (x.total > x.points.length) toast(`Showing ${x.points.length.toLocaleString()} of ${x.total.toLocaleString()} points`);
    }).catch((e) => toast(e.message, "bad"));
  }, [allPts, a, b, version]);

  // ---------------------------------------------------------------- live drawing while matching
  useEffect(() => {
    if (!job) return;
    let since = job.created_at;
    let live = true;
    const poll = async () => {
      try {
        const x = await api(`/trips/changes?since=${encodeURIComponent(since)}`);
        since = x.now;
        const cur = new Map((dataRef.current?.trips || []).map((t) => [t.id, t]));
        const fresh = x.trips.filter((t) => cur.has(t.id) && t.version > (cur.get(t.id).version || 0) && !anims.current.has(t.id) && t.preview.length);
        if (fresh.length) enqueue(fresh, cur);
      } catch (e) { /* the job poller reports errors */ }
    };
    const timer = setInterval(() => { if (live) poll(); }, 900);
    poll();
    return () => { live = false; clearInterval(timer); poll(); };
  }, [job?.id]);

  function enqueue(fresh, cur) {
    const now = performance.now();
    const step = Math.max(8, Math.min(90, 4000 / fresh.length));
    const wasIdle = anims.current.size === 0;
    for (const t of fresh) {
      before.current.set(t.id, cur.get(t.id));
      lastStart.current = Math.max(now, lastStart.current + step);
      const n = t.preview.reduce((s, p) => s + p.c.length, 0);
      anims.current.set(t.id, { trip: t, n, start: lastStart.current });
    }
    setDrawing(new Set(anims.current.keys()));
    if (wasIdle) requestAnimationFrame(frame);
  }

  function frame(ts) {
    const map = mapRef.current;
    if (!map) return;
    const feats = [], done = [];
    for (const [id, a] of anims.current) {
      const p = (ts - a.start) / DRAW_MS;
      if (p <= 0) continue;
      if (p >= 1) { done.push(a.trip); continue; }
      let f = (1 - (1 - p) ** 2) * (a.n - 1);
      for (const part of a.trip.preview) {
        const L = part.c.length;
        if (f >= L - 1) { feats.push(line(part.c, { m: part.m })); f -= L - 1; continue; }
        const k = Math.floor(f), c0 = part.c[k], c1 = part.c[Math.min(k + 1, L - 1)], r = f - k;
        const tip = [c0[0] + (c1[0] - c0[0]) * r, c0[1] + (c1[1] - c0[1]) * r];
        if (k > 0 || r > 0) feats.push(line([...part.c.slice(0, k + 1), tip], { m: part.m }));
        feats.push({ type: "Feature", properties: { m: part.m }, geometry: { type: "Point", coordinates: [tip[1], tip[0]] } });
        break;
      }
    }
    for (const t of done) anims.current.delete(t.id);
    const data = fc(feats);
    if (map._mm.overlays.has("live")) setData(map, "live", data);
    else setOverlay(map, "live", { data, layers: () => [
      { id: "live-line", type: "line", filter: ["==", ["geometry-type"], "LineString"], layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": colorExpr(), "line-width": 3.5 } },
      { id: "live-tip", type: "circle", filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": 5, "circle-color": colorExpr(), "circle-stroke-color": cssVar("--casing"), "circle-stroke-width": 2 } },
    ] });
    if (done.length) {
      const by = new Map(done.map((t) => [t.id, t]));
      setTrips((d) => d && { ...d, trips: d.trips.map((t) => (by.has(t.id) ? { ...t, ...by.get(t.id) } : t)) });
      for (const t of done) before.current.delete(t.id);
      setDrawing(new Set(anims.current.keys()));
    }
    if (anims.current.size) requestAnimationFrame(frame);
    else lastStart.current = 0;
  }

  // ---------------------------------------------------------------- actions
  const match = (scope, params, label) => startJob("rematch", { scope, ...params }).then((ok) => ok && toast(label));
  const togglePick = (id) => setPicked((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const groups = useMemo(() => {
    const g = [];
    for (const t of trips) {
      const k = dayKey(t.start);
      if (!g.length || g[g.length - 1].key !== k) g.push({ key: k, ts: t.start, trips: [], km: 0 });
      g[g.length - 1].trips.push(t);
      g[g.length - 1].km += t.km;
    }
    return g;
  }, [trips]);
  const matching = job && (job.kind === "rematch" || job.kind === "auto");
  const noData = data && data.trips.length === 0;
  const periodLabel = PERIODS.find((p) => p[0] === period)?.[1].toLowerCase() || "";

  return html`<div class=${`trips ${selecting ? "selecting" : ""}`}>
    <aside class="side">
      <div class="side-top">
        <div class="row">
          <select aria-label="Period" value=${period} onChange=${(e) => { setPeriod(e.target.value); save("mm-period", e.target.value); fitted.current = false; }}>
            ${PERIODS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
          <select aria-label="Show" value=${show} onChange=${(e) => setShow(e.target.value)} class="grow">
            ${SHOW.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
        </div>
        ${period === "custom" && html`<div class="row">
          <input type="date" aria-label="From" value=${custom.from} onChange=${(e) => { setCustom({ ...custom, from: e.target.value }); fitted.current = false; }} />
          <span class="faint">to</span>
          <input type="date" aria-label="To" value=${custom.to} onChange=${(e) => { setCustom({ ...custom, to: e.target.value }); fitted.current = false; }} /></div>`}
        ${data && html`<div class="summary"><b class="num">${plural(data.trips.length, "trip")}</b><span class="dim num">· ${km(totals.all)}</span></div>`}
        ${totals.by.length > 0 && html`<${Ribbon} modes=${totals.by} />
          <div class="legend">${totals.by.map((m) => html`<button class=${onlyMode && onlyMode !== m.mode ? "off" : ""} style=${modeVar(m.mode)}
            aria-pressed=${onlyMode === m.mode} title="Show only trips with this mode" onClick=${() => setOnlyMode(onlyMode === m.mode ? "" : m.mode)}>
            <span class="sw"></span>${MODES[m.mode]?.label} <span class="faint num">${km(m.km)}</span></button>`)}</div>`}
        <div class="tools">
          <${Menu} left cls=${unmatched.length ? "go" : ""} label=${html`Match<${Icon} name="down" small />`}>
            <button disabled=${!unmatched.length} onClick=${() => match("ids", { ids: unmatched.map((t) => t.id) }, `Matching ${plural(unmatched.length, "trip")}`)}>
              Unmatched trips <span class="faint num">${unmatched.length}</span></button>
            <button disabled=${!data?.trips.length} onClick=${() => match("range", { start: a, end: b }, `Matching every trip in ${periodLabel}`)}>
              Everything in ${periodLabel} <span class="faint num">${data?.trips.length || 0}</span></button>
            <div class="sep"></div>
            <button onClick=${() => setSelecting(true)}>Choose trips…</button>
          <//>
          <button class=${selecting ? "go" : ""} onClick=${() => { setSelecting(!selecting); setPicked(new Set()); }}>${selecting ? "Done" : "Select"}</button>
          <span class="grow"></span>
          <label class="check small"><input type="checkbox" checked=${allPts} onChange=${(e) => setAllPts(e.target.checked)} />GPS points</label>
        </div>
      </div>
      <div class="list">
        ${!data && html`<p class="faint" style="padding:16px">Loading…</p>`}
        ${data && !trips.length && !noData && html`<p class="faint" style="padding:16px">Nothing here with these filters.</p>`}
        ${groups.map((g) => html`<div key=${g.key}>
          <div class="day"><span>${day(g.ts)}</span><span class="num">${km(g.km)}</span></div>
          ${g.trips.map((t) => html`<div key=${t.id} class=${`trip ${hot === t.id ? "hot" : ""} ${picked.has(t.id) ? "picked" : ""}`}
              role=${selecting ? "checkbox" : "link"} aria-checked=${selecting ? picked.has(t.id) : undefined} tabindex="0"
              onMouseEnter=${() => setHot(t.id)} onMouseLeave=${() => setHot(null)}
              onClick=${() => (selecting ? togglePick(t.id) : navigate(`#/trip/${t.id}`))}
              onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selecting ? togglePick(t.id) : navigate(`#/trip/${t.id}`); } }}>
            <span class="picker"><input type="checkbox" tabindex="-1" checked=${picked.has(t.id)} /></span>
            <div class="when num">${time(t.start)}<span>${duration(t.end - t.start)}</span></div>
            <div class="what"><${Ribbon} modes=${t.modes} />
              <div class="modes ell">${t.modes.length ? t.modes.map((m) => MODES[m.mode]?.label).join(" → ")
                : html`<span class="faint">Not matched yet</span>`}
                ${t.state === "review" ? html` · <span class="flag">needs a look</span>` : ""}
                ${t.state === "failed" ? html` · <span class="flag bad">couldn't match</span>` : ""}
                ${t.push === "pushed" ? html` · <span class="faint">in Dawarich</span>` : ""}
                ${t.push === "outdated" ? html` · <span class="flag">changed since push</span>` : ""}</div></div>
            <div class="dist num">${km(t.km)}</div>
          </div>`)}
        </div>`)}
      </div>
      ${selecting && html`<div class="selbar">
        <span class="grow small">${picked.size ? plural(picked.size, "trip") + " selected" : "Tap trips to select them"}</span>
        <button class="go" disabled=${!picked.size} onClick=${() => { match("ids", { ids: [...picked] }, `Matching ${plural(picked.size, "trip")}`); setSelecting(false); setPicked(new Set()); }}>Match</button>
        <button disabled=${!picked.size} onClick=${() => { startJob("push_all", { ids: [...picked] }); setSelecting(false); setPicked(new Set()); }}>Push</button>
      </div>`}
    </aside>
    <div class="map-wrap">
      <div class="map" ref=${mapEl}></div>
      ${matching && html`<div class="float bc" aria-live="polite"><span class="pulse"></span>
        <span>Matching <span class="dim num">${job.message || ""}</span></span><span class="meter"><i style=${`width:${job.progress}%`}></i></span></div>`}
      ${noData && html`<div class="empty"><div>
        <h2>No trips yet</h2>
        <p>Sync downloads your GPS points from Dawarich and splits them into trips. Nothing is matched or changed yet.</p>
        <div class="row"><button class="go" onClick=${() => startJob("sync")}><${Icon} name="sync" small />Sync now</button></div>
      </div></div>`}
    </div>
  </div>`;
}
