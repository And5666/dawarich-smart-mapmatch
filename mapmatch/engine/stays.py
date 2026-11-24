"""Stay detection: runs of fixes that remain within a small radius."""
from __future__ import annotations

from typing import Any

from .geo import haversine

STAY_RADIUS_M = 50.0
STAY_MIN_S = 180
# A run whose first and last quarters sit this far apart is slow movement (a
# scramble, a queue), not a stop: the running centroid trails a slow walker,
# so the radius test alone would call 1 km/h progress a stay.
DRIFT_M = 30.0
DRIFT_MIN_FIXES = 3
DRIFT_MIN_KMH = 0.25   # slower than this is GPS wander during a real stop


def _mean(pts: list[dict[str, Any]]) -> tuple[float, float]:
    return sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts)


def drifts(run: list[dict[str, Any]]) -> bool:
    """True when a candidate stay is really steady progress."""
    if len(run) < DRIFT_MIN_FIXES:
        return False
    q = max(1, len(run) // 4)
    a, b = _mean(run[:q]), _mean(run[-q:])
    acc = sorted(p.get("accuracy") or 20.0 for p in run)[len(run) // 2]
    d = haversine(a[0], a[1], b[0], b[1])
    dt = max(1, run[-1]["timestamp"] - run[0]["timestamp"])
    return d > DRIFT_M + 0.5 * max(0.0, acc - 10.0) and d / dt * 3.6 >= DRIFT_MIN_KMH


def find_stays(fixes: list[dict[str, Any]], radius: float = STAY_RADIUS_M,
               min_s: int = STAY_MIN_S, trim: bool = False) -> list[tuple[int, int]]:
    """Return [start, end) index runs that are stays.

    A run grows while each fix stays within `radius` of the run's running
    centroid. It counts as a stay when it lasts at least `min_s`. Sparse
    logging is fine: two fixes an hour apart at the same place are a stay.
    """
    runs: list[tuple[int, int]] = []
    n = len(fixes)
    i = 0
    while i < n:
        lat, lon = fixes[i]["lat"], fixes[i]["lon"]
        k = 1
        j = i + 1
        while j < n:
            p = fixes[j]
            r = radius + min(30.0, max(0.0, (p.get("accuracy") or 0.0) - 20.0))
            if haversine(lat, lon, p["lat"], p["lon"]) > r:
                break
            lat = (lat * k + p["lat"]) / (k + 1)
            lon = (lon * k + p["lon"]) / (k + 1)
            k += 1
            j += 1
        if j - i >= 2 and fixes[j - 1]["timestamp"] - fixes[i]["timestamp"] >= min_s \
                and not drifts(fixes[i:j]):
            # Trimmed for labelling a trip's modes; trip splitting keeps the
            # whole dwell, arrival included.
            a, b = _trim(fixes, i, j) if trim else (i, j)
            if fixes[b - 1]["timestamp"] - fixes[a]["timestamp"] >= min_s:
                runs.append((a, b))
                i = b
                continue
        i += 1
    return runs


def _trim(fixes: list[dict[str, Any]], i: int, j: int) -> tuple[int, int]:
    """Drop the arriving and leaving fixes the running centroid swallowed: the
    stop is where the fixes cluster, not the slow approach to it."""
    c = _mean(fixes[i:j])

    def far(p: dict[str, Any]) -> bool:
        return haversine(c[0], c[1], p["lat"], p["lon"]) > max(20.0, 1.5 * (p.get("accuracy") or 10.0))

    while j - i > 2 and far(fixes[i]):
        i += 1
    while j - i > 2 and far(fixes[j - 1]):
        j -= 1
    return i, j


def centroid(fixes: list[dict[str, Any]]) -> tuple[float, float]:
    """Accuracy-weighted centre of a stay."""
    wsum = lat = lon = 0.0
    for p in fixes:
        w = 1.0 / max(5.0, p.get("accuracy") or 20.0) ** 2
        wsum += w
        lat += p["lat"] * w
        lon += p["lon"] * w
    return lat / wsum, lon / wsum
