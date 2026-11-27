"""The one gap filler.

Every place where matched geometry is missing (unmatched fixes, a matching
break, a mode change, a stay boundary, a data gap) comes here with the two
anchor points around it and the raw fixes in between. The filler tries a
route, then a route through the raw fixes, and only then keeps the raw fixes
as a polyline. A straight jump is used only when there are no fixes between.
"""
from __future__ import annotations

from typing import Any

from . import rail, railnet, valhalla
from .geo import cumulative, dist_point_segment, haversine, point_at, project_arc

PROFILE = {"walk": "pedestrian", "cycle": "bicycle", "drive": "auto"}
VMAX_KMH = {"walk": 16.0, "cycle": 50.0, "drive": 200.0, "train": 260.0}
# Typical average speeds. When the elapsed time across a gap is long enough
# that a detour at this pace fits, a longer route is believable.
TYPICAL_KMH = {"walk": 5.0, "cycle": 18.0, "drive": 60.0, "train": 100.0}
JOIN_M = 25.0         # shorter seams are drawn directly
LONG_GAP_M = 1000.0
LONG_WALK_KMH = 9.0
RAIL_FILL_MIN_M = 300.0   # shorter train holes are platform noise
WAY_ROUND_EXTRA_M = 300.0   # a route this much longer than the hop it fills
WAY_ROUND_RATIO = 2.5       # and this many times as long
WAY_ROUND_JOIN_M = 300.0    # a hop up to this long is drawn straight when every route goes round
HOLE_M = 1000.0             # two fixes this far apart with nothing between: a hole, not a hop
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
               chord: float, check_mids: bool, trail: bool = False, mode: str = "walk",
               force: bool = False) -> str:
    """Empty string when the route is acceptable, else the reason it is not.

    force=True is an explicit "route it anyway" from the editor: it waves through
    the two judgements that depend on the timestamps being right (too_long and
    too_fast) but still refuses a route that does not join the two ends or is
    shorter than the distance between them — those are broken routes, not fast ones.
    """
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
    if not force and length > limit:
        return "too_long"
    if length < 0.6 * chord:
        return "too_short"
    if not force and dt > 0 and length / dt * 3.6 > vmax * 1.3 + 10.0:
        return "too_fast"
    # The slack above is for short seams, where a few seconds either way
    # change the speed a lot. Over a kilometre it let an 8 km "walk" at
    # 23 km/h through (a train with no fixes on the way, drawn along the
    # road). Nobody walks a kilometre and more at over LONG_WALK_KMH: leave
    # the hole as it is, for the mode to be set by hand.
    if not force and mode == "walk" and length > LONG_GAP_M and dt > 0 and length / dt * 3.6 > LONG_WALK_KMH:
        return "too_fast"
    if trail:
        ref = [(p["lat"], p["lon"]) for p in (a, *mids, b)]
        if mids:
            if max_deviation(shape, ref) > TRAIL_DEV_BY_MODE.get(mode, TRAIL_DEV_M) + max(min(p.get("accuracy") or 15.0, 30.0) for p in mids):
                return "off_trail"
        elif not force and length > (2.0 * chord + 40.0 if chord < 150.0 else 1.25 * chord + 30.0):
            # A gap with no fixes: a short one may go round a corner; a long
            # one is only believable as a nearly direct route. Asked for by
            # hand ("route it anyway") there is no fix to contradict the
            # route, and a straight line through buildings is surely wrong.
            return "off_trail"
    if check_mids and mids:
        close = sum(1 for p in mids if _near_line(p, shape) <= max(60.0, 1.5 * (p.get("accuracy") or 0.0)))
        if close < 0.6 * len(mids):
            return "misses_fixes"
    return ""


def rail_path(a: dict[str, Any], b: dict[str, Any]) -> list[tuple[float, float]] | None:
    """The railway from a to b: along the rails of an OpenStreetMap network
    where there is one (the real track, and it has the Underground and trams),
    else along a GB timetable shape (coarse: straight between stations)."""
    try:
        shape = railnet.between(a["lat"], a["lon"], b["lat"], b["lon"])
    except (OSError, ValueError):
        shape = None
    if shape:
        return shape
    try:
        return rail.between(a["lat"], a["lon"], b["lat"], b["lon"])
    except (OSError, ValueError):
        return None


def at_station(p: dict[str, Any], reach: float = 300.0) -> bool:
    try:
        if rail.station_distance(p["lat"], p["lon"]) <= reach:
            return True
    except (OSError, ValueError):
        pass
    try:
        return railnet.station_distance(p["lat"], p["lon"]) <= reach
    except (OSError, ValueError):
        return False


def _any_route(a: dict[str, Any], b: dict[str, Any], mode: str, chord: float,
               tried: set[Any]) -> tuple[str, list[tuple[float, float]]] | None:
    """(mode, shape) of a believable way from a to b in the time it took: a
    train when both ends are at stations and rails join them, else on foot, by
    bike or by car, whichever the pace allows, slowest first. None when
    nothing fits: the two fixes are then left unconnected."""
    dt = b["timestamp"] - a["timestamp"]
    if dt <= 0:
        return None
    kmh = chord / dt * 3.6
    if mode != "train" and LONG_WALK_KMH <= kmh <= VMAX_KMH["train"] and at_station(a) and at_station(b):
        shape = rail_path(a, b)
        if shape:
            length = _length(shape)
            if length <= 1.6 * chord + 500.0 and length / dt * 3.6 <= VMAX_KMH["train"] * 1.1:
                return "train", shape
    for m in ("walk", "cycle", "drive"):
        if PROFILE[m] in tried or kmh > VMAX_KMH[m] or (m == "walk" and kmh > LONG_WALK_KMH):
            continue
        r = valhalla.route([a, b], PROFILE[m])
        if not r:
            continue
        shape = r["shape"]
        if _plausible(shape, a, b, [], VMAX_KMH[m], TYPICAL_KMH[m], chord, chord, True, False, m):
            continue
        if _way_round(_length(shape), chord):
            continue
        return m, shape
    return None


def _way_round(length: float, chord: float) -> bool:
    return length - chord >= WAY_ROUND_EXTRA_M and length >= WAY_ROUND_RATIO * max(chord, 30.0)


def _straighten(shape: list[tuple[float, float]], pts: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """A route through the fixes of a gap can be right for most of the way and
    still go a long way round between two of them (past a car park entrance
    and back, round a block). Those legs, where the hop is short, are cut out
    and drawn straight; the rest of the route is kept."""
    if len(shape) < 2 or len(pts) < 3:
        return shape
    cum = cumulative(shape)
    arcs = [0.0]
    for p in pts[1:-1]:
        arc, _ = project_arc(shape, cum, p["lat"], p["lon"], arcs[-1], cum[-1])
        arcs.append(min(cum[-1], max(arc, arcs[-1])))
    arcs.append(cum[-1])
    out: list[tuple[float, float]] = []
    changed = False
    for k in range(len(pts) - 1):
        lo, hi = arcs[k], arcs[k + 1]
        hop = haversine(pts[k]["lat"], pts[k]["lon"], pts[k + 1]["lat"], pts[k + 1]["lon"])
        ends = [tuple(point_at(shape, cum, lo)[:2]), tuple(point_at(shape, cum, hi)[:2])]
        if hop <= WAY_ROUND_JOIN_M and _way_round(hi - lo, hop):
            leg = ends
            changed = True
        else:
            leg = [ends[0], *(shape[i] for i in range(len(shape)) if lo < cum[i] < hi), ends[1]]
        out += leg[1:] if out else leg
    return out if changed else shape


def _through(mids: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(mids) <= MAX_THROUGH:
        return mids
    step = len(mids) / MAX_THROUGH
    return [mids[int(k * step)] for k in range(MAX_THROUGH)]


SHORT_GAP_M = 1500.0   # below this a footpath route is an acceptable seam for any mode


def fill(a: dict[str, Any], mids: list[dict[str, Any]], b: dict[str, Any], mode: str,
         strength: str = "fill", vmax: float | None = None, force: bool = False) -> dict[str, Any]:
    """Geometry from a to b. a/b carry lat, lon, timestamp (snapped positions).

    Returns {"kind": filled|raw|straight|join, "coords": [(lat, lon), ...]} and,
    when no route was accepted, "why" with the rejection reasons.

    strength="strict" never routes a gap: the line runs between the recorded
    points (raw when there are fixes inside the gap, straight when the hole is
    empty). strength="fill" tries a route first, and keeps the recorded points
    when no route fits the timing.
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
    if strength != "strict":
        for prof in profiles:
            attempts.append((prof, [a, b], True))
            if mids:
                attempts.append((prof, [a, *_through(mids), b], False))
    why = [] if attempts else ["strict" if profile else "no_profile"]
    if mode == "train" and strength != "strict" and chord >= RAIL_FILL_MIN_M:
        # A train with nothing recorded on the way: along the railway that
        # joins the two ends, when one does and the timing fits a train.
        shape = rail_path(a, b)
        dt = b["timestamp"] - a["timestamp"]
        if shape:
            length = _length(shape)
            near = sum(1 for p in mids if _near_line(p, shape) <= 150.0)
            if length > 1.6 * chord + 500.0:
                why = ["too_long"]
            elif not force and dt > 0 and length / dt * 3.6 > vmax * 1.1:
                why = ["too_fast"]
            elif mids and near < 0.6 * len(mids):
                why = ["misses_fixes"]
            else:
                return {"kind": "filled", "coords": [(a["lat"], a["lon"]), *shape, (b["lat"], b["lon"])]}
        else:
            why = ["no_railway"]
    for prof, locs, check in attempts:
        r = valhalla.route(locs, prof)
        if not r:
            why.append("no_route")
            continue
        shape = r["shape"]
        bad = _plausible(shape, a, b, mids, vmax, typical, raw_path, chord, check, mode in TRAIL_MODES,
                         mode, force)
        if not bad and not mids and not force and _way_round(_length(shape), chord):
            # Two fixes a short hop apart, nothing recorded between them, and
            # a route several times the hop: an anchor on the far carriageway
            # or a one-way street sends the car round the block. Try the next
            # profile (walking follows the same streets either way round).
            bad = "way_round"
        if not bad:
            if mids and not force:
                shape = _straighten(shape, pts)
            coords = [(a["lat"], a["lon"]), *shape, (b["lat"], b["lon"])]
            return {"kind": "filled", "coords": coords}
        why.append(bad)
    if "way_round" in why and chord <= WAY_ROUND_JOIN_M:
        # No route goes nearly straight there. Over so short a hop the plain
        # line between the two fixes is nearer the truth than a loop.
        return {"kind": "join", "why": why, "coords": [(a["lat"], a["lon"]), (b["lat"], b["lon"])]}
    if not mids and strength != "strict" and chord >= HOLE_M:
        # Two fixes far apart, nothing between them, and no route in the mode
        # this stretch was given. Before leaving a hole, try every way the
        # ground could have been covered in that time.
        other = _any_route(a, b, mode, chord, {PROFILE.get(mode), *profiles})
        if other:
            return {"kind": "filled", "via": other[0], "why": why,
                    "coords": [(a["lat"], a["lon"]), *other[1], (b["lat"], b["lon"])]}
    kind = "raw" if mids else "straight"
    return {"kind": kind, "why": why,
            "coords": [(p["lat"], p["lon"]) for p in pts] if mids else [(a["lat"], a["lon"]), (b["lat"], b["lon"])]}
