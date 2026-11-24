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
  if (!map || map._mm.theme === theme) return;
  map._mm.theme = theme;
  if (map._mm.fellBack) { applyAll(map); return; }
  // diff:false forces a full reload, which drops our layers; put them back
  // as soon as the new style is usable.
  map.setStyle(STYLES[theme], { diff: false });
  const again = () => { if (map.isStyleLoaded()) { map.off("styledata", again); applyAll(map); } };
  map.on("styledata", again);
}

// overlay: { data: GeoJSON, layers: () => [layer specs] } — layers is a function so
// colours are re-read from CSS after a theme change.
export function setOverlay(map, id, overlay) {
  map._mm.overlays.set(id, overlay);
  if (map.isStyleLoaded()) apply(map, id, overlay);
}

export function setData(map, id, data) {
  const o = map._mm.overlays.get(id);
  if (!o) return;
  o.data = data;
  const src = map.getSource(id);
  if (src) src.setData(data);
}

function applyAll(map) {
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
