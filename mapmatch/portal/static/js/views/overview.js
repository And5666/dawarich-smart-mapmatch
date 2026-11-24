// Trips overview: filters and stats, a day-grouped list, and a map of every trip.
import {
  html, useState, useEffect, useRef, useMemo, api, useStore, toast, Icon, ModeChip, StateBadge, PushBadge,
  MODES, modeColor, modeStyle, dominantMode, km, duration, time, day, dayKey, navigate,
} from "../lib.js";
import { startJob } from "../session.js";
import { createMap, setMapTheme, setOverlay, setData, fc, lineFeature, fitCoords } from "../map.js";

const RANGES = [["7d", "7 days", 7], ["30d", "30 days", 30], ["90d", "90 days", 90], ["1y", "Year", 365], ["all", "All", 0]];
const FILTERS = [
  ["all", "All"], ["new", "Not matched"], ["review", "Needs review"], ["failed", "Failed"], ["original", "Not pushed"],
  ["outdated", "Edited since push"],
];
// How long one trip takes to draw (instant for people who prefer reduced motion).
const DRAW_MS = matchMedia("(prefers-reduced-motion: reduce)").matches ? 1 : 1200;
const pref = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
const save = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };

function rangeBounds(range, custom) {
  if (range === "custom") {
    const a = custom.from ? Date.parse(custom.from) / 1000 : 0;
    const b = custom.to ? Date.parse(custom.to) / 1000 + 86399 : 0;
    return [a || null, b || null];
  }
  const r = RANGES.find((x) => x[0] === range);
  if (!r || !r[2]) return [null, null];
  return [Math.floor(Date.now() / 1000) - r[2] * 86400, null];
}

export function Overview() {
  const me = useStore((s) => s.me);
  const theme = useStore((s) => s.theme);
  const version = useStore((s) => s.dataVersion);
  const [range, setRange] = useState(() => pref("mm-range", "all"));
  const [custom, setCustom] = useState({ from: "", to: "" });
  const [filter, setFilter] = useState("all");
  const [mode, setMode] = useState("");
  const [data, setDataState] = useState(null);
  const [hot, setHot] = useState(null);
  const [allPts, setAllPts] = useState(false);
  const job = useStore((s) => s.job);
  // Live drawing: trips whose new match is queued or being drawn keep showing
  // their previous (raw) line underneath until the drawing finishes.
  const [animating, setAnimating] = useState(() => new Set());
  const anims = useRef(new Map());   // id -> { coords, mode, start }
  const prevShown = useRef(new Map()); // id -> trip as it was before the new match
  const lastStart = useRef(0);
  const mapEl = useRef(null);
  const mapRef = useRef(null);
  const fitted = useRef(false);

  useEffect(() => {
    const [a, b] = rangeBounds(range, custom);
    const q = new URLSearchParams();
    if (a) q.set("start", a);
    if (b) q.set("end", b);
    api(`/trips?${q}`).then(setDataState).catch((e) => toast(e.message, "error"));
  }, [range, custom.from, custom.to, version]);

  const trips = useMemo(() => {
    if (!data) return [];
    return data.trips.filter((t) => (filter === "all" || t.state === filter || t.push === filter) &&
      (!mode || t.modes.some((m) => m.mode === mode)));
  }, [data, filter, mode]);

  // ---- map
  useEffect(() => {
    const map = createMap(mapEl.current, theme);
    mapRef.current = map;
    const pop = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
    for (const lid of ["trips-line", "trips-raw"]) {
      map.on("mousemove", lid, (e) => {
        const f = e.features[0];
        map.getCanvas().style.cursor = "pointer";
        setHot(f.properties.id);
        pop.setLngLat(e.lngLat).setHTML(`<b>${f.properties.label}</b><br>${f.properties.sub}`).addTo(map);
      });
      map.on("mouseleave", lid, () => { map.getCanvas().style.cursor = ""; setHot(null); pop.remove(); });
      map.on("click", lid, (e) => navigate(`#/trip/${e.features[0].properties.id}`));
    }
    return () => map.remove();
  }, []);
  useEffect(() => setMapTheme(mapRef.current, theme), [theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const shown = trips.map((t) => (animating.has(t.id) && prevShown.current.get(t.id)) || t);
    const feats = shown.filter((t) => t.preview.length > 1).map((t) => lineFeature(t.preview, {
      id: t.id, mode: dominantMode(t.modes),
      label: `${day(t.start)} · ${time(t.start)}–${time(t.end)}`,
      sub: `${km(t.km)} · ${t.modes.map((m) => MODES[m.mode]?.label).join(" → ") || "raw GPS, not matched yet"}`,
    }));
    const overlay = {
      data: fc(feats), promoteId: "id",
      layers: () => [
        { id: "trips-casing", type: "line", layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": getComputedStyle(document.documentElement).getPropertyValue("--casing").trim(), "line-width": 5, "line-opacity": 0.9 } },
        { id: "trips-raw", type: "line", filter: ["==", ["get", "mode"], "none"], layout: { "line-join": "round" },
          paint: {
            "line-color": getComputedStyle(document.documentElement).getPropertyValue("--muted").trim(),
            "line-width": ["case", ["boolean", ["feature-state", "hot"], false], 4.5, 2.2],
            "line-dasharray": [2, 1.5],
          } },
        { id: "trips-line", type: "line", filter: ["!=", ["get", "mode"], "none"], layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": ["match", ["get", "mode"], ...Object.keys(MODES).flatMap((m) => [m, modeColor(m)]), modeColor("raw")],
            "line-width": ["case", ["boolean", ["feature-state", "hot"], false], 6, 3],
            "line-opacity": ["case", ["boolean", ["feature-state", "hot"], false], 1, 0.82],
          } },
      ],
    };
    if (map._mm.overlays.has("trips")) setData(map, "trips", overlay.data);
    else setOverlay(map, "trips", overlay);
    if (!fitted.current && feats.length) {
      const go = () => { fitCoords(map, trips.flatMap((t) => t.preview), 40, false); fitted.current = true; };
      map.loaded() ? go() : map.once("load", go);
    }
  }, [trips, animating]);

  // ---- live matching: poll for newly matched trips while a job runs, draw each one
  const dataRef = useRef(null);
  dataRef.current = data;
  useEffect(() => {
    if (!job) return;
    let since = job.created_at;
    let stopped = false;
    const poll = async () => {
      try {
        const x = await api(`/trips/changes?since=${encodeURIComponent(since)}`);
        since = x.now;
        const cur = new Map((dataRef.current?.trips || []).map((t) => [t.id, t]));
        const fresh = x.trips.filter((t) => cur.has(t.id) && t.version > (cur.get(t.id).version || 0) &&
          !anims.current.has(t.id) && t.preview.length > 1);
        if (fresh.length) enqueue(fresh, cur);
      } catch (e) { /* the job poller reports errors */ }
    };
    const timer = setInterval(() => { if (!stopped) poll(); }, 900);
    poll();
    return () => { stopped = true; clearInterval(timer); poll(); };
  }, [job?.id]);

  function enqueue(fresh, cur) {
    const map = mapRef.current;
    if (!map) return;
    const now = performance.now();
    // Many at once: stagger them so the map fills in as a wave, within ~4 s.
    const step = Math.max(8, Math.min(90, 4000 / fresh.length));
    for (const t of fresh) {
      prevShown.current.set(t.id, cur.get(t.id));
      lastStart.current = Math.max(now, lastStart.current + step);
      anims.current.set(t.id, { trip: t, coords: t.preview, mode: dominantMode(t.modes), start: lastStart.current });
    }
    setAnimating(new Set(anims.current.keys()));
    if (anims.current.size === fresh.length) requestAnimationFrame(frame);
  }

  function frame(ts) {
    const map = mapRef.current;
    if (!map) return;
    const lines = [], heads = [], finished = [];
    for (const [id, a] of anims.current) {
      const p = (ts - a.start) / DRAW_MS;
      if (p <= 0) continue;
      if (p >= 1) { finished.push(a.trip); continue; }
      // Ease out, then cut the line at that fraction, interpolating the tip.
      const e = 1 - (1 - p) ** 2;
      const f = e * (a.coords.length - 1);
      const k = Math.floor(f);
      const c0 = a.coords[k], c1 = a.coords[Math.min(k + 1, a.coords.length - 1)];
      const tip = [c0[0] + (c1[0] - c0[0]) * (f - k), c0[1] + (c1[1] - c0[1]) * (f - k)];
      const part = [...a.coords.slice(0, k + 1), tip];
      lines.push(lineFeature(part, { mode: a.mode }));
      heads.push({ type: "Feature", properties: { mode: a.mode }, geometry: { type: "Point", coordinates: [tip[1], tip[0]] } });
    }
    for (const t of finished) anims.current.delete(t.id);
    const live = { type: "FeatureCollection", features: [...lines, ...heads] };
    if (map._mm.overlays.has("live")) setData(map, "live", live);
    else setOverlay(map, "live", { data: live, layers: () => {
      const color = ["match", ["get", "mode"], ...Object.keys(MODES).flatMap((m) => [m, modeColor(m)]), modeColor("raw")];
      return [
        { id: "live-line", type: "line", filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": color, "line-width": 3.5 } },
        { id: "live-head", type: "circle", filter: ["==", ["geometry-type"], "Point"],
          paint: { "circle-radius": 5, "circle-color": color, "circle-stroke-color": "#fff", "circle-stroke-width": 2 } },
      ];
    } });
    if (finished.length) {
      const byId = new Map(finished.map((t) => [t.id, t]));
      setDataState((d) => d && { ...d, trips: d.trips.map((t) => byId.get(t.id) ? { ...t, ...byId.get(t.id) } : t) });
      for (const t of finished) prevShown.current.delete(t.id);
      setAnimating(new Set(anims.current.keys()));
    }
    if (anims.current.size) requestAnimationFrame(frame);
    else lastStart.current = 0;
  }

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (!allPts) { if (map._mm.overlays.has("allpts")) setData(map, "allpts", fc([])); return; }
    const [a, b] = rangeBounds(range, custom);
    const q = new URLSearchParams();
    if (a) q.set("start", a);
    if (b) q.set("end", b);
    api(`/points?${q}`).then((x) => {
      const data = { type: "FeatureCollection", features: x.points.map(([lat, lon]) => ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lon, lat] } })) };
      const ov = { data, layers: () => [{ id: "allpts", type: "circle", paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 1, 12, 2, 17, 4],
        "circle-color": getComputedStyle(document.documentElement).getPropertyValue("--m-raw").trim(),
        "circle-opacity": 0.6 } }] };
      if (map._mm.overlays.has("allpts")) setData(map, "allpts", data); else setOverlay(map, "allpts", ov);
      if (x.total > x.points.length) toast(`Showing ${x.points.length.toLocaleString()} of ${x.total.toLocaleString()} points`);
    }).catch((e) => toast(e.message, "error"));
  }, [allPts, range, custom.from, custom.to, version]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !map.getSource("trips")) return;
    map.removeFeatureState({ source: "trips" });
    if (hot != null) map.setFeatureState({ source: "trips", id: hot }, { hot: true });
  }, [hot, trips]);

  // ---- stats
  const totals = useMemo(() => {
    const byMode = {};
    let total = 0;
    // Matched trips count their matched distance, unmatched ones their raw GPS distance.
    for (const t of trips) {
      if (!t.modes.length) { total += t.km; continue; }
      for (const m of t.modes) { byMode[m.mode] = (byMode[m.mode] || 0) + m.km; total += m.km; }
    }
    return { byMode, total };
  }, [trips]);
  const counts = data?.counts || {};
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

  const st = me?.settings || {};
  const noTrips = data && data.trips.length === 0;

  return html`<div class="overview">
    <aside class="side">
      <div class="side-head">
        <div class="ranges seg-ctl" role="group" aria-label="Date range">
          ${RANGES.map(([k, label]) => html`<button class=${range === k ? "on" : ""} onClick=${() => { setRange(k); save("mm-range", k); fitted.current = false; }}>${label}</button>`)}
          <button class=${range === "custom" ? "on" : ""} onClick=${() => setRange("custom")}>Custom</button>
        </div>
        ${range === "custom" && html`<div class="custom-range">
          <label class="field"><span>From</span><input type="date" value=${custom.from} onChange=${(e) => { setCustom({ ...custom, from: e.target.value }); fitted.current = false; }} /></label>
          <label class="field"><span>To</span><input type="date" value=${custom.to} onChange=${(e) => { setCustom({ ...custom, to: e.target.value }); fitted.current = false; }} /></label></div>`}
        <div class="kpis">
          <div class="kpi"><b class="num">${trips.length}</b><span>trips</span></div>
          <div class="kpi"><b class="num">${km(totals.total)}</b><span>travelled</span></div>
          <div class="kpi"><b class="num">${(counts.review || 0) + (counts.failed || 0)}</b><span>to review</span></div>
        </div>
        ${Object.keys(totals.byMode).length > 0 && html`<div class="stack" style="gap:6px">
          <div class="modebar">${Object.entries(totals.byMode).sort((a, b) => b[1] - a[1]).map(([m, v]) =>
            html`<i style=${`${modeStyle(m)};width:${(100 * v) / Object.values(totals.byMode).reduce((a, b) => a + b, 0)}%`} title=${`${MODES[m]?.label}: ${km(v)}`}></i>`)}</div>
          <div class="legend">${Object.entries(totals.byMode).sort((a, b) => b[1] - a[1]).map(([m, v]) =>
            html`<button class=${`ghost sm ${mode === m ? "on" : ""}`} style=${`${modeStyle(m)};padding:1px 4px`} aria-pressed=${mode === m}
              onClick=${() => setMode(mode === m ? "" : m)} title="Show only trips with this mode">
              <span class="dot"></span>${MODES[m]?.label} <span class="num faint">${km(v)}</span></button>`)}</div></div>`}
        ${counts.new > 0 && html`<div class="callout info" style="align-items:center">
          <span class="grow">${counts.new} ${counts.new === 1 ? "trip is" : "trips are"} still raw GPS, not map matched.</span>
          <button class="sm primary" onClick=${() => startJob("rematch", { scope: "pending" })}>Map match</button></div>`}
        <div class="filters" role="group" aria-label="Filter trips">
          ${FILTERS.map(([k, label]) => {
            const n = k === "all" ? data?.trips.length : (counts[k] || counts[`push_${k}`] || 0);
            return html`<button class=${filter === k ? "on" : ""} aria-pressed=${filter === k} onClick=${() => setFilter(k)}>${label}${n ? html` <span class="num">${n}</span>` : ""}</button>`;
          })}
        </div>
      </div>
      <div class="list">
        ${!data && html`<p class="muted" style="padding:14px">Loading trips…</p>`}
        ${data && !trips.length && !noTrips && html`<p class="muted" style="padding:14px">No trips match these filters.</p>`}
        ${groups.map((g) => html`<div key=${g.key}>
          <div class="day"><span>${day(g.ts)}</span><span class="num">${km(g.km)}</span></div>
          ${g.trips.map((t) => html`<div key=${t.id} class=${`trip-row ${hot === t.id ? "hot" : ""}`} role="link" tabindex="0"
              onMouseEnter=${() => setHot(t.id)} onMouseLeave=${() => setHot(null)}
              onClick=${() => navigate(`#/trip/${t.id}`)} onKeyDown=${(e) => e.key === "Enter" && navigate(`#/trip/${t.id}`)}>
            <div class="top"><span class="time num">${time(t.start)}–${time(t.end)}</span>
              <span class="muted small num">${duration(t.end - t.start)} · ${km(t.km)}</span><span class="grow"></span>
              ${t.state !== "matched" && html`<${StateBadge} state=${t.state} />`}</div>
            <div class="row wrap" style="gap:6px"><div class="chips">${t.modes.length ? t.modes.map((m) => html`<${ModeChip} mode=${m.mode} km=${m.km} />`)
              : html`<span class="faint small">${t.error || `Raw GPS · ${km(t.km)} · ${t.n_points} points`}</span>`}</div>
              <span class="grow"></span>${t.push !== "original" && html`<${PushBadge} push=${t.push} />`}</div>
          </div>`)}
        </div>`)}
      </div>
    </aside>
    <div class="map-wrap">
      <div class="map" ref=${mapEl}></div>
      ${job && (job.kind === "rematch" || job.kind === "auto") && html`<div class="live-card card" aria-live="polite">
        <span class="live-dot"></span><span><b>Map matching</b> <span class="muted num">${job.message || "starting"}</span></span>
        <span class="bar"><i style=${`width:${job.progress}%`}></i></span></div>`}
      <div class="map-tools"><div class="card">
        <label><input type="checkbox" checked=${allPts} onChange=${(e) => setAllPts(e.target.checked)} />All GPS points</label>
      </div></div>
      ${noTrips && html`<div class="map-empty"><div class="card onboard"><header><h2>Get your trips in</h2>
        <p>Mapmatch keeps a copy of every original GPS point before it changes anything.</p></header>
        <div class="body"><ol>
          <li>${st.api_key_set ? html`<span class="row"><${Icon} name="check" size="sm" />Dawarich is connected.</span>`
            : html`<span><a href="#/settings">Connect your Dawarich account</a> with its API key, or import a CSV export.</span>`}</li>
          <li>Sync: the portal downloads your raw points and splits them into trips. Nothing is matched yet.</li>
          <li>Map match the trips you want, check each segment's mode, then push to Dawarich when you're happy.</li>
        </ol></div>
        <div class="foot"><button class="primary" disabled=${!st.api_key_set} onClick=${() => startJob("sync")}><${Icon} name="sync" size="sm" />Sync now</button>
          <a class="btn" href="#/settings">Settings</a></div></div></div>`}
    </div>
  </div>`;
}
