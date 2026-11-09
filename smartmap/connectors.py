"""Fill the visual gaps between state parts with matched connectors."""
from __future__ import annotations

from typing import Any

from .geo import cumulative, haversine, point_at
from .valhalla import route_between

MIN_GAP = 2.0


def _profile(a: dict[str, Any], b: dict[str, Any], gap: float) -> str:
    modes = {a.get("mode"), b.get("mode")}
    if "driving" in modes or "train" in modes:
        return "auto"
    if "cycling" in modes:
        return "bicycle"
    if "walking" in modes:
        return "pedestrian"
    return "auto"


def _route_points(a: dict[str, Any], b: dict[str, Any], profile: str) -> tuple[list[list[float]], str]:
    p = a["points"][-1]
    q = b["points"][0]
    gap = haversine(p[0], p[1], q[0], q[1])
    r = route_between({"lat": p[0], "lon": p[1]}, {"lat": q[0], "lon": q[1]}, profile)
    if not r or len(r.get("shape") or []) < 2:
        return [p, q], "connector"
    if r["length_m"] > max(500.0, gap * 4.0 + 200.0):
        return [p, q], "connector"
    poly = r["shape"]
    pts: list[list[float]] = [p]
    for lat, lon in poly:
        if haversine(pts[-1][0], pts[-1][1], lat, lon) > 2.0:
            pts.append([lat, lon])
    if haversine(pts[-1][0], pts[-1][1], q[0], q[1]) > 2.0:
        pts.append(q)
    return pts, f"connector:{profile}"


def _interpolate(pts: list[list[float]], t0: int, t1: int, accuracy: Any) -> list[dict[str, Any]]:
    if not pts:
        return []
    if t1 <= t0:
        t1 = t0 + 1
    if len(pts) == 1:
        return [{"lat": pts[0][0], "lon": pts[0][1], "timestamp": int(t0),
                 "accuracy": accuracy, "inserted": True}]
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    total = cum[-1]
    out: list[dict[str, Any]] = []
    for i, (lat, lon) in enumerate(pts):
        ts = t0 if i == 0 else t1 if i == len(pts) - 1 else int(round(t0 + (t1 - t0) * cum[i] / total))
        if out and ts <= out[-1]["timestamp"]:
            if i == len(pts) - 1:
                ts = out[-1]["timestamp"] + 1
            else:
                continue
        out.append({"lat": lat, "lon": lon, "timestamp": int(ts),
                    "accuracy": accuracy, "inserted": True})
    return out


def add_connectors(track: dict[str, Any]) -> dict[int, int]:
    """Insert a connector part after every adjacent pair with a real gap.

    Returns a mapping from old part index to new part index. That lets an
    existing review keep its votes when connectors are added later.
    """
    old = track.get("parts") or []
    new: list[dict[str, Any]] = []
    mapping: dict[int, int] = {}
    for i, part in enumerate(old):
        mapping[i] = len(new)
        new.append(part)
        if i + 1 >= len(old):
            continue
        nxt = old[i + 1]
        if not part.get("points") or not nxt.get("points"):
            continue
        a = part["points"][-1]
        b = nxt["points"][0]
        gap = haversine(a[0], a[1], b[0], b[1])
        if gap < MIN_GAP:
            continue
        profile = _profile(part, nxt, gap)
        pts, engine = _route_points(part, nxt, profile)
        if len(pts) < 2:
            continue
        t0 = int(part.get("end") or 0)
        t1 = int(nxt.get("start") or 0)
        cpts = _interpolate(pts, t0, t1, part.get("accuracy"))
        if len(cpts) < 2:
            continue
        dist = sum(haversine(x["lat"], x["lon"], y["lat"], y["lon"])
                   for x, y in zip(cpts, cpts[1:])) / 1000.0
        new.append({
            "start": int(cpts[0]["timestamp"]), "end": int(cpts[-1]["timestamp"]),
            "mode": "connector", "engine": engine,
            "label": f"connector · {'matched:' + profile if engine.startswith('connector:') else 'straight'}",
            "n_in": 0, "n_out": len(cpts), "distance": round(dist, 3),
            "points": [[p["lat"], p["lon"]] for p in cpts],
        })
    for i, p in enumerate(new):
        p["i"] = i
    track["parts"] = new
    return mapping
