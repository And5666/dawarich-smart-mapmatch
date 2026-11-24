// Trips overview: filters and stats, a day-grouped list, and a map of every trip.
import {
  html, useState, useEffect, useRef, useMemo, api, useStore, toast, Icon, ModeChip, StateBadge, PushBadge,
  MODES, modeColor, modeStyle, dominantMode, km, duration, time, day, dayKey, navigate,
} from "../lib.js";
import { startJob } from "../session.js";
import { createMap, setMapTheme, setOverlay, setData, fc, lineFeature, fitCoords } from "../map.js";

const RANGES = [["7d", "7 days", 7], ["30d", "30 days", 30], ["90d", "90 days", 90], ["1y", "Year", 365], ["all", "All", 0]];
const FILTERS = [
  ["all", "All"], ["review", "Needs review"], ["failed", "Failed"], ["original", "Not pushed"], ["outdated", "Edited since push"],
];
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
    map.on("mousemove", "trips-line", (e) => {
      const f = e.features[0];
      map.getCanvas().style.cursor = "pointer";
      setHot(f.properties.id);
      pop.setLngLat(e.lngLat).setHTML(`<b>${f.properties.label}</b><br>${f.properties.sub}`).addTo(map);
    });
    map.on("mouseleave", "trips-line", () => { map.getCanvas().style.cursor = ""; setHot(null); pop.remove(); });
    map.on("click", "trips-line", (e) => navigate(`#/trip/${e.features[0].properties.id}`));
    return () => map.remove();
  }, []);
  useEffect(() => setMapTheme(mapRef.current, theme), [theme]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const feats = trips.filter((t) => t.preview.length > 1).map((t) => lineFeature(t.preview, {
      id: t.id, mode: dominantMode(t.modes),
      label: `${day(t.start)} · ${time(t.start)}–${time(t.end)}`,
      sub: `${km(t.km)} · ${t.modes.map((m) => MODES[m.mode]?.label).join(" → ") || "not matched"}`,
    }));
    const overlay = {
      data: fc(feats), promoteId: "id",
      layers: () => [
        { id: "trips-casing", type: "line", layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": getComputedStyle(document.documentElement).getPropertyValue("--casing").trim(), "line-width": 5, "line-opacity": 0.9 } },
        { id: "trips-line", type: "line", layout: { "line-cap": "round", "line-join": "round" },
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
  }, [trips]);

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
    for (const t of trips) for (const m of t.modes) { byMode[m.mode] = (byMode[m.mode] || 0) + m.km; total += m.km; }
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
        ${totals.total > 0 && html`<div class="stack" style="gap:6px">
          <div class="modebar">${Object.entries(totals.byMode).sort((a, b) => b[1] - a[1]).map(([m, v]) =>
            html`<i style=${`${modeStyle(m)};width:${(100 * v) / totals.total}%`} title=${`${MODES[m]?.label}: ${km(v)}`}></i>`)}</div>
          <div class="legend">${Object.entries(totals.byMode).sort((a, b) => b[1] - a[1]).map(([m, v]) =>
            html`<button class=${`ghost sm ${mode === m ? "on" : ""}`} style=${`${modeStyle(m)};padding:1px 4px`} aria-pressed=${mode === m}
              onClick=${() => setMode(mode === m ? "" : m)} title="Show only trips with this mode">
              <span class="dot"></span>${MODES[m]?.label} <span class="num faint">${km(v)}</span></button>`)}</div></div>`}
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
              : html`<span class="faint small">${t.error || "waiting to be matched"}</span>`}</div>
              <span class="grow"></span>${t.push !== "original" && html`<${PushBadge} push=${t.push} />`}</div>
          </div>`)}
        </div>`)}
      </div>
    </aside>
    <div class="map-wrap">
      <div class="map" ref=${mapEl}></div>
      ${noTrips && html`<div class="map-empty"><div class="card onboard"><header><h2>Get your trips in</h2>
        <p>Mapmatch keeps a copy of every original GPS point before it changes anything.</p></header>
        <div class="body"><ol>
          <li>${st.api_key_set ? html`<span class="row"><${Icon} name="check" size="sm" />Dawarich is connected.</span>`
            : html`<span><a href="#/settings">Connect your Dawarich account</a> with its API key, or import a CSV export.</span>`}</li>
          <li>Sync: the portal downloads your points, splits them into trips and matches each one to the map.</li>
          <li>Review a trip, fix any segment's mode, then push it to Dawarich, or turn on automatic push.</li>
        </ol></div>
        <div class="foot"><button class="primary" disabled=${!st.api_key_set} onClick=${() => startJob("sync")}><${Icon} name="sync" size="sm" />Sync now</button>
          <a class="btn" href="#/settings">Settings</a></div></div></div>`}
    </div>
  </div>`;
}
