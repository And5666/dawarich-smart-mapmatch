"""Valhalla trace_attributes client using only the standard library."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .geo import decode_polyline

VALHALLA_URL = os.environ.get("VALHALLA_URL", "http://127.0.0.1:8002").rstrip("/")

PROFILES: dict[str, dict[str, Any]] = {
    "auto": {
        "costing": "auto",
        "search_radius": 80,
        "gps_accuracy": 15,
        "breakage_distance": 6000,
        "costing_options": {"auto": {"use_highways": 1.0, "use_tolls": 0.5}},
    },
    "bicycle": {
        "costing": "bicycle",
        "search_radius": 110,
        "gps_accuracy": 18,
        "breakage_distance": 3000,
        "costing_options": {"bicycle": {
            "use_trails": 1.0, "avoid_bad_surfaces": 0.0,
            "bicycle_type": "Mountain", "use_roads": 0.4, "use_hills": 1.0,
        }},
    },
    "pedestrian": {
        "costing": "pedestrian",
        "search_radius": 130,
        "gps_accuracy": 20,
        "breakage_distance": 2000,
        "costing_options": {"pedestrian": {
            "use_tracks": 1.0, "walkway_factor": 0.8,
            "sidewalk_factor": 1.0, "max_grade": 40,
        }},
    },
}

ATTRIBUTES = [
    "shape",
    "matched.point",
    "matched.type",
    "matched.edge_index",
    "matched.matchings_index",
    "matched.percent_along",
    "matched.distance_from_trace_point",
    "edge.way_id",
    "edge.names",
    "edge.road_class",
    "edge.use",
    "edge.length",
    "edge.speed",
    "edge.travel_mode",
]


class ValhallaError(RuntimeError):
    pass


def _post(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{VALHALLA_URL}{path}", data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise ValhallaError(f"HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValhallaError(str(exc)) from exc
    try:
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValhallaError(f"bad JSON: {raw[:200]!r}") from exc


def trace_attributes(points: list[dict[str, Any]], profile: str, *,
                     timeout: float = 180.0,
                     breakage_distance: int | None = None) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile}")
    cfg = PROFILES[profile]
    shape = []
    for p in points:
        item = {"lat": float(p["lat"]), "lon": float(p["lon"])}
        if p.get("timestamp") is not None:
            item["time"] = int(p["timestamp"])
        if p.get("accuracy") is not None:
            item["accuracy"] = float(p["accuracy"])
        shape.append(item)
    body = {
        "shape": shape,
        "costing": cfg["costing"],
        "costing_options": cfg["costing_options"],
        "shape_match": "map_snap",
        "search_radius": cfg["search_radius"],
        "gps_accuracy": cfg["gps_accuracy"],
        "breakage_distance": int(breakage_distance if breakage_distance is not None
                                 else cfg["breakage_distance"]),
        "filters": {"attributes": ATTRIBUTES, "action": "include"},
    }
    raw = _post("/trace_attributes", body, timeout)
    if raw.get("error_code"):
        raise ValhallaError(f"{raw.get('error')} ({raw.get('error_code')})")
    poly = decode_polyline(raw.get("shape") or "", 6)
    edges = raw.get("edges") or []
    matched: list[dict[str, Any]] = []
    for mp in raw.get("matched_points") or []:
        idx = mp.get("edge_index")
        edge = edges[idx] if isinstance(idx, int) and 0 <= idx < len(edges) else {}
        matched.append({
            "lat": mp.get("lat"),
            "lon": mp.get("lon"),
            "dist": float(mp.get("distance_from_trace_point") or 0.0),
            "type": mp.get("type") or "matched",
            "edge_index": idx,
            "matchings_index": mp.get("matchings_index"),
            "percent_along": mp.get("percent_along"),
            "road_class": edge.get("road_class") or "",
            "use": edge.get("use") or "",
            "way_id": edge.get("way_id"),
            "speed": edge.get("speed"),
        })
    return {
        "shape": poly,
        "matched_points": matched,
        "edges": edges,
        "raw": raw,
    }


def route_between(a: dict[str, Any], b: dict[str, Any], profile: str = "auto",
                  timeout: float = 60.0) -> dict[str, Any] | None:
    """Route between two reliable anchors. Used only to rescue a road section
    that map_snap abandoned because the fixes were sparse."""
    if profile not in PROFILES:
        profile = "auto"
    body = {
        "locations": [{"lat": float(a["lat"]), "lon": float(a["lon"])},
                      {"lat": float(b["lat"]), "lon": float(b["lon"])}],
        "costing": PROFILES[profile]["costing"],
        "costing_options": PROFILES[profile]["costing_options"],
        "directions_options": {"units": "kilometers"},
    }
    try:
        raw = _post("/route", body, timeout)
    except ValhallaError:
        return None
    trip = raw.get("trip") or {}
    legs = trip.get("legs") or []
    if not legs:
        return None
    summary = trip.get("summary") or {}
    return {"shape": decode_polyline(legs[0].get("shape") or "", 6),
            "length_m": float(summary.get("length") or 0) * 1000.0,
            "time_s": float(summary.get("time") or 0)}


def status(timeout: float = 5.0) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{VALHALLA_URL}/status", timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
