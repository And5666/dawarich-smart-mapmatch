"""Engine entry point: one trip of raw fixes in, segments and a stream out."""
from __future__ import annotations

from typing import Any

from . import assemble, valhalla
from .clean import clean
from .match import fit_score, match_segment
from .modes import label, runs, summary_speed
from .quality import deviations, path_km, percentile

ENGINE_VERSION = "2.9.1"
MODES = ("walk", "cycle", "drive", "train", "stay", "raw")
MOVING = ("walk", "cycle", "drive", "train")
NO_MAP_FRAC = 0.75   # share of sampled fixes with no map under them: the trip is abroad


def _choose_road(fixes: list[dict[str, Any]], mode: str) -> tuple[str, dict[str, Any]]:
    """Settle cycle-vs-drive by speed, and by fit where speed is ambiguous."""
    sp = summary_speed(fixes)
    if mode == "cycle" and sp["p90"] > 45:
        mode = "drive"
    if mode == "drive" and sp["p90"] <= 40 and sp["median"] <= 30:
        alts = ("drive", "cycle")
    elif mode == "cycle":
        alts = ("cycle", "drive")
    else:
        alts = (mode,)
    best = None
    for k, m in enumerate(alts):
        res = match_segment(fixes, m)
        sc = fit_score(fixes, res)
        if k == 0:
            sc += 0.03  # the speed-based label wins ties
        if best is None or sc > best[0]:
            best = (sc, m, res)
    return best[1], best[2]


def _rail_wins(fixes: list[dict[str, Any]], rail_res: dict[str, Any]) -> bool:
    """A train must fit the railway better than a road beside it."""
    got = [a for a in rail_res["anchors"] if a]
    cov = len(got) / max(1, len(fixes))
    if cov < 0.5:
        return False
    rail_med = sorted(a["dist"] for a in got)[len(got) // 2]
    road = match_segment(fixes, "drive")
    rgot = [a for a in road["anchors"] if a]
    if not rgot:
        # No road to compare with (no map here): only a journey that keeps to
        # the railway almost all the way is a train.
        return cov >= 0.8
    road_cov = len(rgot) / len(fixes)
    road_med = sorted(a["dist"] for a in rgot)[len(rgot) // 2]
    return not (road_cov > cov + 0.2 or road_med + 8.0 < rail_med)


def match_trip(points: list[dict[str, Any]], overrides: list[dict[str, Any]] | None = None,
               options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Match one trip.

    overrides: [{"t0", "t1", "mode"}] with mode in MODES; fixes in the range
    are forced to that mode. options: {"strength": "fill" | "strict"},
    {"keep_gaps": [[t0, t1], ...]} for holes that must stay as recorded, and
    {"force_route": bool, "force_gaps": [...]} for holes the user insists on
    routing even though the plausibility gate would refuse them,
    {"route_gaps": [...]} for holes to route (with the usual checks) in a trip
    that otherwise keeps its gaps, and
    {"snap_recorded": bool} to match detailed tracks instead of keeping them,
    and {"match_modes": [...]} for the modes snapped to the map at all: parts
    in any other mode keep their GPS exactly as recorded, unless the user set
    that part's mode by hand.
    """
    opts = {"strength": "fill", **(options or {})}
    strength = opts["strength"]
    keep_ranges = [tuple(sorted((int(a), int(b)))) for a, b in (opts.get("keep_gaps") or [])]
    force_ranges = [tuple(sorted((int(a), int(b)))) for a, b in (opts.get("force_gaps") or [])]
    route_ranges = [tuple(sorted((int(a), int(b)))) for a, b in (opts.get("route_gaps") or [])]
    force_all = bool(opts.get("force_route"))
    # Detailed tracks (a watch, a bike computer) are kept as recorded unless
    # the trip asks for them to be snapped to the map anyway.
    snap = bool(opts.get("snap_recorded"))
    match_modes = set(MOVING if opts.get("match_modes") is None else opts["match_modes"])

    def left_alone(mode: str, by_hand: bool) -> bool:
        return mode in MOVING and mode not in match_modes and not by_hand

    def keep_gap(t0: int, t1: int) -> bool:
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        return any(lo <= r1 and r0 <= hi for r0, r1 in keep_ranges)

    def force_gap(t0: int, t1: int) -> bool | str:
        """True: route it past the plausibility gate. "route": route it, with
        the usual checks, even where the trip keeps its gaps. False: no say."""
        if force_all:
            return True
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        if any(lo <= r1 and r0 <= hi for r0, r1 in force_ranges):
            return True
        return "route" if any(lo <= r1 and r0 <= hi for r0, r1 in route_ranges) else False
    fixes, dropped = clean(points)
    if not fixes:
        return {"segments": [], "stream": [], "dropped": dropped, "quality": {},
                "engine_version": ENGINE_VERSION}
    labels, forced, keys = label(fixes, overrides)
    if not snap and assemble.precise(fixes):
        return _as_recorded(fixes, dropped, labels, forced, keys, "detailed")
    moving = [(labels[i], any(forced[i:j])) for _, i, j in runs(keys) if labels[i] in MOVING]
    if moving and all(left_alone(m, f) for m, f in moving):
        # Nothing in this trip is a mode that gets matched.
        return _as_recorded(fixes, dropped, labels, forced, keys, "modes")
    # Somewhere the map server has no map for (another country): nothing can
    # be matched, so the GPS stays as recorded rather than "needing a look".
    no_map = valhalla.uncovered(fixes)
    trains = any(labels[i] == "train" for _, i, j in runs(keys))
    if no_map is not None and no_map >= NO_MAP_FRAC and not trains:
        return _as_recorded(fixes, dropped, labels, forced, keys, "no_map", no_map)
    # A trip logged partly on a watch and partly on a phone: each part is
    # judged on its own, so the watch part is kept and the phone part matched.
    dense = assemble.dense_blocks(fixes)
    detailed = assemble.detailed_fixes(fixes)
    keys = [k if forced[i] else f"{k}|{'d' if dense[i] else 's'}" for i, k in enumerate(keys)]

    segments: list[dict[str, Any]] = []
    for key, i, j in runs(keys):
        mode = labels[i]
        seg_fixes = fixes[i:j]
        is_forced = any(forced[i:j])
        profile = None
        if mode not in ("stay", "raw") and len(seg_fixes) == 1 and not is_forced:
            # A lone fix between stays: a waypoint of the seam, not a segment.
            segments.append({"mode": mode, "forced": False, "profile": None, "n_fixes": 1,
                             "pieces": [], "via": seg_fixes, "key": key})
            continue
        if mode == "stay" and not snap and all(detailed[i:j]) and len(seg_fixes) > 1:
            # A stop logged by a watch keeps its points, like the ride around it.
            profile = "recorded"
            pieces = [assemble._piece("recorded", [[p["lat"], p["lon"], p["timestamp"], 1] for p in seg_fixes],
                                      len(seg_fixes))]
        elif mode == "stay":
            pieces = assemble.stay_pieces(seg_fixes)
        elif mode == "raw":
            pieces = assemble.raw_pieces(seg_fixes)
        elif left_alone(mode, is_forced):
            # A mode the user doesn't want matched: its GPS stays as recorded.
            profile = "kept"
            pieces = [assemble._piece("recorded", [[p["lat"], p["lon"], p["timestamp"], 1] for p in seg_fixes],
                                      len(seg_fixes))]
        elif mode != "train" and not snap and (all(detailed[i:j]) or assemble.precise(seg_fixes)):
            # Already a better line than the map would give: keep it.
            profile = "recorded"
            pieces = assemble.recorded_pieces(seg_fixes, mode, strength, keep_gap, force_gap)
        else:
            if mode == "train":
                res = match_segment(seg_fixes, "train")
                if not is_forced and not _rail_wins(seg_fixes, res):
                    mode = "drive" if summary_speed(seg_fixes)["median"] > 25 else "walk"
                    mode, res = _choose_road(seg_fixes, mode)
            elif is_forced:
                res = match_segment(seg_fixes, mode)
            else:
                mode, res = _choose_road(seg_fixes, mode)
            profile = res.get("profile")
            pieces = assemble.road_pieces(seg_fixes, mode, res, strength, keep_gap, force_gap)
        segments.append({"mode": mode, "forced": is_forced, "profile": profile,
                         "n_fixes": j - i, "pieces": pieces, "key": key if is_forced else None})

    segments = assemble.connect(segments, strength, keep_gap, force_gap)
    segments = assemble.merge_same_mode(segments)
    assemble.finish(segments)
    out = assemble.stream(segments)
    return {
        "segments": segments,
        "stream": out,
        "dropped": [{k: p.get(k) for k in ("lat", "lon", "timestamp", "accuracy", "_drop")} for p in dropped],
        "quality": {**quality(fixes, segments, out), **({"no_map_frac": round(no_map, 3)} if no_map else {})},
        "engine_version": ENGINE_VERSION,
    }


def _as_recorded(fixes: list[dict[str, Any]], dropped: list[dict[str, Any]], labels: list[str],
                 forced: list[bool], keys: list[str], why: str, no_map: float | None = None) -> dict[str, Any]:
    """A whole trip logged in fine detail (why="detailed"), made only of
    modes the user doesn't match (why="modes"), or where the map server has
    no map (why="no_map") is left exactly as recorded:
    every fix stays, stops and holes included. Only the modes are worked out,
    for the record; the portal does not change such a trip in Dawarich."""
    rs = runs(keys)
    segments = []
    for k, (key, i, j) in enumerate(rs):
        # Each part runs on to the first fix of the next, so the line is unbroken.
        part = fixes[i:j + 1] if k + 1 < len(rs) else fixes[i:j]
        coords = [[p["lat"], p["lon"], p["timestamp"], 1] for p in part]
        segments.append({"mode": labels[i], "forced": any(forced[i:j]),
                         "profile": "recorded" if why == "detailed" else "kept",
                         "n_fixes": j - i, "key": key if any(forced[i:j]) else None,
                         "pieces": [assemble._piece("recorded", coords, j - i)]})
    segments = assemble.merge_same_mode(segments)
    assemble.finish(segments)
    out = assemble.stream(segments)
    q = quality(fixes, segments, out)
    q["as_recorded"] = True
    q["kept_because"] = why
    if no_map is not None:
        q["no_map_frac"] = round(no_map, 3)
    return {
        "segments": segments,
        "stream": out,
        "dropped": [{k: p.get(k) for k in ("lat", "lon", "timestamp", "accuracy", "_drop")} for p in dropped],
        "quality": q,
        "engine_version": ENGINE_VERSION,
    }


def quality(fixes: list[dict[str, Any]], segments: list[dict[str, Any]],
            out: list[dict[str, Any]]) -> dict[str, Any]:
    km: dict[str, float] = {}
    for s in segments:
        for p in s["pieces"]:
            km[p["kind"]] = km.get(p["kind"], 0.0) + p["km"]
    moving = sum(v for k, v in km.items() if k != "stay")
    errs = deviations(fixes, out)
    raw_km = path_km(fixes)
    return {
        "raw_km": round(raw_km, 3),
        "km": round(path_km(out), 3),
        "matched_frac": round(km.get("matched", 0.0) / moving, 3) if moving else 0.0,
        "recorded_frac": round(km.get("recorded", 0.0) / moving, 3) if moving else 0.0,
        "filled_frac": round(km.get("filled", 0.0) / moving, 3) if moving else 0.0,
        "raw_frac": round((km.get("raw", 0.0) + km.get("straight", 0.0)) / moving, 3) if moving else 0.0,
        "p50": round(percentile(errs, 0.5), 1),
        "p95": round(percentile(errs, 0.95), 1),
        "segments": len(segments),
    }
