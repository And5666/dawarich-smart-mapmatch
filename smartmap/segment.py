"""Turn raw fixes into sections and mark states that must stay raw."""
from __future__ import annotations

import statistics
from typing import Any

from .geo import haversine

GAP_S = 900
GAP_M = 8000
SPIKE_KMH = 230.0
STOP_RADIUS = 30.0
STOP_MIN_S = 90.0
STOP_MIN_POINTS = 4


def _speed(a: dict[str, Any], b: dict[str, Any]) -> float:
    dt = b["timestamp"] - a["timestamp"]
    if dt <= 0:
        return 0.0
    return haversine(a["lat"], a["lon"], b["lat"], b["lon"]) / dt * 3.6


def dedup_exact(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int, int]] = set()
    for p in points:
        k = (int(p["timestamp"]), round(float(p["lat"]) * 1e6), round(float(p["lon"]) * 1e6))
        if k in seen:
            continue
        seen.add(k)
        out.append(dict(p))
    return out


def stationary_runs(points: list[dict[str, Any]], radius: float = STOP_RADIUS,
                    min_s: float = STOP_MIN_S, min_points: int = STOP_MIN_POINTS) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(points)
    while i < n:
        j = i + 1
        while j < n:
            if haversine(points[i]["lat"], points[i]["lon"],
                         points[j]["lat"], points[j]["lon"]) > radius:
                break
            j += 1
        dur = points[j - 1]["timestamp"] - points[i]["timestamp"]
        if j - i >= min_points and dur >= min_s:
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def features(points: list[dict[str, Any]]) -> dict[str, float]:
    if len(points) < 2:
        return {"speed": 0.0, "p95": 0.0, "path": 0.0, "straight": 1.0, "gap": 0.0}
    speeds, path = [], 0.0
    for a, b in zip(points, points[1:]):
        d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        dt = max(1, b["timestamp"] - a["timestamp"])
        path += d
        speeds.append(d / dt * 3.6)
    disp = haversine(points[0]["lat"], points[0]["lon"], points[-1]["lat"], points[-1]["lon"])
    speeds.sort()
    tgaps = sorted(max(1, b["timestamp"] - a["timestamp"]) for a, b in zip(points, points[1:]))
    return {
        "speed": speeds[len(speeds) // 2] if speeds else 0.0,
        "p95": speeds[int(len(speeds) * 0.95)] if speeds else 0.0,
        "path": path,
        "straight": disp / path if path > 1 else 1.0,
        "gap": path / max(1, len(points) - 1),
        "tgap": tgaps[len(tgaps) // 2] if tgaps else 0.0,
    }


def prepare(points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (fixes, sections). Each fix has a base state in {move, stationary, gap, spike}."""
    pts = sorted(dedup_exact(points), key=lambda p: (int(p["timestamp"]), p["lat"], p["lon"]))
    for i, p in enumerate(pts):
        p["_i"] = i
        p["_speed"] = 0.0 if i == 0 else _speed(pts[i - 1], p)
        p["_state"] = "move"

    stops: list[tuple[int, int]] = []
    for a, b in stationary_runs(pts):
        stops.append((a, b))
        for k in range(a, b):
            pts[k]["_state"] = "stationary"

    # A single implausible fix is a spike, not a shortcut. Keep it, never match it.
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        if b["_state"] != "move":
            continue
        d1 = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        d2 = haversine(b["lat"], b["lon"], c["lat"], c["lon"])
        d3 = haversine(a["lat"], a["lon"], c["lat"], c["lon"])
        if d1 > 150 and d2 > 150 and d1 + d2 > 2.0 * d3 + 500:
            b["_state"] = "spike"

    # Long gaps break the trace. Do not route across them.
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        dt = b["timestamp"] - a["timestamp"]
        d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        if dt > GAP_S or (d > GAP_M and dt > 0 and d / dt * 3.6 > 300):
            if b["_state"] == "move":
                b["_state"] = "gap"

    # Movement sections are maximal runs of move fixes. Stationary/gap/spike
    # fixes get their own one-state sections so their raw points are preserved.
    sections: list[dict[str, Any]] = []
    i = 0
    while i < len(pts):
        state = pts[i]["_state"]
        if state != "move":
            sections.append({"kind": state, "start": i, "end": i + 1, "points": pts[i:i + 1]})
            i += 1
            continue
        j = i + 1
        while j < len(pts) and pts[j]["_state"] == "move":
            j += 1
        # a move section needs at least two fixes; one fix is raw
        if j - i < 2:
            pts[i]["_state"] = "raw"
            sections.append({"kind": "raw", "start": i, "end": j, "points": pts[i:j]})
        else:
            sections.append({"kind": "move", "start": i, "end": j, "points": pts[i:j]})
        i = j
    return pts, sections


def section_features(section: dict[str, Any]) -> dict[str, float]:
    return features(section["points"])
