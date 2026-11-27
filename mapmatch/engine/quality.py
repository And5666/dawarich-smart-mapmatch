"""Quality metrics for a matched trip (ported from smartmap/metrics.py)."""
from __future__ import annotations

import bisect
import math
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


# ---------------------------------------------------------------- area between the two lines
SNAPPED = ("matched", "filled", "join")   # pieces that are not the GPS itself
AREA_STEP_M = 10.0


def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    return lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110540.0


def _to_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    s2 = dx * dx + dy * dy
    t = 0.0 if s2 < 1e-9 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / s2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def between(fixes: list[dict[str, Any]], segments: list[dict[str, Any]]) -> dict[str, Any]:
    """The area enclosed between the GPS fixes joined up and the line drawn
    for them: how far the drawn route strays from
    what was recorded, including between two fixes, where the per-fix
    distances (p50, p95) see nothing.

    Each snapped piece is closed against the fixes inside it (joined at the
    piece's own two ends), and the area is summed hop by hop: the distance
    from the drawn line to the straight hop between two fixes, integrated
    along the hop. That is the enclosed area, and unlike a signed polygon
    area it does not cancel out when the two lines cross. Where two fixes
    are at the same spot (a loop) it is the area of the loop. Pieces that
    are the GPS itself (recorded, raw, a straight hole, a stay) enclose
    nothing and are left out.

    Returns {} when nothing was snapped, else:
      area_m2      the whole trip
      gap_m        area / snapped length: the average distance apart
      bulge_*      the one hop enclosing the most area (m2, t0, t1, max_m)
      detour_*     the one hop whose line is longest for the straight hop it
                   stands for (line_m, hop_m, t0, t1): an invented loop or hook
    Big numbers are not proof of a wrong match: a motorway logged every 2 km
    bends well away from the straight hops. They say where to look first.
    """
    fx = sorted(fixes, key=lambda p: p["timestamp"])
    if len(fx) < 2:
        return {}
    ts = [int(p["timestamp"]) for p in fx]
    lat0 = fx[0]["lat"]
    area = length = 0.0
    bulge: tuple[float, int, int, float] | None = None
    detour: tuple[float, float, float, int, int] | None = None
    for s in segments:
        for pc in s["pieces"]:
            c = pc["coords"]
            if pc["kind"] not in SNAPPED or len(c) < 2 or c[-1][2] <= c[0][2]:
                continue
            pts = [_xy(x[0], x[1], lat0) for x in c]
            tt = [x[2] for x in c]

            def at(t: float) -> tuple[float, float]:
                k = bisect.bisect_left(tt, t)
                if k <= 0:
                    return pts[0]
                if k >= len(tt):
                    return pts[-1]
                f = 0.0 if tt[k] == tt[k - 1] else (t - tt[k - 1]) / (tt[k] - tt[k - 1])
                return pts[k - 1][0] + f * (pts[k][0] - pts[k - 1][0]), pts[k - 1][1] + f * (pts[k][1] - pts[k - 1][1])

            inside = fx[bisect.bisect_right(ts, tt[0]):bisect.bisect_left(ts, tt[-1])]
            ref = [(tt[0], pts[0])] + [(int(p["timestamp"]), _xy(p["lat"], p["lon"], lat0)) for p in inside] + [(tt[-1], pts[-1])]
            for (ta, a), (tb, b) in zip(ref, ref[1:]):
                if tb <= ta:
                    continue
                path = [at(ta)] + pts[bisect.bisect_right(tt, ta):bisect.bisect_left(tt, tb)] + [at(tb)]
                hop = math.hypot(b[0] - a[0], b[1] - a[1])
                hx, hy = ((b[0] - a[0]) / hop, (b[1] - a[1]) / hop) if hop >= 5.0 else (0.0, 0.0)
                ar = ln = far = 0.0
                for u, v in zip(path, path[1:]):
                    d = math.hypot(v[0] - u[0], v[1] - u[1])
                    if d < 0.5:
                        continue
                    along = abs((v[0] - u[0]) * hx + (v[1] - u[1]) * hy)   # this step's share of the hop
                    n = max(1, int(d // AREA_STEP_M))
                    for k in range(n):
                        f = (k + 0.5) / n
                        dd = _to_segment((u[0] + f * (v[0] - u[0]), u[1] + f * (v[1] - u[1])), a, b)
                        ar += dd * along / n
                        far = max(far, dd)
                    ln += d
                if ln < 1.0:
                    continue
                if hop < 5.0:
                    # Out and back to the same spot: the area of the loop itself.
                    ar = abs(sum(u[0] * v[1] - v[0] * u[1] for u, v in zip(path, path[1:] + path[:1]))) / 2.0
                area += ar
                length += ln
                if bulge is None or ar > bulge[0]:
                    bulge = (ar, int(ta), int(tb), far)
                score = (ln - hop) * min(3.0, ln / max(hop, 30.0))
                if detour is None or score > detour[0]:
                    detour = (score, ln, hop, int(ta), int(tb))
    if length < 1.0 or bulge is None or detour is None:
        return {}
    return {
        "area_m2": round(area), "gap_m": round(area / length, 1),
        "bulge_m2": round(bulge[0]), "bulge_t0": bulge[1], "bulge_t1": bulge[2], "bulge_max_m": round(bulge[3]),
        "detour_line_m": round(detour[1]), "detour_hop_m": round(detour[2]), "detour_t0": detour[3], "detour_t1": detour[4],
    }
