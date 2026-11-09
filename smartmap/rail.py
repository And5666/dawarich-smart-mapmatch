"""Rail shapes from the GB National Rail GTFS feed.

The feed gives route alignments, not a routed graph. That is enough for a
train: snap to the nearest shape, keep one shape through the run, and never
send the line to the road graph.
"""
from __future__ import annotations

import gzip
import json
import math
import os
import threading
from typing import Any

DATA_DIR = os.environ.get("RAIL_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
SHAPES_PATH = os.path.join(DATA_DIR, "rail_shapes.json.gz")
STATIONS_PATH = os.path.join(DATA_DIR, "rail_stations.json.gz")
CELL = 0.01
R = 6371008.8

_shapes: list[list[tuple[float, float]]] = []
_cum: list[list[float]] = []
_index: dict[tuple[int, int], list[tuple[int, int]]] = {}
_stations: list[tuple[float, float]] = []
_loaded = False
_stations_loaded = False
_lock = threading.Lock()


def _hav(a: float, b: float, c: float, d: float) -> float:
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(x)))


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        with gzip.open(SHAPES_PATH, "rt") as f:
            raw = json.load(f)
        for _sid, pts in raw.items():
            if len(pts) < 2:
                continue
            si = len(_shapes)
            line = [(float(a), float(b)) for a, b in pts]
            _shapes.append(line)
            cum = [0.0]
            for a, b in zip(line, line[1:]):
                cum.append(cum[-1] + _hav(a[0], a[1], b[0], b[1]))
            _cum.append(cum)
            for pi in range(len(line) - 1):
                lat = (line[pi][0] + line[pi + 1][0]) / 2
                lon = (line[pi][1] + line[pi + 1][1]) / 2
                _index.setdefault((int(lat / CELL), int(lon / CELL)), []).append((si, pi))
        _loaded = True


def _load_stations() -> None:
    global _stations_loaded
    if _stations_loaded:
        return
    with _lock:
        if _stations_loaded:
            return
        with gzip.open(STATIONS_PATH, "rt") as f:
            raw = json.load(f)
        _stations.extend((float(s["lat"]), float(s["lon"])) for s in raw)
        _stations_loaded = True


def _project(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    ax, ay = a[1] * kx, a[0] * ky
    bx, by = b[1] * kx, b[0] * ky
    px, py = lon * kx, lat * ky
    dx, dy = bx - ax, by - ay
    s2 = dx * dx + dy * dy
    t = 0.0 if s2 <= 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / s2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def nearest(lat: float, lon: float) -> dict[str, Any] | None:
    _load()
    ci, cj = int(lat / CELL), int(lon / CELL)
    best = None
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for si, pi in _index.get((ci + di, cj + dj), ()):
                line = _shapes[si]
                d, t = _project(lat, lon, line[pi], line[pi + 1])
                if best is None or d < best["dist"]:
                    best = {"shape": si, "seg": pi, "t": t, "dist": d}
    if best is None:
        return None
    line = _shapes[best["shape"]]
    a, b = line[best["seg"]], line[best["seg"] + 1]
    best["lat"] = a[0] + (b[0] - a[0]) * best["t"]
    best["lon"] = a[1] + (b[1] - a[1]) * best["t"]
    best["arc"] = _cum[best["shape"]][best["seg"]] + best["t"] * (
        _cum[best["shape"]][best["seg"] + 1] - _cum[best["shape"]][best["seg"]])
    return best


def shape_line(si: int) -> list[tuple[float, float]]:
    _load()
    return _shapes[si]


def shape_cum(si: int) -> list[float]:
    _load()
    return _cum[si]


def is_train(points: list[dict[str, Any]], radius: float = 45.0) -> float:
    if len(points) < 6:
        return 0.0
    step = max(1, len(points) // 60)
    close = total = 0
    for p in points[::step]:
        n = nearest(p["lat"], p["lon"])
        total += 1
        if n and n["dist"] <= radius:
            close += 1
    return close / max(1, total)


def station_distance(lat: float, lon: float) -> float:
    _load_stations()
    return min((_hav(lat, lon, s[0], s[1]) for s in _stations), default=1e9)


def train_score(points: list[dict[str, Any]]) -> dict[str, Any]:
    frac = is_train(points)
    if len(points) < 6:
        return {"train": False, "rail_frac": frac, "p95_kmh": 0.0, "station": False}
    speeds = []
    for a, b in zip(points, points[1:]):
        dt = b["timestamp"] - a["timestamp"]
        if dt > 0:
            speeds.append(_hav(a["lat"], a["lon"], b["lat"], b["lon"]) / dt * 3.6)
    speeds.sort()
    med = speeds[len(speeds) // 2] if speeds else 0.0
    p95 = speeds[int(len(speeds) * 0.95)] if speeds else 0.0
    start_station = station_distance(points[0]["lat"], points[0]["lon"]) <= 900.0
    end_station = station_distance(points[-1]["lat"], points[-1]["lon"]) <= 900.0
    train = (frac >= 0.48 and 5.0 <= med <= 200.0) or (
        frac >= 0.42 and 5.0 <= med <= 200.0 and (start_station or end_station)
    )
    return {"train": train, "rail_frac": frac, "p95_kmh": p95, "med_kmh": med,
            "station": start_station or end_station}
