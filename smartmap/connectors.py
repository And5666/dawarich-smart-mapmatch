"""Fill the visual gaps between state parts with matched connectors."""
from __future__ import annotations

from typing import Any

from .geo import cumulative, haversine, point_at
from .valhalla import route_between

MIN_GAP = 2.0


def _profile(a: dict[str, Any], b: dict[str, Any], gap: float) -> str | None:
    modes = {a.get("mode"), b.get("mode")}
    if "train" in modes:
        # There is no routed rail profile. Never turn a train seam into a
        # footpath or a road: show the straight link instead.
        return None
    if "driving" in modes:
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
    if profile is None:
        return [p, q], "connector"
    r = route_between({"lat": p[0], "lon": p[1]}, {"lat": q[0], "lon": q[1]}, profile)
    if not r or len(r.get("shape") or []) < 2 or float(r.get("length_m") or 0) <= 0:
        return [p, q], "connector"
    poly = r["shape"]
    # Valhalla can return a degenerate route for two points off the network:
    # a shape far from both endpoints and a zero summary length. Reject it.
    if haversine(p[0], p[1], poly[0][0], poly[0][1]) > 120.0 or             haversine(q[0], q[1], poly[-1][0], poly[-1][1]) > 120.0:
        return [p, q], "connector"
    poly_len = sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(poly, poly[1:]))
    # A connector is a seam, not a detour. If the routed line is more than
    # roughly twice the straight gap, show the honest straight link instead.
    if poly_len > max(120.0, gap * 2.0 + 80.0):
        return [p, q], "connector"
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



def _closest_index(points: list[list[float]], lat: float, lon: float) -> int:
    best = (1e30, 0)
    for i, (a, b) in enumerate(points):
        d = haversine(a, b, lat, lon)
        if d < best[0]:
            best = (d, i)
    return best[1]


def _line_len(points: list[list[float]]) -> float:
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))


def repair_internal_gaps(track: dict[str, Any]) -> int:
    """Replace straight interpolation across a long GPS dropout with a route.

    The core matcher only splits at very long gaps. A 3 km dropout inside a
    moving part was therefore drawn as a straight line. For road/walk/cycle
    parts, route between the fixes on either side of that dropout and splice
    the routed geometry into the part. Part indices and votes are unchanged.
    """
    raw = track.get("_raw_points") or []
    repaired = 0
    for part in track.get("parts") or []:
        mode = part.get("mode")
        # The reported holes are road holes. Do not turn a hiking or cycling
        # data gap into a routed road; connectors handle the state seams.
        if mode != "driving":
            continue
        seg = [p for p in raw if int(part["start"]) <= int(p["timestamp"]) <= int(part["end"])]
        if len(seg) < 2 or len(part.get("points") or []) < 4:
            continue
        profile = "auto"
        gaps = []
        for a, b in zip(seg, seg[1:]):
            dt = int(b["timestamp"]) - int(a["timestamp"])
            d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            if (dt > 600 and d > 300) or d > 1500:
                gaps.append((a, b, d))
        for a, b, d in reversed(gaps):
            i0 = _closest_index(part["points"], a["lat"], a["lon"])
            i1 = _closest_index(part["points"], b["lat"], b["lon"])
            if i1 <= i0 + 1:
                continue
            r = route_between({"lat": a["lat"], "lon": a["lon"]},
                              {"lat": b["lat"], "lon": b["lon"]}, profile)
            if not r or len(r.get("shape") or []) < 2:
                continue
            poly = r["shape"]
            if haversine(poly[0][0], poly[0][1], a["lat"], a["lon"]) > 150 or                     haversine(poly[-1][0], poly[-1][1], b["lat"], b["lon"]) > 150:
                continue
            length = _line_len(poly)
            if not (0.65 * d <= length <= 2.0 * d + 300):
                continue
            mid = [[lat, lon] for lat, lon in poly]
            part["points"] = part["points"][:i0 + 1] + mid[1:-1] + part["points"][i1:]
            part["distance"] = round(_line_len(part["points"]) / 1000.0, 3)
            part["n_out"] = len(part["points"])
            part["engine"] = f"repaired:{profile}"
            part["label"] = f"{mode} · repaired:{profile}"
            repaired += 1
    return repaired

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
