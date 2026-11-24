"""The one gap filler.

Every place where matched geometry is missing (unmatched fixes, a matching
break, a mode change, a stay boundary, a data gap) comes here with the two
anchor points around it and the raw fixes in between. The filler tries a
route, then a route through the raw fixes, and only then keeps the raw fixes
as a polyline. A straight jump is used only when there are no fixes between.
"""
from __future__ import annotations

from typing import Any

from . import valhalla
from .geo import dist_point_segment, haversine

PROFILE = {"walk": "pedestrian", "cycle": "bicycle", "drive": "auto"}
VMAX_KMH = {"walk": 16.0, "cycle": 50.0, "drive": 200.0, "train": 260.0}
# Typical average speeds. When the elapsed time across a gap is long enough
# that a detour at this pace fits, a longer route is believable.
TYPICAL_KMH = {"walk": 5.0, "cycle": 18.0, "drive": 60.0, "train": 100.0}
JOIN_M = 25.0         # shorter seams are drawn directly
# Walks and rides can be off-road (MTB trails, hill paths). A route there is
# only believed when it stays this close to the recorded fixes; otherwise the
# original GPS is kept rather than snapping to the wrong path.
TRAIL_MODES = ("walk", "cycle")
TRAIL_DEV_M = 30.0
# Road cycling is matched like driving; only off-road rides get the walk rule.
TRAIL_DEV_BY_MODE = {"walk": 30.0, "cycle": 55.0}
MAX_THROUGH = 12
END_SNAP_M = 150.0


def _length(coords: list[tuple[float, float]]) -> float:
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def _near_line(p: dict[str, Any], shape: list[tuple[float, float]]) -> float:
    best = 1e18
    for a, b in zip(shape, shape[1:]):
        d = dist_point_segment(p["lat"], p["lon"], a[0], a[1], b[0], b[1])
        if d < best:
            best = d
    return best


def max_deviation(line: list[tuple[float, float]], ref: list[tuple[float, float]]) -> float:
    """Largest distance from `line` (its vertices and edge midpoints) to `ref`."""
    if len(ref) < 2:
        return 0.0 if not ref else max(haversine(p[0], p[1], ref[0][0], ref[0][1]) for p in line)
    pts = list(line)
    pts += [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(line, line[1:])]
    worst = 0.0
    for p in pts:
        best = min(dist_point_segment(p[0], p[1], a[0], a[1], b[0], b[1]) for a, b in zip(ref, ref[1:]))
        worst = max(worst, best)
    return worst


def _plausible(shape: list[tuple[float, float]], a: dict[str, Any], b: dict[str, Any],
               mids: list[dict[str, Any]], vmax: float, typical: float, raw_path: float,
               chord: float, check_mids: bool, trail: bool = False, mode: str = "walk") -> str:
    """Empty string when the route is acceptable, else the reason it is not."""
    if len(shape) < 2:
        return "no_shape"
    if haversine(a["lat"], a["lon"], shape[0][0], shape[0][1]) > END_SNAP_M or \
            haversine(b["lat"], b["lon"], shape[-1][0], shape[-1][1]) > END_SNAP_M:
        return "ends_far"
    length = _length(shape)
    dt = b["timestamp"] - a["timestamp"]
    limit = max(1.3 * raw_path, 1.5 * chord + 150.0)
    if dt > 0 and length / dt * 3.6 <= 1.5 * typical:
        limit = max(limit, 2.5 * chord + 300.0)
    if length > limit:
        return "too_long"
    if length < 0.6 * chord:
        return "too_short"
    if dt > 0 and length / dt * 3.6 > vmax * 1.3 + 10.0:
        return "too_fast"
    if trail:
        ref = [(p["lat"], p["lon"]) for p in (a, *mids, b)]
        if mids:
            if max_deviation(shape, ref) > TRAIL_DEV_BY_MODE.get(mode, TRAIL_DEV_M) + max(min(p.get("accuracy") or 15.0, 30.0) for p in mids):
                return "off_trail"
        elif length > (2.0 * chord + 40.0 if chord < 150.0 else 1.25 * chord + 30.0):
            # A gap with no fixes: a short one may go round a corner; a long
            # one is only believable as a nearly direct route.
            return "off_trail"
    if check_mids and mids:
        close = sum(1 for p in mids if _near_line(p, shape) <= max(60.0, 1.5 * (p.get("accuracy") or 0.0)))
        if close < 0.6 * len(mids):
            return "misses_fixes"
    return ""


def _through(mids: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(mids) <= MAX_THROUGH:
        return mids
    step = len(mids) / MAX_THROUGH
    return [mids[int(k * step)] for k in range(MAX_THROUGH)]


SHORT_GAP_M = 1500.0   # below this a footpath route is an acceptable seam for any mode


def fill(a: dict[str, Any], mids: list[dict[str, Any]], b: dict[str, Any], mode: str,
         strength: str = "fill", vmax: float | None = None) -> dict[str, Any]:
    """Geometry from a to b. a/b carry lat, lon, timestamp (snapped positions).

    Returns {"kind": filled|raw|straight|join, "coords": [(lat, lon), ...]} and,
    when no route was accepted, "why" with the rejection reasons.
    """
    pts = [a, *mids, b]
    raw_path = _length([(p["lat"], p["lon"]) for p in pts])
    chord = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if chord < JOIN_M and raw_path < 2 * JOIN_M:
        return {"kind": "join", "coords": [(a["lat"], a["lon"]), (b["lat"], b["lon"])]}
    vmax = vmax if vmax is not None else VMAX_KMH.get(mode, 200.0)
    typical = TYPICAL_KMH.get(mode, 30.0)
    profile = PROFILE.get(mode)
    profiles = [profile] if profile else []
    # One-way streets and anchors on the far carriageway make short car/bike
    # routes loop round the block. A walking route follows the same streets
    # without those restrictions, so try it before giving up on a short gap.
    if profile and profile != "pedestrian" and chord <= SHORT_GAP_M:
        profiles.append("pedestrian")
    attempts: list[tuple[str, list[dict[str, Any]], bool]] = []
    if not (strength == "strict" and mids):
        for prof in profiles:
            attempts.append((prof, [a, b], True))
            if mids and strength != "strict":
                attempts.append((prof, [a, *_through(mids), b], False))
    why = [] if attempts else ["strict" if profile else "no_profile"]
    for prof, locs, check in attempts:
        r = valhalla.route(locs, prof)
        if not r:
            why.append("no_route")
            continue
        shape = r["shape"]
        bad = _plausible(shape, a, b, mids, vmax, typical, raw_path, chord, check, mode in TRAIL_MODES, mode)
        if not bad:
            coords = [(a["lat"], a["lon"]), *shape, (b["lat"], b["lon"])]
            return {"kind": "filled", "coords": coords}
        why.append(bad)
    kind = "raw" if mids else "straight"
    return {"kind": kind, "why": why,
            "coords": [(p["lat"], p["lon"]) for p in pts] if mids else [(a["lat"], a["lon"]), (b["lat"], b["lon"])]}
