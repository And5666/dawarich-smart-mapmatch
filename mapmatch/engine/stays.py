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


# A place visited with the phone logging once a minute or less: the fixes
# wander 60-100 m (car park, tent, tap) for most of an hour. That is outside
# the stay radius, so it used to read as a 0.4 km/h "walk" and got a walking
# route drawn for it. It is a stop.
DWELL_RADIUS_M = 100.0
DWELL_MIN_S = 900
DWELL_MAX_KMH = 1.5     # path length over elapsed time: slower than any walk


def find_dwells(fixes: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """[start, end) runs that stay within DWELL_RADIUS_M of their centre for
    DWELL_MIN_S or more while covering almost no ground. Only for labelling
    modes inside a trip (modes.label uses it at the trip's two ends); trips
    are still split on find_stays alone."""
    runs: list[tuple[int, int]] = []
    n = len(fixes)
    i = 0
    while i < n:
        lat, lon = fixes[i]["lat"], fixes[i]["lon"]
        k = 1
        j = i + 1
        path = 0.0
        while j < n:
            p = fixes[j]
            if haversine(lat, lon, p["lat"], p["lon"]) > DWELL_RADIUS_M:
                break
            path += haversine(fixes[j - 1]["lat"], fixes[j - 1]["lon"], p["lat"], p["lon"])
            lat = (lat * k + p["lat"]) / (k + 1)
            lon = (lon * k + p["lon"]) / (k + 1)
            k += 1
            j += 1
        dur = fixes[j - 1]["timestamp"] - fixes[i]["timestamp"]
        if j - i >= 3 and dur >= DWELL_MIN_S and path / dur * 3.6 <= DWELL_MAX_KMH and not drifts(fixes[i:j]):
            runs.append((i, j))
            i = j
            continue
        i += 1
    return runs


MILL_MIN_S = 300
MILL_MIN_FIXES = 8
MILL_OUT_S = 20


def find_milling(fixes: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """[start, end) runs of five minutes or more inside DWELL_RADIUS_M of their own
    centre that are not steady progress: round a shop, a market, a station
    concourse. Indoors the positions scatter by tens of metres, so the path is
    long and fast and neither find_stays (50 m) nor find_dwells (almost no
    ground covered) sees a stop. Nobody travelling stays inside 100 m for five
    minutes; a slow scramble does, and is told apart by drifting one way."""
    runs: list[tuple[int, int]] = []
    n = len(fixes)
    i = 0
    while i < n:
        lat, lon = fixes[i]["lat"], fixes[i]["lon"]
        k = 1
        j = i + 1
        end = j                     # one past the last fix inside the radius
        while j < n:
            p = fixes[j]
            if haversine(lat, lon, p["lat"], p["lon"]) > DWELL_RADIUS_M:
                # A stray fix or two does not end it (indoors they fly off and come
                # back); twenty seconds outside does.
                if p["timestamp"] - fixes[end - 1]["timestamp"] > MILL_OUT_S:
                    break
                j += 1
                continue
            lat = (lat * k + p["lat"]) / (k + 1)
            lon = (lon * k + p["lon"]) / (k + 1)
            k += 1
            j += 1
            end = j
        j = end
        if k >= MILL_MIN_FIXES and fixes[j - 1]["timestamp"] - fixes[i]["timestamp"] >= MILL_MIN_S \
                and not drifts(fixes[i:j]):
            runs.append((i, j))
            i = j
            continue
        i += 1
    return runs
