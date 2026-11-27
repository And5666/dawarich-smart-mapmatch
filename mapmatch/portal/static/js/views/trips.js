// Trips: a day-by-day list beside a map of every trip, segments in their own colours.
import {
  html, useState, useEffect, useRef, useMemo, api, useStore, toast, Icon, Ribbon, Menu, Dialog, HowItWorks, MapOptions, MapSwitch, setMapPrefs, LINE_WIDTHS,
  MODES, modeVar, cssVar, colorExpr, km, duration, time, day, dayKey, plural, navigate, pct,
} from "../lib.js";
import { startJob } from "../session.js";
import { createMap, setMapTheme, setOverlay, setData, fc, fitCoords } from "../map.js";

const PERIODS = [["all", "All time", 0], ["7", "Last 7 days", 7], ["30", "Last 30 days", 30], ["90", "Last 90 days", 90], ["365", "Last 12 months", 365], ["custom", "Custom dates…", -1]];
const SHOW = [["all", "All trips"], ["new", "Not matched yet"], ["review", "Need a look"], ["ready", "Ready to push"],
  ["pushed", "In Dawarich"], ["outdated", "Changed since pushed"], ["recorded", "Kept as recorded"], ["nomap", "No map here"],
  ["failed", "Couldn't be matched"], ["locked", "Locked"]];
// Matched, finished, not locked, and Dawarich doesn't have this version yet.
const ready = (t) => t.version && t.closed && !t.locked && (t.push === "original" || t.push === "outdated");
const SORTS = [["newest", "Newest first"], ["oldest", "Oldest first"], ["longest", "Longest distance"], ["duration", "Longest time"],
  ["worst", "Needs a look most"], ["apart", "Furthest from the GPS"], ["points", "Most GPS points"]];
// How far a match is from good: unmatched GPS and points far from the line.
// The share of a trip that is a guess: straight lines across holes (older matches only know "GPS kept").
const doubt = (q) => (q?.unsure_frac != null ? q.unsure_frac : q?.raw_frac || 0);
const badness = (t) => (t.version ? doubt(t.quality) + (t.quality?.p95 || 0) / 100 : -1);
// The area between the GPS joined up and the drawn line, per metre of line: how far apart the two run.
const apart = (t) => (t.version && t.quality?.gap_m != null ? t.quality.gap_m : -1);
const SORT_BY = {
  newest: (x, y) => y.start - x.start, oldest: (x, y) => x.start - y.start, longest: (x, y) => y.km - x.km,
  duration: (x, y) => (y.end - y.start) - (x.end - x.start), worst: (x, y) => badness(y) - badness(x),
  points: (x, y) => y.n_points - x.n_points, apart: (x, y) => apart(y) - apart(x),
};
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

// Why a trip is flagged, in a few words, for its row in the list.
export function whyLook(t) {
  const q = t.quality || {};
  const out = [];
  if (doubt(q) > 0.15) out.push(q.unsure_frac != null ? `${pct(doubt(q))} straight across gaps` : `${pct(doubt(q))} left as GPS`);
  if ((q.detour_line_m || 0) - (q.detour_hop_m || 0) >= 300 && q.detour_line_m >= 2.5 * Math.max(q.detour_hop_m || 0, 30)) out.push("long way round");
  if ((q.p95 || 0) > 60) out.push("points far from the line");
  return out;
}

// Where most trips are. One holiday abroad must not shrink every other trip to a dot.
function usualCoords(trips) {
  const all = (list) => list.flatMap((t) => t.preview.flatMap((p) => p.c));
  const first = (t) => t.preview.find((p) => p.c.length)?.c[0];
  const starts = trips.map(first).filter(Boolean);
  if (starts.length < 8) return { coords: all(trips), left: 0 };
  const mid = (k) => starts.map((c) => c[k]).sort((x, y) => x - y)[Math.floor(starts.length / 2)];
  const la = mid(0), lo = mid(1);
  const near = trips.filter((t) => { const c = first(t); return c && Math.abs(c[0] - la) < 1.2 && Math.abs(c[1] - lo) < 1.8; });
  if (near.length < 0.6 * trips.length || near.length === trips.length) return { coords: all(trips), left: 0 };
  return { coords: all(near), left: trips.length - near.length };
}

// The shape of a trip, small, in its mode colours: easier to recognise than a time.
const thumbs = new Map();
function Thumb({ t }) {
  const key = `${t.id}:${t.version}:${t.n_points}`;
  let parts = thumbs.get(key);
  if (!parts) {
    const pts = t.preview.flatMap((p) => p.c);
    parts = [];
    if (pts.length > 1) {
      let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
      for (const [la, lo] of pts) { a = Math.min(a, lo); b = Math.min(b, la); c = Math.max(c, lo); d = Math.max(d, la); }
      const kx = Math.cos(((b + d) / 2) * Math.PI / 180), W = 40, H = 30, P = 3;
      const s = Math.min((W - 2 * P) / Math.max((c - a) * kx, 1e-6), (H - 2 * P) / Math.max(d - b, 1e-6));
      const ox = (W - (c - a) * kx * s) / 2, oy = (H - (d - b) * s) / 2;
      for (const p of t.preview) {
        if (p.c.length < 2) continue;
        const step = Math.max(1, Math.floor(p.c.length / 60));
        const pick = p.c.filter((_, i) => i % step === 0 || i === p.c.length - 1);
        parts.push([p.m, pick.map(([la, lo], i) => `${i ? "L" : "M"}${(ox + (lo - a) * kx * s).toFixed(1)} ${(H - oy - (la - b) * s).toFixed(1)}`).join("")]);
      }
    }
    if (thumbs.size > 4000) thumbs.clear();
    thumbs.set(key, parts);
  }
  return html`<svg class="thumb" viewBox="0 0 40 30" aria-hidden="true">${parts.map(([m, dpath]) => html`<path d=${dpath} style=${modeVar(m)} />`)}</svg>`;
}

function tripFeatures(t, label) {
  return t.preview.filter((p) => p.c.length > 1).map((p) => line(p.c, { tid: t.id, m: p.m, label }));
}

export function Trips() {
  const theme = useStore((s) => s.theme);
  const version = useStore((s) => s.dataVersion);
  const job = useStore((s) => s.job);
  const me = useStore((s) => s.me);
  const [ask, setAsk] = useState(null);            // trips waiting for a yes before they are pushed
  const [left, setLeft] = useState(0);             // trips outside the opening view of the map
  const [intro, setIntro] = useState(() => pref("mm-intro", "show") === "show");
  const [period, setPeriod] = useState(() => pref("mm-period", "all"));
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [show, setShow] = useState("all");
  const [onlyMode, setOnlyMode] = useState("");
  const [sort, setSort] = useState(() => pref("mm-sort", "newest"));
  const byDay = sort === "newest" || sort === "oldest";
  const [data, setTrips] = useState(null);
  const [hot, setHot] = useState(null);
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState(() => new Set());
  const prefs = useStore((s) => s.mapPrefs);
  const allPts = prefs.points;
  const look = useRef("");
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
    (show === "all" || t.state === show || t.push === show || (show === "nomap" && t.no_map) || (show === "ready" && ready(t))) && (!onlyMode || t.modes.some((m) => m.mode === onlyMode)))
    .sort(SORT_BY[sort] || SORT_BY.newest), [data, show, onlyMode, sort]);
  const unmatched = useMemo(() => (data?.trips || []).filter((t) => !t.version && !t.locked), [data]);
  const pushing = useMemo(() => (data?.trips || []).filter(ready), [data]);
  // Trips that matching changed (the rest are kept as recorded and never pushed), and those of them Dawarich has.
  const corrected = useMemo(() => (data?.trips || []).filter((t) => t.version && t.push !== "recorded"), [data]);
  const inDawarich = useMemo(() => corrected.filter((t) => t.push === "pushed" || t.push === "outdated"), [corrected]);
  const [undoAll, setUndoAll] = useState(false);
  const reviewing = useMemo(() => (data?.trips || []).filter((t) => t.state === "review").sort((x, y) => y.start - x.start), [data]);
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
  useEffect(() => setMapTheme(mapRef.current, theme, prefs.style), [theme, prefs.style]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const feats = trips.flatMap((t) => {
      const shown = (drawing.has(t.id) && before.current.get(t.id)) || t;
      const what = shown.modes.length ? shown.modes.map((m) => `${MODES[m.mode]?.label} ${km(m.km)}`).join(" · ") : "Not matched yet";
      return tripFeatures(shown, `<b>${day(t.start)}, ${time(t.start)}–${time(t.end)}</b><br>${what}`);
    });
    const data = fc(feats);
    const W = LINE_WIDTHS[prefs.width] || 1;
    const vis = { visibility: prefs.routes ? "visible" : "none" };
    const key = `${prefs.width}|${prefs.routes}`;
    if (map._mm.overlays.has("trips") && look.current === key) setData(map, "trips", data);
    else {
      look.current = key;
      setOverlay(map, "trips", { data, layers: () => [
        { id: "trips-casing", type: "line", layout: { ...vis, "line-cap": "round", "line-join": "round" },
          paint: { "line-color": cssVar("--casing"), "line-width": ["case", ["==", ["get", "m"], "none"], 5 * W, 5.5 * W], "line-opacity": 0.9 } },
        { id: "trips-hot", type: "line", filter: ["==", ["get", "tid"], -1], layout: { ...vis, "line-cap": "round", "line-join": "round" },
          paint: { "line-color": cssVar("--ink"), "line-width": 9 * W, "line-opacity": 0.3 } },
        { id: "trips-raw", type: "line", filter: ["==", ["get", "m"], "none"], layout: { ...vis, "line-join": "round" },
          paint: { "line-color": cssVar("--m-none"), "line-width": 2.6 * W, "line-dasharray": [2.2, 1.4] } },
        { id: "trips-line", type: "line", filter: ["!=", ["get", "m"], "none"], layout: { ...vis, "line-cap": "round", "line-join": "round" },
          paint: { "line-color": colorExpr(), "line-width": 3 * W } },
      ] });
      // the recorded points stay on top of the routes
      const ap = map._mm.overlays.get("allpts");
      if (ap) setOverlay(map, "allpts", ap);
    }
    if (!fitted.current && feats.length && !anims.current.size) {
      const go = () => { const u = usualCoords(trips); fitCoords(map, u.coords, 40, false); setLeft(u.left); fitted.current = true; };
      map.loaded() ? go() : map.once("load", go);
    }
  }, [trips, drawing, prefs.width, prefs.routes]);

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
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 1.3, 11, 2.2, 14, 3.2, 17, 5], "circle-color": cssVar("--ink"), "circle-opacity": 0.8,
        "circle-stroke-color": cssVar("--panel"), "circle-stroke-width": ["interpolate", ["linear"], ["zoom"], 11, 0, 14, 0.8] } }] });
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
        // New trips (appearing while a sync downloads) and newly matched ones are drawn in.
        const fresh = x.trips.filter((t) => !anims.current.has(t.id) && t.preview.length &&
          t.closed && (!cur.has(t.id) || t.version > (cur.get(t.id).version || 0)) && (!a || t.end >= a) && (!b || t.start <= b));
        if (!dataRef.current) setTrips({ trips: [], counts: {}, km_by_mode: {} });
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
      setTrips((d) => {
        const base = d || { trips: [], counts: {}, km_by_mode: {} };
        const known = new Set(base.trips.map((t) => t.id));
        const added = done.filter((t) => !known.has(t.id));
        const merged = base.trips.map((t) => (by.has(t.id) ? { ...t, ...by.get(t.id) } : t)).concat(added);
        merged.sort((x, y) => y.start - x.start);
        return { ...base, trips: merged };
      });
      for (const t of done) before.current.delete(t.id);
      setDrawing(new Set(anims.current.keys()));
    }
    if (anims.current.size) requestAnimationFrame(frame);
    else lastStart.current = 0;
  }

  // ---------------------------------------------------------------- actions
  const match = (scope, params, label) => startJob("rematch", { scope, ...params }).then((ok) => ok && toast(label));
  const push = (ids, label) => startJob("push_all", { ids }).then((ok) => ok && toast(label));
  const review = () => {
    // The trip page steps through these in order, so a batch can be checked without coming back here.
    try { sessionStorage.setItem("mm-review", JSON.stringify(reviewing.map((t) => t.id))); } catch (e) { /* ignore */ }
    navigate(`#/trip/${reviewing[0].id}`);
  };
  const showAll = () => { fitCoords(mapRef.current, trips.flatMap((t) => t.preview.flatMap((p) => p.c)), 40); setLeft(0); };
  const togglePick = (id) => setPicked((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const groups = useMemo(() => {
    const g = [];
    if (!byDay) return trips.length ? [{ key: "all", ts: 0, trips, km: trips.reduce((a, t) => a + t.km, 0) }] : [];
    for (const t of trips) {
      const k = dayKey(t.start);
      if (!g.length || g[g.length - 1].key !== k) g.push({ key: k, ts: t.start, trips: [], km: 0 });
      g[g.length - 1].trips.push(t);
      g[g.length - 1].km += t.km;
    }
    return g;
  }, [trips, byDay]);
  const matching = job && ["rematch", "auto", "sync", "import_csv"].includes(job.kind);
  const noData = data && data.trips.length === 0 && !job;
  const periodLabel = PERIODS.find((p) => p[0] === period)?.[1].toLowerCase() || "";

  return html`<div class=${`trips ${selecting ? "selecting" : ""}`}>
    <aside class="side">
      <div class="side-top">
        <div class="side-head">
          <div class="grow">
            <h1>Trips</h1>
            <div class="small dim num">${data ? `${plural(data.trips.length, "trip")} · ${km(totals.all)}` : "Loading…"}</div>
          </div>
          <select class="pill" aria-label="Period" value=${period} onChange=${(e) => { setPeriod(e.target.value); save("mm-period", e.target.value); fitted.current = false; }}>
            ${PERIODS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
        </div>
        ${period === "custom" && html`<div class="row">
          <input type="date" aria-label="From" value=${custom.from} onChange=${(e) => { setCustom({ ...custom, from: e.target.value }); fitted.current = false; }} />
          <span class="faint">to</span>
          <input type="date" aria-label="To" value=${custom.to} onChange=${(e) => { setCustom({ ...custom, to: e.target.value }); fitted.current = false; }} /></div>`}
        ${data && data.trips.length > 0 && html`<div class="todo" role="group" aria-label="What needs doing">
          ${reviewing.length > 0 && html`<div class=${`todo-row warn ${show === "review" ? "on" : ""}`}>
            <button class="todo-main" onClick=${() => setShow(show === "review" ? "all" : "review")} aria-pressed=${show === "review"}
              title="Show only these in the list">
              <b class="num">${reviewing.length}</b><span><strong>${reviewing.length === 1 ? "needs" : "need"} a look</strong><small>The line may be wrong somewhere</small></span></button>
            <button class="go" onClick=${review}>Review</button>
          </div>`}
          ${unmatched.length > 0 && html`<div class=${`todo-row ${show === "new" ? "on" : ""}`}>
            <button class="todo-main" onClick=${() => setShow(show === "new" ? "all" : "new")} aria-pressed=${show === "new"} title="Show only these in the list">
              <b class="num">${unmatched.length}</b><span><strong>not matched yet</strong><small>New trips, still as recorded</small></span></button>
            <button disabled=${!!job} onClick=${() => match("ids", { ids: unmatched.map((t) => t.id) }, `Matching ${plural(unmatched.length, "trip")}`)}>Match</button>
          </div>`}
        </div>`}
        ${data && corrected.length > 0 && html`<div class="dw" role="group" aria-label="Dawarich">
          <div class="dw-head"><b>In Dawarich</b>
            <button class="link" onClick=${() => setShow(show === "pushed" ? "all" : "pushed")} aria-pressed=${show === "pushed"}
              title="Show only the trips that are in Dawarich">${inDawarich.length} of ${plural(corrected.length, "corrected trip")}</button></div>
          <div class="dw-bar" aria-hidden="true"><i style=${`width:${(100 * inDawarich.length) / corrected.length}%`}></i></div>
          <div class="dw-actions">
            <button class=${pushing.length && !reviewing.length ? "go" : ""} disabled=${!pushing.length || !!job}
              title=${pushing.length ? "Replace these trips' points in Dawarich with the corrected routes" : "Every corrected trip is already in Dawarich"}
              onClick=${() => setAsk({ ids: pushing.map((t) => t.id), flagged: pushing.filter((t) => t.state === "review").length })}>
              <${Icon} name="push" small />${pushing.length ? `Push ${pushing.length === corrected.length ? "all" : ""} ${pushing.length}…` : "All pushed"}</button>
            <button disabled=${!inDawarich.length || !!job} onClick=${() => setUndoAll(true)}
              title=${inDawarich.length ? "Put the points you recorded back in Dawarich, for every pushed trip" : "Nothing has been pushed yet, so there is nothing to undo"}>
              <${Icon} name="undo" small />Undo all…</button>
          </div>
          <p class="dw-note">${me?.dry_run ? "Test mode: a push only shows what would change."
            : inDawarich.length ? "Undo puts your recorded points back. You can do it at any time." : "A push can be undone at any time: your recorded points are kept."}</p>
        </div>`}
        ${totals.by.length > 0 && html`<div class="modefilter" role="group" aria-label="Show trips with">
          <button class=${!onlyMode ? "on" : ""} aria-pressed=${!onlyMode} onClick=${() => setOnlyMode("")}>All</button>
          ${totals.by.map((m) => html`<button class=${onlyMode === m.mode ? "on" : ""} style=${modeVar(m.mode)} aria-pressed=${onlyMode === m.mode}
            title=${`Only trips with ${MODES[m.mode]?.label.toLowerCase()}`} onClick=${() => setOnlyMode(onlyMode === m.mode ? "" : m.mode)}>
            <span class="sw"></span>${MODES[m.mode]?.label}<span class="faint num">${km(m.km)}</span></button>`)}
        </div>`}
        <div class="row filters">
          <select aria-label="Show" value=${show} onChange=${(e) => setShow(e.target.value)} class="grow">
            ${SHOW.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
          <select aria-label="Sort" value=${sort} onChange=${(e) => { setSort(e.target.value); save("mm-sort", e.target.value); }} class="grow">
            ${SORTS.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>
          <${Menu} cls="icon" title="More" label=${html`<${Icon} name="more" />`}>
            <div class="label">Matching uses your saved Match settings</div>
            <button disabled=${!data?.trips.length || !!job} onClick=${() => match("range", { start: a, end: b }, `Matching every trip in ${periodLabel}`)}>
              <${Icon} name="sync" small />Match every trip in ${periodLabel} again</button>
            <button onClick=${() => { setSelecting(true); setPicked(new Set()); }}><${Icon} name="check" small />Choose trips to match or push…</button>
            <button onClick=${() => { setIntro(true); save("mm-intro", "show"); }}><${Icon} name="help" small />How this works</button>
            <div class="sep"></div>
            <a href="#/match"><${Icon} name="sliders" small />Match settings…</a>
          <//>
        </div>
      </div>
      <div class=${`list ${byDay ? "" : "flat"}`}>
        ${intro && data && data.trips.length > 0 && html`<div class="intro">
          <div class="row"><b class="grow">How this works</b><button class="sm bare" onClick=${() => { setIntro(false); save("mm-intro", "done"); }}>Got it</button></div>
          <${HowItWorks} dry=${me?.dry_run} /></div>`}
        ${!data && html`<p class="faint" style="padding:16px">Loading…</p>`}
        ${data && !trips.length && !noData && html`<p class="faint" style="padding:16px">Nothing here with these filters.</p>`}
        ${groups.map((g) => html`<div key=${g.key}>
          ${byDay ? html`<div class="day"><span>${day(g.ts)}</span><span class="num">${km(g.km)}</span></div>`
            : html`<div class="day"><span>${SORTS.find((x) => x[0] === sort)?.[1]}</span><span class="num">${km(g.km)}</span></div>`}
          ${g.trips.map((t) => html`<div key=${t.id} class=${`trip ${hot === t.id ? "hot" : ""} ${picked.has(t.id) ? "picked" : ""}`}
              role=${selecting ? "checkbox" : "link"} aria-checked=${selecting ? picked.has(t.id) : undefined} tabindex="0"
              onMouseEnter=${() => setHot(t.id)} onMouseLeave=${() => setHot(null)}
              onClick=${() => (selecting ? togglePick(t.id) : navigate(`#/trip/${t.id}`))}
              onKeyDown=${(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selecting ? togglePick(t.id) : navigate(`#/trip/${t.id}`); } }}>
            <span class="picker"><input type="checkbox" tabindex="-1" checked=${picked.has(t.id)} /></span>
            <div class="when num">${byDay ? time(t.start) : day(t.start)}<span>${byDay ? duration(t.end - t.start) : `${time(t.start)} · ${duration(t.end - t.start)}`}</span></div>
            <${Thumb} t=${t} />
            <div class="what"><${Ribbon} modes=${t.modes} />
              <div class="modes"><span class="ell">${t.modes.length ? t.modes.map((m) => MODES[m.mode]?.label).join(" → ")
                : html`<span class="faint">Not matched yet</span>`}</span>
                ${t.state === "review" && html`<span class="chip warn" title="Needs a look">${whyLook(t)[0] || "needs a look"}</span>`}
                ${t.state === "failed" && html`<span class="chip bad">couldn't match</span>`}
                ${t.push === "pushed" && html`<span class="chip good">in Dawarich</span>`}
                ${t.no_map && html`<span class="chip" title="The map server has no map data here">no map here</span>`}
                ${t.push === "recorded" && !t.no_map && html`<span class="chip">as recorded</span>`}
                ${t.push === "outdated" && html`<span class="chip warn">changed</span>`}</div></div>
            <div class="dist num" title=${sort === "apart" && apart(t) >= 0 ? "Average distance between your GPS and the drawn line" : undefined}>${sort === "apart" && apart(t) >= 0 ? `${Math.round(apart(t))} m apart` : km(t.km)}</div>
          </div>`)}
        </div>`)}
      </div>
      ${selecting && html`<div class="selbar">
        <span class="grow small">${picked.size ? plural(picked.size, "trip") + " selected" : "Tap trips to select them"}</span>
        <button class="go" disabled=${!picked.size} onClick=${() => { match("ids", { ids: [...picked] }, `Matching ${plural(picked.size, "trip")}`); setSelecting(false); setPicked(new Set()); }}>Match</button>
        <button disabled=${!picked.size} onClick=${() => { setAsk({ ids: [...picked], flagged: trips.filter((t) => picked.has(t.id) && t.state === "review").length }); setSelecting(false); setPicked(new Set()); }}>Push…</button>
        <button class="bare" onClick=${() => { setSelecting(false); setPicked(new Set()); }}>Cancel</button>
      </div>`}
    </aside>
    <div class="map-wrap">
      <div class="map" ref=${mapEl}></div>
      <div class="float tl">
        <${MapOptions}>
          <${MapSwitch} on=${prefs.routes} onChange=${(v) => setMapPrefs({ routes: v })} hint="The line drawn for each trip, in its travel colours">Matched routes<//>
          <${MapSwitch} on=${prefs.points} onChange=${(v) => setMapPrefs({ points: v })} hint="Every point you recorded in this period, exactly where it was logged">Original GPS points<//>
        <//>
        ${left > 0 && html`<button class="sm bare" onClick=${showAll} title="The map opened on where most of your trips are">Show ${plural(left, "trip")} further away</button>`}</div>
      ${undoAll && html`<${Dialog} title="Put your recorded points back in Dawarich?" danger confirmLabel=${me?.dry_run ? "Run the test" : `Undo ${plural(inDawarich.length, "push", "es")}`}
        onClose=${() => setUndoAll(false)} onConfirm=${() => startJob("revert_all").then((ok) => ok && toast(`Putting the original points back for ${plural(inDawarich.length, "trip")}`))}>
        <p>${plural(inDawarich.length, "trip")} in Dawarich ${inDawarich.length === 1 ? "goes" : "go"} back to the GPS you recorded.</p>
        <p>The corrected routes stay here in mapmatch, so you can push them again whenever you like.</p>
        ${me?.dry_run && html`<p><b>Test mode is on, so nothing in Dawarich will change.</b></p>`}<//>`}
      ${ask && html`<${Dialog} title=${`Push ${plural(ask.ids.length, "trip")} to Dawarich?`} confirmLabel=${me?.dry_run ? "Run the test" : `Push ${plural(ask.ids.length, "trip")}`}
        onClose=${() => setAsk(null)} onConfirm=${() => push(ask.ids, `Pushing ${plural(ask.ids.length, "trip")}`)}>
        ${me?.dry_run
          ? html`<p><b>Test mode is on, so nothing in Dawarich will change.</b> This works out what a real push would do and lists it under Activity.</p>`
          : html`<p>For each trip, the points recorded in Dawarich are replaced by the corrected route. Your recorded points are kept, and <b>Undo all</b> (or Undo push on a single trip) puts them back at any time.</p>`}
        ${ask.flagged > 0 && html`<p class="note warn">${plural(ask.flagged, "trip")} of these still ${ask.flagged === 1 ? "needs" : "need"} a look. Check ${ask.flagged === 1 ? "it" : "them"} first if you can.</p>`}<//>`}
      ${matching && html`<div class="float bc" aria-live="polite"><span class="pulse"></span>
        <span>${job.kind === "rematch" ? "Matching" : "Downloading"} <span class="dim num">${job.message || ""}</span></span><span class="meter"><i style=${`width:${job.progress}%`}></i></span></div>`}
      ${noData && html`<div class="empty"><div>
        <h2>No trips yet</h2>
        <p>Sync downloads your GPS points from Dawarich and splits them into trips. Nothing is matched or changed yet.</p>
        <div class="row"><button class="go" onClick=${() => startJob("sync")}><${Icon} name="sync" small />Sync now</button></div>
      </div></div>`}
    </div>
  </div>`;
}
