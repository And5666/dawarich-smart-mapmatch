"""Stay detection: runs of fixes that remain within a small radius."""
from __future__ import annotations

from typing import Any

from .geo import haversine

STAY_RADIUS_M = 50.0
STAY_MIN_S = 180


def find_stays(fixes: list[dict[str, Any]], radius: float = STAY_RADIUS_M,
               min_s: int = STAY_MIN_S) -> list[tuple[int, int]]:
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
        if j - i >= 2 and fixes[j - 1]["timestamp"] - fixes[i]["timestamp"] >= min_s:
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def centroid(fixes: list[dict[str, Any]]) -> tuple[float, float]:
    """Accuracy-weighted centre of a stay."""
    wsum = lat = lon = 0.0
    for p in fixes:
        w = 1.0 / max(5.0, p.get("accuracy") or 20.0) ** 2
        wsum += w
        lat += p["lat"] * w
        lon += p["lon"] * w
    return lat / wsum, lon / wsum
