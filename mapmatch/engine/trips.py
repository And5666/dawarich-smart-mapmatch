"""Split a whole point stream into trips.

Trips are the movement between long stays and long gaps. Short stops (a shop,
a red light, a platform) stay inside the trip. Clusters of jitter at home are
not trips.
"""
from __future__ import annotations

from typing import Any

from .clean import TELEPORT_KMH, clean
from .geo import haversine
from .stays import find_stays

TRIP_GAP_S = 1800         # no fix for this long ends a trip (Dawarich default: 30 min)
TRIP_STAY_S = 1800        # a stay at least this long ends a trip
MIN_TRIP_EXTENT_M = 250.0  # trips that never leave this radius are jitter
MIN_TRIP_FIXES = 3


def _extent(fixes: list[dict[str, Any]]) -> float:
    a = fixes[0]
    return max(haversine(a["lat"], a["lon"], p["lat"], p["lon"]) for p in fixes)


def split_trips(points: list[dict[str, Any]], gap_s: int = TRIP_GAP_S,
                stay_s: int = TRIP_STAY_S) -> list[list[dict[str, Any]]]:
    """Return trips as lists of the original (cleaned) fixes, in time order."""
    fixes, _dropped = clean(points)
    if not fixes:
        return []
    in_stay = [False] * len(fixes)
    for a, b in find_stays(fixes):
        if fixes[b - 1]["timestamp"] - fixes[a]["timestamp"] >= stay_s:
            for k in range(a, b):
                in_stay[k] = True

    trips: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []

    def close() -> None:
        if len(cur) >= MIN_TRIP_FIXES and _extent(cur) >= MIN_TRIP_EXTENT_M:
            trips.append(list(cur))
        cur.clear()

    for k, p in enumerate(fixes):
        if in_stay[k]:
            close()
            continue
        if cur:
            q = cur[-1]
            dt = p["timestamp"] - q["timestamp"]
            d = haversine(q["lat"], q["lon"], p["lat"], p["lon"])
            if dt > gap_s or (dt > 0 and d / dt * 3.6 > TELEPORT_KMH and d > 1000):
                close()
        cur.append(p)
    close()
    for t in trips:
        for p in t:
            p.pop("_i", None)
    return trips
