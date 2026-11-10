"""Turn the review parts back into one point stream.

The review is the authoritative geometry: it contains routed connectors and
repaired road gaps. The raw track `points` list does not. Pushing the review
stream keeps Dawarich and the review visually identical.
"""
from __future__ import annotations

import math
from typing import Any

from .geo import haversine


def from_parts(track: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    last_ts = -1
    for part in track.get("parts") or []:
        pts = part.get("points") or []
        if not pts:
            continue
        t0 = int(part.get("start") or 0)
        t1 = int(part.get("end") or 0)
        if t1 <= t0:
            t1 = t0 + 1
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
        total = cum[-1]
        for i, (lat, lon) in enumerate(pts):
            if i == 0:
                ts = t0
            elif i == len(pts) - 1:
                ts = t1
            elif total > 0:
                ts = int(round(t0 + (t1 - t0) * cum[i] / total))
            else:
                ts = t0 + i
            if last_ts >= 0:
                d = haversine(out[-1]["lat"], out[-1]["lon"], lat, lon)
                # 50 m/s is 180 km/h. Give every segment enough integer seconds
                # to stay under that, and never emit two points in one second.
                ts = max(int(ts), last_ts + max(1, math.ceil(d / 50.0)))
            if out and out[-1]["lat"] == lat and out[-1]["lon"] == lon and out[-1]["timestamp"] == ts:
                continue
            out.append({"lat": float(lat), "lon": float(lon), "timestamp": int(ts),
                        "accuracy": 8.0, "inserted": True})
            last_ts = int(ts)
    return out
