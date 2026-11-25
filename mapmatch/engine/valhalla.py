"""Valhalla client, standard library only.

`match()` turns a `trace_attributes` response into continuous matched pieces
and an exact arc position for every fix, using edge shape indices rather than
re-projecting points onto the shape. `route()` asks for a road route between
anchors, optionally through intermediate waypoints.

Set VALHALLA_CACHE_DIR to cache responses on disk (benchmarks, tests).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .geo import cumulative, decode_polyline

# Matching knobs must go in "trace_options": Valhalla silently ignores them at
# the top level (the pre-redesign engine never had its settings applied).
# This server caps search_radius at 100 m and breakage_distance at 5000 m.
PROFILES: dict[str, dict[str, Any]] = {
    "auto": {
        "costing": "auto",
        "trace_options": {"gps_accuracy": 25, "search_radius": 100,
                          "breakage_distance": 3000, "turn_penalty_factor": 200},
        "costing_options": {"auto": {"use_highways": 1.0, "use_tolls": 0.5}},
    },
    "bicycle": {
        "costing": "bicycle",
        "trace_options": {"gps_accuracy": 25, "search_radius": 80, "breakage_distance": 2000},
        # Bikes are not held to car rules: contraflow and one-way streets are
        # common for cyclists, so one-ways and turn restrictions are ignored.
        "costing_options": {"bicycle": {
            "use_trails": 1.0, "avoid_bad_surfaces": 0.0,
            "bicycle_type": "Mountain", "use_roads": 0.4, "use_hills": 1.0,
            "ignore_oneways": True, "ignore_restrictions": True,
        }},
    },
    "pedestrian": {
        "costing": "pedestrian",
        "trace_options": {"gps_accuracy": 25, "search_radius": 80, "breakage_distance": 2000},
        # Valhalla's default only allows easy hiking paths (sac_scale 1), which
        # left mountain paths such as Snowdon's unmatched; allow all grades.
        "costing_options": {"pedestrian": {
            "use_tracks": 1.0, "walkway_factor": 0.8,
            "sidewalk_factor": 1.0, "max_hiking_difficulty": 6,
            "ignore_oneways": True,
        }},
    },
}
MAX_RADIUS = 100
# Fixes that report no accuracy (GPX from a bike computer or a watch) are
# usually the most precise of all, logged every second. Among the candidates in
# its search radius Valhalla prefers a road: a road 9 m away beats the cycle
# track a metre from the fix. The only lever that works is the radius itself,
# and the server never searches less than gps_accuracy, so both are narrowed
# when most of a request is such fixes (a phone track with the odd fix missing
# its accuracy is searched as before). Holes the narrow search leaves are
# routed by fill.py, and match.py retries wide where it loses the trace.
NO_ACCURACY_RADIUS = 10
NARROW_GPS_ACCURACY = 5


def unreported(points: list[dict[str, Any]]) -> bool:
    """Most fixes report no accuracy: a GPX-style track."""
    return sum(1 for p in points if not p.get("accuracy")) * 2 > len(points)


def breakage(profile: str) -> float:
    return float(PROFILES[profile]["trace_options"]["breakage_distance"])


ATTRIBUTES = [
    "shape",
    "matched.point",
    "matched.type",
    "matched.edge_index",
    "matched.distance_along_edge",
    "matched.distance_from_trace_point",
    "matched.begin_route_discontinuity",
    "matched.end_route_discontinuity",
    "edge.begin_shape_index",
    "edge.end_shape_index",
    "edge.source_percent_along",
    "edge.target_percent_along",
    "edge.road_class",
    "edge.use",
    "edge.length",
]


class ValhallaError(RuntimeError):
    pass


# The portal keeps the Valhalla address in its database (one server for every
# account) and configures it at start-up and whenever an admin changes it. The
# environment variable stays as the fallback for the bench and for development.
_configured: str | None = None


def configure(url: str | None) -> None:
    global _configured
    _configured = (url or "").strip().rstrip("/") or None


def configured() -> str:
    return _configured or ""


def base_url() -> str:
    return _configured or os.environ.get("VALHALLA_URL", "http://127.0.0.1:8002").rstrip("/")


def _cache_path(path: str, body: dict[str, Any]) -> Path | None:
    d = os.environ.get("VALHALLA_CACHE_DIR")
    if not d:
        return None
    key = hashlib.sha256((path + json.dumps(body, sort_keys=True)).encode()).hexdigest()
    p = Path(d) / key[:2] / f"{key}.json"
    return p


def post(path: str, body: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    cp = _cache_path(path, body)
    if cp is not None and cp.exists():
        cached = json.loads(cp.read_text())
        if "__error__" in cached:
            raise ValhallaError(cached["__error__"])
        return cached
    req = urllib.request.Request(
        f"{base_url()}{path}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    err: str | None = None
    out: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        # 4xx are deterministic answers (no path, no route) and safe to cache.
        if 400 <= exc.code < 500:
            err = f"HTTP {exc.code}: {detail}"
        else:
            raise ValhallaError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001 - network errors are not cached
        raise ValhallaError(str(exc)) from exc
    if cp is not None:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps({"__error__": err} if err else out))
        tmp.replace(cp)
    if err:
        raise ValhallaError(err)
    return out


def _shape_item(p: dict[str, Any], default_radius: float, narrow: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {"lat": float(p["lat"]), "lon": float(p["lon"])}
    if p.get("timestamp") is not None:
        item["time"] = int(p["timestamp"])
    acc = p.get("accuracy")
    if acc:
        # Per-fix search radius follows the reported accuracy.
        item["radius"] = int(max(30.0, min(MAX_RADIUS, 1.5 * float(acc), default_radius * 1.25)))
    elif narrow:
        item["radius"] = NO_ACCURACY_RADIUS
    return item


def match(points: list[dict[str, Any]], profile: str, timeout: float = 120.0,
          wide: bool = False) -> dict[str, Any]:
    """Map-match points with one profile.

    A GPX-style request (see unreported) is searched narrowly unless
    wide=True, which uses the profile's radius as for any other track.

    Returns {"pieces": [{"poly", "cum"}], "anchors": [anchor | None per point]}.
    An anchor is {"piece", "arc", "lat", "lon", "dist", "kind"} where kind is
    the edge road class/use, used for mode scoring.
    """
    cfg = PROFILES[profile]
    r0 = cfg["trace_options"]["search_radius"]
    trace_options = cfg["trace_options"]
    narrow = not wide and unreported(points)
    if narrow:
        trace_options = {**trace_options, "gps_accuracy": NARROW_GPS_ACCURACY}
    body = {
        "shape": [_shape_item(p, r0, narrow) for p in points],
        "costing": cfg["costing"],
        "costing_options": cfg["costing_options"],
        "shape_match": "map_snap",
        "trace_options": trace_options,
        "filters": {"attributes": ATTRIBUTES, "action": "include"},
    }
    raw = post("/trace_attributes", body, timeout)
    poly = decode_polyline(raw.get("shape") or "", 6)
    edges = raw.get("edges") or []
    if len(poly) < 2 or not edges:
        return {"pieces": [], "anchors": [None] * len(points)}

    # Split the shape into continuous pieces. A break is where the next edge
    # does not start on the vertex the previous one ended on.
    piece_of_edge: list[int] = []
    bounds: list[list[int]] = []  # [first_vertex, last_vertex] per piece
    for i, e in enumerate(edges):
        b, t = int(e.get("begin_shape_index", 0)), int(e.get("end_shape_index", 0))
        if i == 0 or b != int(edges[i - 1].get("end_shape_index", -1)):
            bounds.append([b, t])
        else:
            bounds[-1][1] = t
        piece_of_edge.append(len(bounds) - 1)
    pieces = []
    for b, t in bounds:
        sub = poly[b:t + 1]
        pieces.append({"poly": sub, "cum": cumulative(sub), "offset": b})

    anchors: list[dict[str, Any] | None] = []
    last_arc: dict[int, float] = {}
    for j, mp in enumerate(raw.get("matched_points") or []):
        if j >= len(points):
            break
        ei = mp.get("edge_index")
        if mp.get("type") not in ("matched", "interpolated") or not isinstance(ei, int) \
                or not 0 <= ei < len(edges):
            anchors.append(None)
            continue
        e = edges[ei]
        pi = piece_of_edge[ei]
        pc = pieces[pi]
        b = int(e.get("begin_shape_index", 0)) - pc["offset"]
        t = int(e.get("end_shape_index", 0)) - pc["offset"]
        s0 = float(e.get("source_percent_along", 0.0) or 0.0)
        s1 = float(e.get("target_percent_along", 1.0) or 1.0)
        f = float(mp.get("distance_along_edge") or 0.0)
        frac = 0.0 if s1 - s0 <= 1e-9 else max(0.0, min(1.0, (f - s0) / (s1 - s0)))
        cum = pc["cum"]
        b = max(0, min(b, len(cum) - 1))
        t = max(0, min(t, len(cum) - 1))
        arc = cum[b] + frac * (cum[t] - cum[b])
        # The path is in travel order; never step backwards inside a piece.
        arc = max(arc, last_arc.get(pi, 0.0))
        last_arc[pi] = arc
        anchors.append({
            "piece": pi, "arc": arc,
            "lat": float(mp.get("lat")), "lon": float(mp.get("lon")),
            "dist": float(mp.get("distance_from_trace_point") or 0.0),
            "kind": f"{e.get('road_class') or ''} {e.get('use') or ''}".strip(),
        })
    while len(anchors) < len(points):
        anchors.append(None)
    for pc in pieces:
        pc.pop("offset", None)
    return {"pieces": pieces, "anchors": anchors}


def route(locations: list[dict[str, Any]], profile: str = "auto",
          timeout: float = 60.0) -> dict[str, Any] | None:
    """Route through locations (first/last are break points, middle are `through`)."""
    if len(locations) < 2:
        return None
    cfg = PROFILES.get(profile, PROFILES["auto"])
    locs = []
    for k, p in enumerate(locations):
        item: dict[str, Any] = {"lat": float(p["lat"]), "lon": float(p["lon"])}
        if 0 < k < len(locations) - 1:
            item["type"] = "through"
        locs.append(item)
    body = {"locations": locs, "costing": cfg["costing"],
            "costing_options": cfg["costing_options"],
            "directions_options": {"units": "kilometers", "directions_type": "none"}}
    try:
        raw = post("/route", body, timeout)
    except ValhallaError:
        return None
    trip = raw.get("trip") or {}
    legs = trip.get("legs") or []
    if not legs:
        return None
    shape: list[tuple[float, float]] = []
    for leg in legs:
        pts = decode_polyline(leg.get("shape") or "", 6)
        if shape and pts and shape[-1] == pts[0]:
            pts = pts[1:]
        shape.extend(pts)
    summary = trip.get("summary") or {}
    return {"shape": shape, "length_m": float(summary.get("length") or 0) * 1000.0,
            "time_s": float(summary.get("time") or 0)}


def status(timeout: float = 5.0, url: str | None = None) -> dict[str, Any]:
    base = (url or base_url()).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/status", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
