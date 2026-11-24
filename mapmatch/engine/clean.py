"""Remove fixes that must not steer matching.

Nothing is deleted from the record: dropped fixes are returned with a reason
so callers can show them. Matching only sees the kept fixes.
"""
from __future__ import annotations

from typing import Any

from .geo import haversine

MAX_ACCURACY_M = 150.0
TELEPORT_KMH = 400.0
ISOLATED_S = 30 * 86400  # a fix a month away from both neighbours is a bad clock
MIN_TS = 946684800       # 2000-01-01


def _kmh(a: dict[str, Any], b: dict[str, Any]) -> float:
    dt = abs(b["timestamp"] - a["timestamp"])
    d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if dt <= 0:
        return 0.0 if d < 1 else 1e9
    return d / dt * 3.6


def clean(points: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (kept, dropped). Kept fixes are sorted copies; dropped carry `_drop`."""
    pts = sorted((dict(p) for p in points), key=lambda p: (int(p["timestamp"]), p["lat"], p["lon"]))
    dropped: list[dict[str, Any]] = []

    def drop(p: dict[str, Any], why: str) -> None:
        p["_drop"] = why
        dropped.append(p)

    # Exact duplicates and same-second duplicates: keep the most accurate.
    out: list[dict[str, Any]] = []
    for p in pts:
        p["timestamp"] = int(p["timestamp"])
        if p["timestamp"] < MIN_TS:
            drop(p, "bad_time")
            continue
        acc = p.get("accuracy")
        if acc is not None and acc > MAX_ACCURACY_M:
            drop(p, "low_accuracy")
            continue
        if out and out[-1]["timestamp"] == p["timestamp"]:
            q = out[-1]
            if (p.get("accuracy") or 99.0) < (q.get("accuracy") or 99.0):
                out[-1] = p
                drop(q, "duplicate")
            else:
                drop(p, "duplicate")
            continue
        out.append(p)

    # A fix far in time from both neighbours is a bad clock, not a trip.
    kept: list[dict[str, Any]] = []
    for i, p in enumerate(out):
        before = p["timestamp"] - out[i - 1]["timestamp"] if i else None
        after = out[i + 1]["timestamp"] - p["timestamp"] if i + 1 < len(out) else None
        if len(out) > 1 and (before is None or before > ISOLATED_S) and (after is None or after > ISOLATED_S):
            drop(p, "bad_time")
            continue
        kept.append(p)
    out = kept

    # Spikes: one fix that sticks out from an otherwise straight-ish path.
    changed = True
    while changed:
        changed = False
        kept = []
        for i, b in enumerate(out):
            if 0 < i < len(out) - 1:
                a, c = kept[-1] if kept else out[i - 1], out[i + 1]
                d1 = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                d2 = haversine(b["lat"], b["lon"], c["lat"], c["lon"])
                d3 = haversine(a["lat"], a["lon"], c["lat"], c["lon"])
                spike = d1 > 150 and d2 > 150 and d1 + d2 > 2.0 * d3 + 500
                tele = _kmh(a, b) > TELEPORT_KMH and _kmh(b, c) > TELEPORT_KMH and _kmh(a, c) < TELEPORT_KMH
                if spike or tele:
                    drop(b, "spike")
                    changed = True
                    continue
            kept.append(b)
        out = kept

    # Teleports: a pair with an impossible speed. Drop whichever fix, once
    # removed, leaves its neighbours consistent. A short excursion (up to
    # three fixes away and back) is dropped whole. If it is still ambiguous,
    # keep both; trip and leg splitting break the line there instead.
    kept = []
    i = 0
    while i < len(out):
        p = out[i]
        if kept and _kmh(kept[-1], p) > TELEPORT_KMH and \
                haversine(kept[-1]["lat"], kept[-1]["lon"], p["lat"], p["lon"]) > 1000:
            a = kept[-1]
            prev = kept[-2] if len(kept) > 1 else None
            nxt = out[i + 1] if i + 1 < len(out) else None
            a_bad = prev is not None and _kmh(prev, p) < TELEPORT_KMH
            p_bad = nxt is not None and _kmh(a, nxt) < TELEPORT_KMH
            if a_bad and not p_bad:
                drop(kept.pop(), "teleport")
                continue
            if p_bad and not a_bad:
                drop(p, "teleport")
                i += 1
                continue
            back = next((k for k in range(i + 1, min(len(out), i + 4))
                         if _kmh(a, out[k]) < TELEPORT_KMH and
                         haversine(a["lat"], a["lon"], out[k]["lat"], out[k]["lon"]) <
                         haversine(a["lat"], a["lon"], p["lat"], p["lon"]) / 2), None)
            if back is not None:
                for k in range(i, back):
                    drop(out[k], "teleport")
                i = back
                continue
        kept.append(p)
        i += 1
    for k, p in enumerate(kept):
        p["_i"] = k
    dropped.sort(key=lambda p: p["timestamp"])
    return kept, dropped
