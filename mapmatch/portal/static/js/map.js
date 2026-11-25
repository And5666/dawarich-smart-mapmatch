// MapLibre helpers. Overlays are re-applied whenever the basemap style changes.
/* global maplibregl */
const STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};
// Shown if the CARTO style cannot be fetched (offline, blocked): overlays still draw.
const BLANK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#e9ecf0" } }] };
const BLANK_DARK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#2a2f37" } }] };

// ---- dark basemap, lifted: CARTO's dark style is near-black, which hides roads
// and makes the coloured routes hard to read. Every colour is raised to a soft
// slate and greys get a cool tint, so the map reads as dim rather than black.
function parseColor(s) {
  if (typeof s !== "string") return null;
  let m = s.match(/^#([0-9a-f]{3,8})$/i);
  if (m) {
    let h = m[1];
    if (h.length <= 4) h = [...h].map((c) => c + c).join("");
    const n = parseInt(h.slice(0, 6), 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: h.length === 8 ? parseInt(h.slice(6), 16) / 255 : 1 };
  }
  m = s.match(/^rgba?\(([^)]+)\)$/i);
  if (m) { const v = m[1].split(/[\s,/]+/).filter(Boolean).map(Number); return { r: v[0], g: v[1], b: v[2], a: v[3] ?? 1 }; }
  m = s.match(/^hsla?\(([^)]+)\)$/i);
  if (m) {
    const v = m[1].split(/[\s,/]+/).filter(Boolean).map((x) => parseFloat(x));
    return { ...hslToRgb(v[0] / 360, v[1] / 100, v[2] / 100), a: v[3] ?? 1 };
  }
  return null;
}
function rgbToHsl({ r, g, b }) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2;
  if (mx === mn) return { h: 0, s: 0, l };
  const d = mx - mn, s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
  const h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return { h: h / 6, s, l };
}
function hslToRgb(h, s, l) {
  if (!s) return { r: l * 255, g: l * 255, b: l * 255 };
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s, p = 2 * l - q;
  const f = (t) => { t = (t + 1) % 1; return 255 * (t < 1 / 6 ? p + (q - p) * 6 * t : t < 1 / 2 ? q : t < 2 / 3 ? p + (q - p) * (2 / 3 - t) * 6 : p); };
  return { r: f(h + 1 / 3), g: f(h), b: f(h - 1 / 3) };
}
function lift(s) {
  const c = parseColor(s);
  if (!c) return s;
  let { h, s: sat, l } = rgbToHsl(c);
  l = 0.19 + l * 0.78;
  if (sat < 0.15) { h = 0.6; sat = Math.max(sat, 0.12); }
  const o = hslToRgb(h, sat, l);
  return `rgba(${Math.round(o.r)},${Math.round(o.g)},${Math.round(o.b)},${+c.a.toFixed(3)})`;
}
const liftValue = (v) => (typeof v === "string" ? lift(v) : Array.isArray(v) ? v.map(liftValue)
  : v && typeof v === "object" ? Object.fromEntries(Object.entries(v).map(([k, x]) => [k, liftValue(x)])) : v);
function liftDark(_prev, next) {
  for (const layer of next.layers || []) {
    for (const [k, v] of Object.entries(layer.paint || {})) if (/color$/.test(k)) layer.paint[k] = liftValue(v);
  }
  return next;
}
function loadBasemap(map, theme) {
  if (theme === "dark") map.setStyle(STYLES.dark, { diff: false, transformStyle: liftDark });
  else map.setStyle(STYLES.light, { diff: false });
}

export function createMap(el, theme, { center = [-0.9, 51.35], zoom = 9 } = {}) {
  const map = new maplibregl.Map({
    container: el, style: theme === "dark" ? BLANK_DARK : BLANK, center, zoom,
    attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false,
  });
  loadBasemap(map, theme);
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map._mm = { overlays: new Map(), theme };
  el.__map = map; // reachable from devtools / browser tests
  map.on("style.load", () => applyAll(map));
  map.on("error", (e) => {
    if (!map._mm.fellBack && e?.error && /style|Failed to fetch/i.test(String(e.error.message || e.error))) {
      map._mm.fellBack = true;
      map.setStyle(map._mm.theme === "dark" ? BLANK_DARK : BLANK);
    }
  });
  return map;
}

export function setMapTheme(map, theme) {
  if (gone(map) || map._mm.theme === theme) return;
  map._mm.theme = theme;
  if (map._mm.fellBack) { applyAll(map); return; }
  // diff:false forces a full reload, which drops our layers; put them back
  // as soon as the new style is usable.
  loadBasemap(map, theme);
  const again = () => { if (ready(map)) { map.off("styledata", again); applyAll(map); } };
  map.on("styledata", again);
}

// overlay: { data: GeoJSON, layers: () => [layer specs] } — layers is a function so
// colours are re-read from CSS after a theme change.
// A map that has been removed (the page navigated away) has no style left.
const gone = (map) => !map || map._removed || !map.style;
// isStyleLoaded() is false while tiles load; the style itself being ready is enough.
const ready = (map) => !!(map.style && map.style._loaded);

export function setOverlay(map, id, overlay) {
  if (gone(map)) return;
  map._mm.overlays.set(id, overlay);
  if (ready(map)) apply(map, id, overlay);
}

export function setData(map, id, data) {
  if (gone(map)) return;
  const o = map._mm.overlays.get(id);
  if (!o) return;
  o.data = data;
  const src = map.getSource(id);
  if (src) src.setData(data);
}

function applyAll(map) {
  if (gone(map)) return;
  for (const [id, o] of map._mm.overlays) apply(map, id, o);
}

function apply(map, id, o) {
  const src = map.getSource(id);
  if (src) src.setData(o.data);
  else map.addSource(id, { type: "geojson", data: o.data, promoteId: o.promoteId });
  for (const layer of o.layers()) {
    if (map.getLayer(layer.id)) map.removeLayer(layer.id);
    map.addLayer({ ...layer, source: id });
  }
}

export const lineFeature = (coords, props) => ({
  type: "Feature", properties: props, geometry: { type: "LineString", coordinates: coords.map((c) => [c[1], c[0]]) },
});
export const pointFeature = (lat, lon, props) => ({ type: "Feature", properties: props, geometry: { type: "Point", coordinates: [lon, lat] } });
export const fc = (features) => ({ type: "FeatureCollection", features });

export function fitCoords(map, coords, padding = 48, animate = true) {
  if (!coords.length) return;
  let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity;
  for (const [lat, lon] of coords) { a = Math.min(a, lon); b = Math.min(b, lat); c = Math.max(c, lon); d = Math.max(d, lat); }
  if (a === c && b === d) { map.jumpTo({ center: [a, b], zoom: 15 }); return; }
  const el = map.getContainer();
  const pad = Math.max(8, Math.min(padding, el.clientWidth / 6, el.clientHeight / 6));
  map.fitBounds([[a, b], [c, d]], { padding: pad, animate, maxZoom: 16, duration: animate ? 500 : 0 });
}

// "south,west,north,east" <-> numbers; null when the text is not four numbers.
export function parseBounds(text) {
  const v = String(text || "").split(/[,\s]+/).filter(Boolean).map(Number);
  if (v.length !== 4 || v.some((x) => !Number.isFinite(x))) return null;
  const [s, w, n, e] = v;
  return s < n && w < e ? { s, w, n, e } : null;
}
export const boundsText = (b) => [b.s, b.w, b.n, b.e].map((x) => x.toFixed(2)).join(",");
export const boundsFeature = (b) => ({ type: "Feature", properties: {}, geometry: { type: "Polygon",
  coordinates: [[[b.w, b.s], [b.e, b.s], [b.e, b.n], [b.w, b.n], [b.w, b.s]]] } });

// A small map showing one region's outline: static, or pannable for editing.
export function regionMap(el, theme, bounds, { interactive = false } = {}) {
  const map = new maplibregl.Map({ container: el, style: theme === "dark" ? BLANK_DARK : BLANK, interactive,
    center: [-2, 54], zoom: 3, attributionControl: false, dragRotate: false });
  map._mm = { overlays: new Map(), theme };
  loadBasemap(map, theme);
  map.on("style.load", () => applyAll(map));
  if (interactive) map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  const draw = (b) => {
    const o = { data: fc(b ? [boundsFeature(b)] : []), layers: () => [
      { id: "rb-fill", type: "fill", paint: { "fill-color": "#2f6ae0", "fill-opacity": 0.12 } },
      { id: "rb-line", type: "line", paint: { "line-color": "#2f6ae0", "line-width": 2 } },
    ] };
    if (map._mm.overlays.has("rb")) setData(map, "rb", o.data); else setOverlay(map, "rb", o);
  };
  map.showBounds = (b, fit = true) => {
    draw(b);
    if (b && fit) map.fitBounds([[b.w, b.s], [b.e, b.n]], { padding: interactive ? 30 : 12, animate: false, maxZoom: 9 });
  };
  map.once("load", () => map.showBounds(bounds));
  return map;
}
