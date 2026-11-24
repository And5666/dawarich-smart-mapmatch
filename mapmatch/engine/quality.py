"""Quality metrics for a matched trip (ported from smartmap/metrics.py)."""
from __future__ import annotations

import bisect
from typing import Any

from .geo import dist_point_segment, haversine


def path_km(points: list[Any]) -> float:
    if len(points) < 2:
        return 0.0
    if isinstance(points[0], dict):
        seq = [(p["lat"], p["lon"]) for p in points]
    else:
        seq = [(p[0], p[1]) for p in points]
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(seq, seq[1:])) / 1000.0


def deviations(raw: list[dict[str, Any]], out: list[dict[str, Any]], window_s: int = 90) -> list[float]:
    """Distance from each raw fix to the output line near the same time."""
    if not out:
        return [1e9] * len(raw)
    ts = [int(g["timestamp"]) for g in out]
    errs = []
    for p in raw:
        lo = max(0, bisect.bisect_left(ts, p["timestamp"] - window_s) - 1)
        hi = min(len(out), bisect.bisect_right(ts, p["timestamp"] + window_s) + 1)
        near = out[lo:hi]
        if not near:
            errs.append(1e9)
        elif len(near) == 1:
            errs.append(haversine(p["lat"], p["lon"], near[0]["lat"], near[0]["lon"]))
        else:
            errs.append(min(dist_point_segment(p["lat"], p["lon"], a["lat"], a["lon"], b["lat"], b["lon"])
                            for a, b in zip(near, near[1:])))
    return errs


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


def count_spikes(points: list[dict[str, Any]], max_kmh: float = 180.0) -> int:
    n = 0
    for a, b in zip(points, points[1:]):
        dt = max(1, int(b["timestamp"]) - int(a["timestamp"]))
        if haversine(a["lat"], a["lon"], b["lat"], b["lon"]) / dt * 3.6 > max_kmh:
            n += 1
    return n
