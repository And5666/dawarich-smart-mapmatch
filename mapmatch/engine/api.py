"""Engine entry point: one trip of raw fixes in, segments and a stream out."""
from __future__ import annotations

from typing import Any

from . import assemble, valhalla
from .clean import clean
from .geo import haversine
from .match import fit_score, match_segment
from .modes import label, runs, summary_speed
from .quality import between, deviations, path_km, percentile

ENGINE_VERSION = "2.12.1"
MODES = ("walk", "cycle", "drive", "train", "stay", "raw")
MOVING = ("walk", "cycle", "drive", "train")
NO_MAP_FRAC = 0.75   # share of sampled fixes with no map under them: the trip is abroad


FIT_EDGE = 0.08            # fit_score: 0.08 is 8% more fixes on the network, or 8 m closer
FIT_EDGE_SLOW = 0.25       # for a segment that never looked faster than a bike
RIDE_FAST_P90_KMH = 30.0


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
    # How much better the other mode must fit to win. Any ride on ordinary
    # streets fits the car network as well as the cycle network, often a
    # shade better, so a small difference says nothing: a ride that never
    # passed 30 km/h was being called a drive on it. Only a clear difference
    # counts (the track is on roads that mode cannot use), and a ride at
    # cycling speeds needs a clearer one still.
    edge = 0.03
    if mode == "cycle":
        edge = FIT_EDGE_SLOW if sp["p90"] < RIDE_FAST_P90_KMH else FIT_EDGE
    elif len(alts) > 1:
        edge = FIT_EDGE
    best = None
    for k, m in enumerate(alts):
        res = match_segment(fixes, m)
        sc = fit_score(fixes, res)
        if k == 0:
            sc += edge  # the speed-based label wins unless the other fits clearly better
        if best is None or sc > best[0]:
            best = (sc, m, res)
    return best[1], best[2]


HOOK_EXTRA_M = 300.0     # the line is this much longer than the straight hop between two fixes
HOOK_RATIO = 1.8         # and this many times as long
HOOK_GAIN = 0.6          # leaving a fix out must take away this share of the extra
HOOK_MAX = 3             # per segment
HOOK_END_HOP_M = 300.0   # an end fix is only given up for a hop this short


def _unhook(fixes: list[dict[str, Any]], mode: str, res: dict[str, Any]
            ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """One fix a little off the road can pull the matched line round the
    block: up a side street and back, or to the far carriageway and round a
    roundabout (seen as 350-500 m loops on a straight main road). Where the
    line between two fixes is much longer than the hop, try the match again
    without one of them, the less accurate first, and keep that when the
    loop goes and the rest of the line stays as it was. The fix is reported
    as dropped ("hook"); nothing else is touched.

    Returns (fixes, match, dropped)."""
    dropped: list[dict[str, Any]] = []
    tried: set[int] = set()          # hops (by their first fix's time) that no dropped fix improved
    for _ in range(HOOK_MAX * 4):
        if len(dropped) >= HOOK_MAX:
            break
        anchors = res["anchors"]
        n = len(fixes)
        found = None
        for k in range(0, n - 1):
            if fixes[k]["timestamp"] in tried:
                continue
            a0, a1 = anchors[k], anchors[k + 1]
            if not a0 or not a1 or a0["piece"] != a1["piece"]:
                continue
            line = a1["arc"] - a0["arc"]
            hop = haversine(fixes[k]["lat"], fixes[k]["lon"], fixes[k + 1]["lat"], fixes[k + 1]["lon"])
            if line - hop >= HOOK_EXTRA_M and line >= HOOK_RATIO * max(hop, 30.0):
                found = (k, line - hop, hop)
                break
        if found is None:
            break
        k, extra, hop = found
        tried.add(fixes[k]["timestamp"])
        total = sum(1 for a in anchors if a)

        def attempt(x: int) -> dict[str, Any] | None:
            try:
                new = match_segment(fixes[:x] + fixes[x + 1:], mode)
            except Exception:  # noqa: BLE001 - no better match, keep what we have
                return None
            return new if sum(1 for a in new["anchors"] if a) >= total - 1 else None

        def take(x: int, new: dict[str, Any]) -> None:
            nonlocal fixes, res
            fixes[x]["_drop"] = "hook"
            dropped.append(fixes[x])
            fixes, res = fixes[:x] + fixes[x + 1:], {**new, "profile": new.get("profile", res.get("profile"))}

        if k == 0 or k == n - 2:
            # The first or last hop of the segment (setting off, arriving). The end fix has no
            # neighbour beyond it to hold the line, so it is the one on the wrong road. Without it
            # the segment ends one fix earlier, and the short seam to the stop beside it is left
            # to the gap filler, which draws a short hop straight when every route goes round.
            if n < 4 or hop > HOOK_END_HOP_M:
                continue
            x = 0 if k == 0 else n - 1
            new = attempt(x)
            if new is not None:
                take(x, new)
            continue
        before = _span(anchors, k - 1, k + 2)
        if before is None:
            continue
        for x in sorted((k, k + 1), key=lambda i: -(fixes[i].get("accuracy") or 20.0)):
            new = attempt(x)
            # The two neighbours sit one place nearer each other in the trial.
            after = _span(new["anchors"], k - 1, k + 1) if new is not None else None
            if after is not None and after <= before - HOOK_GAIN * extra:
                take(x, new)
                break
    return fixes, res, dropped


def _span(anchors: list[dict[str, Any] | None], i: int, j: int) -> float | None:
    """Length of matched line from fix i to fix j, when it is one unbroken piece."""
    if i < 0 or j >= len(anchors):
        return None
    a, b = anchors[i], anchors[j]
    if not a or not b or a["piece"] != b["piece"]:
        return None
    return b["arc"] - a["arc"]


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
    fixes, dropped = clean(points, jitter=True)
    if not fixes:
        return {"segments": [], "stream": [], "dropped": dropped, "quality": {},
                "engine_version": ENGINE_VERSION}
    labels, forced, keys = label(fixes, overrides)
    # A trip logged in fine detail is left as recorded, unless it has a train
    # ride with nothing recorded on the way (a watch paused at one station
    # and started again at another): that hole is a 17 km straight line as
    # recorded, and belongs on the railway. The detailed parts either side
    # are still kept point for point, each judged on its own below.
    train_hole = any(k.startswith("auto:train") for k in keys)
    # Kept point for point does not mean kept blindly: the holes in it are routed or
    # left unconnected, and a stop is one dot, like in any other trip. Only when
    # nothing of that kind was found is the trip reported as "kept as recorded".
    whole = not snap and not train_hole and assemble.precise(fixes)
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
    # (A train ride found across a hole is one run whatever was logged either side of it.)
    keys = [k if forced[i] or k.startswith("auto:") else f"{k}|{'d' if dense[i] else 's'}" for i, k in enumerate(keys)]

    segments: list[dict[str, Any]] = []
    hooked: list[dict[str, Any]] = []
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
        if mode == "stay":
            # One dot, however it was logged. A watch left running in a shop draws a
            # tangle a hundred metres wide: where the stop was is its middle.
            pieces = assemble.stay_pieces(seg_fixes)
        elif mode == "raw":
            pieces = assemble.raw_pieces(seg_fixes)
        elif left_alone(mode, is_forced):
            # A mode the user doesn't want matched: its GPS stays as recorded.
            profile = "kept"
            pieces = [assemble._piece("recorded", [[p["lat"], p["lon"], p["timestamp"], 1] for p in seg_fixes],
                                      len(seg_fixes))]
        elif mode != "train" and not snap and (whole or all(detailed[i:j]) or assemble.precise(seg_fixes)):
            # Already a better line than the map would give: keep it.
            profile = "recorded"
            pieces = assemble.recorded_pieces(seg_fixes, mode, strength, keep_gap, force_gap)
        else:
            if mode == "train":
                res = match_segment(seg_fixes, "train")
                if not is_forced and not key.startswith("auto:") and not _rail_wins(seg_fixes, res):
                    mode = "drive" if summary_speed(seg_fixes)["median"] > 25 else "walk"
                    mode, res = _choose_road(seg_fixes, mode)
            elif is_forced:
                res = match_segment(seg_fixes, mode)
            else:
                mode, res = _choose_road(seg_fixes, mode)
            if mode != "train":
                seg_fixes, res, off = _unhook(seg_fixes, mode, res)
                hooked.extend(off)
            profile = res.get("profile")
            pieces = assemble.road_pieces(seg_fixes, mode, res, strength, keep_gap, force_gap)
        segments.append({"mode": mode, "forced": is_forced, "profile": profile,
                         "n_fixes": j - i, "pieces": pieces, "key": key if is_forced else None})

    segments = assemble.connect(segments, strength, keep_gap, force_gap)
    segments = assemble.merge_same_mode(segments)
    assemble.unlink(segments)
    assemble.finish(segments)
    out = assemble.stream(segments)
    if hooked:
        gone = {id(p) for p in hooked}
        fixes = [p for p in fixes if id(p) not in gone]
        dropped = sorted(dropped + hooked, key=lambda p: p["timestamp"])
    q = {**quality(fixes, segments, out), **({"no_map_frac": round(no_map, 3)} if no_map else {})}
    if whole and all(p["kind"] in ("recorded", "join") for s in segments for p in s["pieces"]):
        q["as_recorded"] = True          # nothing in it needed cleaning: Dawarich already has it right
        q["kept_because"] = "detailed"
    return {
        "segments": segments,
        "stream": out,
        "dropped": [{k: p.get(k) for k in ("lat", "lon", "timestamp", "accuracy", "_drop")} for p in dropped],
        "quality": q,
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
    dot = False
    for k, (key, i, j) in enumerate(rs):
        if labels[i] == "stay" and j - i > 1:
            # A stop is one dot here too: it needs no map, and its scatter is not a route.
            segments.append({"mode": "stay", "forced": any(forced[i:j]), "profile": None, "n_fixes": j - i,
                             "key": key if any(forced[i:j]) else None, "pieces": assemble.stay_pieces(fixes[i:j])})
            dot = True
            continue
        # Each part runs on to the first fix of the next, so the line is unbroken.
        part = fixes[i:j + 1] if k + 1 < len(rs) else fixes[i:j]
        coords = [[p["lat"], p["lon"], p["timestamp"], 1] for p in part]
        segments.append({"mode": labels[i], "forced": any(forced[i:j]),
                         "profile": "recorded" if why == "detailed" else "kept",
                         "n_fixes": j - i, "key": key if any(forced[i:j]) else None,
                         "pieces": [assemble._piece("recorded", coords, j - i)]})
    segments = assemble.merge_same_mode(segments)
    assemble.unlink(segments)
    assemble.finish(segments)
    out = assemble.stream(segments)
    q = quality(fixes, segments, out)
    # Reported as kept only when nothing in it was tidied: a stop drawn as a dot or a
    # hole left unconnected is a change the portal shows and can push.
    q["as_recorded"] = not dot and not any(p.get("unlinked") or p["kind"] != "recorded" for s in segments for p in s["pieces"])
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


SPARSE_HOP_M = 200.0


def _unsure_km(segments: list[dict[str, Any]]) -> float:
    total = 0.0
    for s in segments:
        for p in s["pieces"]:
            c = p["coords"]
            if p["kind"] == "straight":
                total += p["km"]
            elif p["kind"] == "raw":
                total += sum(d for d in (haversine(x[0], x[1], y[0], y[1]) for x, y in zip(c, c[1:])) if d > SPARSE_HOP_M) / 1000.0
    return total


def quality(fixes: list[dict[str, Any]], segments: list[dict[str, Any]],
            out: list[dict[str, Any]]) -> dict[str, Any]:
    km: dict[str, float] = {}
    holes = 0.0
    for s in segments:
        for p in s["pieces"]:
            km[p["kind"]] = km.get(p["kind"], 0.0) + p["km"]
            if p.get("unlinked"):
                holes += p["km"]            # not drawn: nothing is known about the way
    moving = sum(v for k, v in km.items() if k != "stay")
    # How far the fixes lie from the line, where there is a line: a stop is one dot, and
    # the fixes scattered round it are not points the route missed.
    stops = [(s["pieces"][0]["coords"][0][2], s["pieces"][-1]["coords"][-1][2]) for s in segments
             if s["mode"] == "stay" and s["pieces"] and s["pieces"][0]["coords"]]
    # Nor are fixes the line does not claim to follow: set aside as scatter, or inside a
    # stretch left unconnected or drawn as a plain line (that doubt is unsure_frac's to count).
    stops += [(p["coords"][0][2], p["coords"][-1][2]) for s in segments for p in s["pieces"]
              if len(p["coords"]) >= 2 and (p.get("scatter") or p.get("unlinked") or p["kind"] == "straight")]
    errs = deviations([p for p in fixes if not any(a < p["timestamp"] < b for a, b in stops)], out)
    raw_km = path_km(fixes)
    return {
        "raw_km": round(raw_km, 3),
        "km": round(path_km(out), 3),
        "unlinked_km": round(holes, 3),
        "matched_frac": round(km.get("matched", 0.0) / moving, 3) if moving else 0.0,
        "recorded_frac": round(km.get("recorded", 0.0) / moving, 3) if moving else 0.0,
        "filled_frac": round(km.get("filled", 0.0) / moving, 3) if moving else 0.0,
        "raw_frac": round((km.get("raw", 0.0) + km.get("straight", 0.0)) / moving, 3) if moving else 0.0,
        # The part of that which is a guess: a straight line across a hole, or kept GPS whose fixes are
        # so far apart that it is straight lines too. GPS kept point for point along a path the map
        # does not have is where you were, not a doubt about the match.
        "unsure_frac": round(min(1.0, _unsure_km(segments) / moving), 3) if moving else 0.0,
        "p50": round(percentile(errs, 0.5), 1),
        "p95": round(percentile(errs, 0.95), 1),
        "segments": len(segments),
        **between(fixes, segments),
    }
