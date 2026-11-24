// MapLibre helpers. Overlays are re-applied whenever the basemap style changes.
/* global maplibregl */
const STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};
// Shown if the CARTO style cannot be fetched (offline, blocked): overlays still draw.
const BLANK = { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#e9ecf0" } }] };

export function createMap(el, theme, { center = [-0.9, 51.35], zoom = 9 } = {}) {
  const map = new maplibregl.Map({
    container: el, style: STYLES[theme] || STYLES.light, center, zoom,
    attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false,
  });
  map.touchZoomRotate.disableRotation();
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  map._mm = { overlays: new Map(), theme };
  el.__map = map; // reachable from devtools / browser tests
  map.on("style.load", () => applyAll(map));
  map.on("error", (e) => {
    if (!map._mm.fellBack && e?.error && /style|Failed to fetch/i.test(String(e.error.message || e.error))) {
      map._mm.fellBack = true;
      map.setStyle(BLANK);
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
  map.setStyle(STYLES[theme], { diff: false });
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
  const map = new maplibregl.Map({ container: el, style: STYLES[theme] || STYLES.light, interactive,
    center: [-2, 54], zoom: 3, attributionControl: false, dragRotate: false });
  map._mm = { overlays: new Map(), theme };
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
