"""Quality metrics for the generated line."""
from __future__ import annotations

from .geo import bearing, dist_point_segment, haversine


def path_length(points):
    if not points:
        return 0.0
    if isinstance(points[0], dict):
        seq = [(p["lat"], p["lon"]) for p in points]
    else:
        seq = [(p[0], p[1]) for p in points]
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(seq, seq[1:]))


def _nearest_error(p, gen):
    import bisect
    ts = [g["timestamp"] for g in gen]
    lo = max(0, bisect.bisect_left(ts, p["timestamp"] - 90) - 1)
    hi = min(len(gen), bisect.bisect_right(ts, p["timestamp"] + 90) + 1)
    near = gen[lo:hi]
    if not near:
        return 1e9
    if len(near) == 1:
        return haversine(p["lat"], p["lon"], near[0]["lat"], near[0]["lon"])
    return min(dist_point_segment(p["lat"], p["lon"], a["lat"], a["lon"], b["lat"], b["lon"])
               for a, b in zip(near, near[1:]))


def errors(raw, gen):
    if not gen:
        return [1e9] * len(raw)
    return [_nearest_error(p, gen) for p in raw]


def count_duplicate_timestamps(points):
    seen = set()
    dup = 0
    for p in points:
        ts = int(p["timestamp"])
        if ts in seen:
            dup += 1
        seen.add(ts)
    return dup


def count_spikes(points, max_kmh=180.0):
    n = 0
    for a, b in zip(points, points[1:]):
        dt = max(1, int(b["timestamp"]) - int(a["timestamp"]))
        if haversine(a["lat"], a["lon"], b["lat"], b["lon"]) / dt * 3.6 > max_kmh:
            n += 1
    return n


def count_zigzags(points, min_m=12.0):
    n = 0
    for a, b, c in zip(points, points[1:], points[2:]):
        d1 = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        d2 = haversine(b["lat"], b["lon"], c["lat"], c["lon"])
        if d1 < min_m or d2 < min_m:
            continue
        p1 = bearing(a["lat"], a["lon"], b["lat"], b["lon"])
        p2 = bearing(b["lat"], b["lon"], c["lat"], c["lon"])
        turn = abs((p1 - p2 + 180) % 360 - 180)
        if turn > 150:
            n += 1
    return n


def track_metrics(track):
    gen = track.get("points") or []
    raw_points = track.get("_raw_points") or []
    errs = sorted(errors(raw_points, gen)) if raw_points and gen else []
    p50 = errs[len(errs) // 2] if errs else 0.0
    p95 = errs[min(len(errs) - 1, int(len(errs) * 0.95))] if errs else 0.0
    raw_km = track["km"]
    gen_km = track["distance"]
    return {
        "id": track["id"],
        "raw_km": raw_km,
        "gen_km": gen_km,
        "ratio": round(gen_km / max(1e-6, raw_km), 3),
        "nraw": track["nraw"],
        "ngen": track["ngen"],
        "p50": round(p50, 1),
        "p95": round(p95, 1),
        "duplicates": count_duplicate_timestamps(gen),
        "spikes": count_spikes(gen),
        "zigzags": count_zigzags(gen),
        "parts": len(track["parts"]),
        "matched_km": track.get("matched_km", 0),
        "raw_part_km": track.get("raw_km", 0),
    }
