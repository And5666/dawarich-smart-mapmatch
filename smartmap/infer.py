"""Per-fix state inference.

A fix gets one of: auto, bicycle, pedestrian, rail, raw. The four matched
states are hypotheses from Valhalla's global trace matcher plus the rail
shapes; a Viterbi pass picks the best sequence with mode-switch and
distance-jump penalties. Raw is a state too, not a post-processing fallback.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from typing import Any

from . import rail
from .geo import cumulative, haversine, point_at, project_arc
from .segment import features, prepare
from .valhalla import ValhallaError, route_between, trace_attributes

PROFILES = ("auto", "bicycle", "pedestrian")
SIGMA = {"auto": 25.0, "bicycle": 28.0, "pedestrian": 35.0, "rail": 22.0}
MODE_SWITCH = 2.2
RAW_SWITCH = 0.55
MAX_CHUNK_KM = 135.0
MAX_CHUNK_N = 140

ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary",
                "residential", "unclassified", "service_other", "service",
                "living_street", "road"}
PATH_CLASSES = {"path", "footway", "pedestrian", "steps", "cycleway",
                "bridleway", "track"}


def chunks(points: list[dict[str, Any]], max_km: float = MAX_CHUNK_KM,
           max_n: int = MAX_CHUNK_N) -> list[list[dict[str, Any]]]:
    if len(points) <= 2:
        return [points]
    out: list[list[dict[str, Any]]] = []
    start = 0
    dist = 0.0
    for i in range(1, len(points)):
        dist += haversine(points[i - 1]["lat"], points[i - 1]["lon"],
                          points[i]["lat"], points[i]["lon"])
        if dist >= max_km * 1000 or i - start >= max_n:
            out.append(points[start:i + 1])
            start = i
            dist = 0.0
    if start < len(points) - 1:
        out.append(points[start:])
    return out or [points]


def _chunk_candidates(chunk: list[dict[str, Any]], profile: str,
                      shapes: dict[tuple[str, int], tuple[list[tuple[float, float]], list[float]]],
                      chunk_id: int) -> list[dict[str, Any] | None]:
    try:
        res = trace_attributes(chunk, profile)
    except ValhallaError:
        if len(chunk) <= 8:
            return [None] * len(chunk)
        mid = len(chunk) // 2
        left = _chunk_candidates(chunk[:mid + 1], profile, shapes, chunk_id)
        right = _chunk_candidates(chunk[mid:], profile, shapes, chunk_id + 1000)
        return left[:-1] + right
    poly = res["shape"]
    if len(poly) < 2:
        return [None] * len(chunk)
    cum = cumulative(poly)
    total = cum[-1]
    shapes[(profile, chunk_id)] = (poly, cum)
    matched = res["matched_points"]
    out: list[dict[str, Any] | None] = []
    prev_arc = 0.0
    for j, p in enumerate(chunk):
        mp = matched[j] if j < len(matched) else None
        if not mp or mp.get("lat") is None or mp.get("lon") is None:
            out.append(None)
            continue
        # Valhalla echoes the input point for an unmatched fix. Matching that
        # echoed coordinate would turn every dropout into a perfectly placed
        # road point, which is exactly the invented geometry this model avoids.
        if mp.get("type") != "matched" or mp.get("edge_index") is None:
            out.append(None)
            continue
        d = float(mp.get("dist") or 0.0)
        if d > 120:
            out.append(None)
            continue
        chord = haversine(chunk[j - 1]["lat"], chunk[j - 1]["lon"],
                          p["lat"], p["lon"]) if j else 100.0
        lo = max(0.0, prev_arc - 20.0)
        hi = min(total, prev_arc + max(900.0, chord * 5.0 + 350.0))
        arc, err = project_arc(poly, cum, float(mp["lat"]), float(mp["lon"]), lo, hi)
        if abs(arc - prev_arc) > max(1200.0, chord * 8 + 500):
            arc2, err2 = project_arc(poly, cum, float(mp["lat"]), float(mp["lon"]))
            if err2 + 2 < err:
                arc, err = arc2, err2
        if j and arc < prev_arc - 80:
            arc = prev_arc
        # The matched point must actually lie on the returned shape. If it
        # does not, this is a different matching run; leave the fix unmatched.
        if err > 90.0:
            out.append(None)
            continue
        prev_arc = arc
        out.append({
            "lat": float(mp["lat"]), "lon": float(mp["lon"]),
            "dist": d, "arc": arc, "chunk": chunk_id,
            "road_class": mp.get("road_class") or "",
            "use": mp.get("use") or "",
            "type": mp.get("type") or "matched",
        })
    return out


def match_profiles(points: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any] | None]],
                                                          dict[tuple[str, int], Any]]:
    shapes: dict[tuple[str, int], Any] = {}
    out: dict[str, list[dict[str, Any] | None]] = {p: [] for p in PROFILES}
    for profile in PROFILES:
        for ci, ch in enumerate(chunks(points)):
            out[profile].extend(_chunk_candidates(ch, profile, shapes, ci))
        if len(out[profile]) < len(points):
            out[profile].extend([None] * (len(points) - len(out[profile])))
        elif len(out[profile]) > len(points):
            out[profile] = out[profile][:len(points)]
    return out, shapes


def rail_candidates(points: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
    """Project a whole section onto one rail alignment.

    Picking the nearest shape per fix makes the line jump between route
    patterns on parallel tracks; those jumps add fabricated distance. Score
    each shape by how many fixes it explains and by whether its arc span is
    close to the raw distance, then project every fix onto the winner.
    """
    nearest = [rail.nearest(p["lat"], p["lon"]) for p in points]
    counts: dict[int, int] = {}
    for n in nearest:
        if n and n["dist"] <= 110:
            counts[n["shape"]] = counts.get(n["shape"], 0) + 1
    if not counts:
        return [None] * len(points)
    path = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
               for a, b in zip(points, points[1:]))
    best: tuple[float, int, list[dict[str, Any] | None]] | None = None
    for shape in counts:
        poly = rail.shape_line(shape)
        cum = rail.shape_cum(shape)
        cands: list[dict[str, Any] | None] = []
        arcs: list[float] = []
        dists: list[float] = []
        for p in points:
            arc, dist = project_arc(poly, cum, p["lat"], p["lon"])
            if dist > 110.0:
                cands.append(None)
            else:
                lat, lon, _ = point_at(poly, cum, arc)
                cands.append({"lat": lat, "lon": lon, "dist": dist, "arc": arc, "shape": shape})
                arcs.append(arc)
                dists.append(dist)
        if not arcs:
            continue
        span = max(arcs) - min(arcs)
        # A route pattern that covers the fixes but travels 2-3x the raw
        # distance is a loop or a different service pattern, not the train.
        if not (0.50 <= span / max(1.0, path) <= 1.40):
            continue
        cov = len(arcs) / max(1, len(points))
        ratio_pen = abs(span / max(1.0, path) - 1.0)
        med = sorted(dists)[len(dists) // 2]
        score = cov - 0.25 * ratio_pen - 0.0003 * med
        if best is None or score > best[0]:
            best = (score, shape, cands)
    return best[2] if best else [None] * len(points)


def _smooth_speeds(points: list[dict[str, Any]]) -> list[float]:
    raw = [float(p.get("_speed") or 0.0) for p in points]
    out: list[float] = []
    for i in range(len(raw)):
        w = raw[max(0, i - 2): i + 3]
        out.append(sorted(w)[len(w) // 2] if w else 0.0)
    return out


def _edge_kind(c: dict[str, Any]) -> str:
    return ((c.get("road_class") or "") + " " + (c.get("use") or "")).lower()


def _emission(profile: str, c: dict[str, Any] | None, speed: float,
              med: float, rail_frac: float) -> float:
    if profile == "rail":
        if c is None:
            return 12.0
        e = 0.5 * (min(c["dist"], 120.0) / SIGMA["rail"]) ** 2
        if c["dist"] > 70:
            e += 3.0
        if speed < 5:
            e += 0.8
        e += -1.0 if rail_frac >= 0.30 else 0.8
        return e
    if c is None:
        return 12.0
    e = 0.5 * (min(c["dist"], 150.0) / SIGMA[profile]) ** 2
    kind = _edge_kind(c)
    is_path = any(k in kind for k in PATH_CLASSES)
    is_road = any(k in kind for k in ROAD_CLASSES)
    if profile == "auto":
        if speed < 2.0:
            e += 1.4
        elif speed < 8.0:
            e += 0.5
        elif speed > 200.0:
            e += 4.0
        if med >= 12.0:
            e -= 0.5
        if is_path:
            e += 1.1
        if is_road:
            e -= 0.55
        if "motorway" in kind or "trunk" in kind:
            e -= 0.5
    elif profile == "bicycle":
        if speed > 65.0:
            e += 2.5
        elif speed < 2.0:
            e += 0.5
        if 7.0 <= med <= 38.0:
            e -= 0.65
        if any(k in kind for k in ("cycleway", "path", "track", "bridleway")):
            e -= 0.55
        if "motorway" in kind or "trunk" in kind:
            e += 3.0
    else:
        if speed > 35.0:
            e += 4.0
        elif speed > 20.0:
            e += 2.0
        if med <= 9.0:
            e -= 0.55
        if any(k in kind for k in ("footway", "path", "pedestrian", "steps", "track")):
            e -= 0.65
        if "motorway" in kind or "trunk" in kind:
            e += 2.5
    return e


def _raw_emission(best_dist: float) -> float:
    if best_dist <= 15:
        return 6.0
    if best_dist <= 30:
        return 4.2
    if best_dist <= 50:
        return 2.6
    if best_dist <= 85:
        return 1.2
    return 0.15


def _transition(prev: dict[str, Any], cur: dict[str, Any], dt: float) -> float:
    a, b = prev["state"], cur["state"]
    if a == b:
        cost = 0.0
    elif a == "raw" or b == "raw":
        cost = RAW_SWITCH
    elif (a == "rail") != (b == "rail"):
        cost = 1.6
    else:
        cost = MODE_SWITCH
    # A jump no vehicle could make is not a mode switch, it is a bad state.
    if a != "raw" and b != "raw" and dt > 0:
        d = haversine(prev.get("_lat", 0.0), prev.get("_lon", 0.0),
                      cur.get("_lat", 0.0), cur.get("_lon", 0.0))
        if d / dt * 3.6 > 320.0:
            cost += 4.0
    return cost


def infer_section(points: list[dict[str, Any]]) -> dict[str, Any]:
    profiles, shapes = match_profiles(points)
    rail_c = rail_candidates(points)
    rail_frac = sum(1 for c in rail_c if c and c["dist"] <= 60) / max(1, len(points))
    # A handful of fixes beside a railway is a road, not a train. Short rail
    # runs are what turned 3190 into a train and routed it onto the tracks.
    if sum(1 for c in rail_c if c) < 10:
        rail_c = [None] * len(points)
        rail_frac = 0.0
    f = features(points)
    med = f["speed"]
    speeds = _smooth_speeds(points)

    # A long run of fixes on one rail alignment at line speed is a train. The
    # road matcher can follow the service road beside the track for a few
    # fixes, but it must not turn the train into a road trip.
    force_rail = rail_frac >= 0.58 and med >= 12.0

    # Viterbi layers. Each state is a dict with a cost, a parent, and the point
    # that will be written if this state wins.
    layers: list[list[dict[str, Any]]] = []
    for i, p in enumerate(points):
        best_road = min((profiles[k][i]["dist"] for k in PROFILES
                         if profiles[k][i] is not None), default=999.0)
        raw = {"state": "raw", "cost": _raw_emission(best_road), "parent": None,
               "_lat": p["lat"], "_lon": p["lon"], "cand": None}
        states = [raw]
        for mode in PROFILES:
            c = profiles[mode][i]
            states.append({"state": mode, "cost": _emission(mode, c, speeds[i], med, rail_frac),
                           "parent": None, "cand": c,
                           "_lat": c["lat"] if c else p["lat"],
                           "_lon": c["lon"] if c else p["lon"]})
        c = rail_c[i]
        states.append({"state": "rail", "cost": _emission("rail", c, speeds[i], med, rail_frac),
                       "parent": None, "cand": c,
                       "_lat": c["lat"] if c else p["lat"],
                       "_lon": c["lon"] if c else p["lon"]})
        if i:
            dt = max(0, p["timestamp"] - points[i - 1]["timestamp"])
            for s in states:
                best = None
                for prev in layers[-1]:
                    total = prev["cost"] + _transition(prev, s, dt)
                    if best is None or total < best[0]:
                        best = (total, prev)
                if best is not None:
                    s["cost"] += best[0]
                    s["parent"] = best[1]
        if force_rail:
            # Keep rail states only where a rail candidate exists; a gap in the
            # rail data is raw, not a reason to drive on the adjacent road.
            for s in states:
                if s["state"] == "rail" and s["cand"] is not None:
                    s["cost"] = -1.0
                elif s["state"] != "rail":
                    s["cost"] = 20.0
        layers.append(states)

    # Reconstruct.
    node = min(layers[-1], key=lambda s: s["cost"])
    states: list[str] = ["raw"] * len(points)
    cands: list[dict[str, Any] | None] = [None] * len(points)
    for i in range(len(points) - 1, -1, -1):
        states[i] = node["state"]
        cands[i] = node["cand"]
        node = node["parent"] or min(layers[max(0, i - 1)], key=lambda s: s["cost"])

    # Smooth very short flickers. A mode that survives less than four fixes
    # and a minute is not an activity; it is the HMM changing its mind.
    i = 0
    while i < len(states):
        j = i + 1
        while j < len(states) and states[j] == states[i]:
            j += 1
        dur = points[j - 1]["timestamp"] - points[i]["timestamp"]
        if states[i] != "raw" and (j - i < 4 and dur < 75):
            neigh = []
            if i > 0:
                neigh.append(states[i - 1])
            if j < len(states):
                neigh.append(states[j])
            pick = max(set(neigh), key=neigh.count) if neigh else "raw"
            for k in range(i, j):
                states[k] = pick
                if pick == "raw":
                    cands[k] = None
        # A short rail visit is a road beside the railway, not a train leg.
        if states[i] == "rail":
            span = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                       for a, b in zip(points[i:j], points[i + 1:j]))
            if j - i < 10 or span < 300.0:
                for k in range(i, j):
                    states[k] = "raw"
                    cands[k] = None
        i = j

    # Cycling at urban speed on roads with cycleway/path evidence is much
    # more likely a bicycle than a car. Prefer the bicycle hypothesis; this
    # also lets Valhalla's bicycle costing take contraflow/cycle paths instead
    # of looping around car one-way restrictions.
    if 8.0 <= med <= 42.0 and f["path"] <= 10000.0:
        auto_idx = [i for i, s in enumerate(states) if s == "auto"]
        bike_path = 0
        auto_motor = 0
        for i in range(len(points)):
            ac = profiles["auto"][i]
            bc = profiles["bicycle"][i]
            if ac is not None:
                k = _edge_kind(ac)
                if "motorway" in k or "trunk" in k:
                    auto_motor += 1
            if bc is not None:
                k = _edge_kind(bc)
                if any(x in k for x in ("cycleway", "path", "footway", "track", "bridleway")):
                    bike_path += 1
        if auto_idx and bike_path >= max(1, int(0.08 * len(points))) and auto_motor <= max(2, int(0.35 * len(points))):
            for i in auto_idx:
                if profiles["bicycle"][i] is not None:
                    states[i] = "bicycle"
                    cands[i] = profiles["bicycle"][i]

    # A matched state without a candidate cannot be written; fall back to raw.
    for i, s in enumerate(states):
        if s != "raw" and cands[i] is None:
            states[i] = "raw"
    return {
        "states": states,
        "candidates": cands,
        "profiles": profiles,
        "rail": rail_c,
        "shapes": shapes,
        "rail_frac": rail_frac,
        "features": f,
        "med_kmh": med,
    }



def _rescue_raw(points: list[dict[str, Any]], states: list[str],
                cands: list[dict[str, Any] | None],
                shapes: dict[tuple[str, int], Any], uid: int) -> None:
    """Rescue a road stretch that map_snap abandoned.

    Sparse motorway fixes can exceed Valhalla's 2 km breakage limit, leaving
    a long raw gap even though the trace is plainly on a road. If the raw run
    is fast, its endpoints route, and the route explains the fixes, use that
    route. Slow/offroad runs are never rescued.
    """
    i = 0
    while i < len(states):
        if states[i] != "raw":
            i += 1
            continue
        j = i + 1
        while j < len(states) and states[j] == "raw":
            j += 1
        if j - i < 2:
            i = j
            continue
        speeds = []
        path = 0.0
        for a, b in zip(points[i:j], points[i + 1:j]):
            d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            path += d
            dt = b["timestamp"] - a["timestamp"]
            if dt > 0:
                speeds.append(d / dt * 3.6)
        speeds.sort()
        med = speeds[len(speeds) // 2] if speeds else 0.0
        dt = points[j - 1]["timestamp"] - points[i]["timestamp"]
        if med < 10.0 or med > 190.0 or path < 150.0 or dt <= 0:
            i = j
            continue
        profile = "auto" if med >= 18.0 else "bicycle"
        r = route_between(points[i], points[j - 1], profile)
        if not r or len(r["shape"]) < 2:
            i = j
            continue
        poly, cum = r["shape"], cumulative(r["shape"])
        ratio = r["length_m"] / max(1.0, path)
        if not (0.65 <= ratio <= 1.45):
            i = j
            continue
        tr = r.get("time_s") or 0.0
        if tr and not (0.40 <= tr / dt <= 2.6):
            i = j
            continue
        arcs: list[float] = []
        errs: list[float] = []
        prev = 0.0
        for p in points[i:j]:
            arc, err = project_arc(poly, cum, p["lat"], p["lon"],
                                   max(0.0, prev - 30.0),
                                   min(cum[-1], prev + max(900.0, path)))
            if arc < prev - 50:
                arc = prev
            arcs.append(arc)
            errs.append(err)
            prev = arc
        cov = sum(1 for e in errs if e <= 90.0) / max(1, len(errs))
        if cov < 0.75:
            i = j
            continue
        cid = uid + 100000 + i
        shapes[(profile, cid)] = (poly, cum)
        for k, arc in enumerate(arcs):
            states[i + k] = profile
            cands[i + k] = {"lat": points[i + k]["lat"], "lon": points[i + k]["lon"],
                            "dist": errs[k], "arc": arc, "chunk": cid,
                            "road_class": "", "use": "", "type": "route"}
        i = j

def infer_track(raw_points: list[dict[str, Any]]) -> dict[str, Any]:
    fixes, sections = prepare(raw_points)
    for sec in sections:
        if sec["kind"] != "move":
            sec["states"] = [sec["kind"]] * len(sec["points"])
            sec["candidates"] = [None] * len(sec["points"])
            sec["match"] = None
            continue
        m = infer_section(sec["points"])
        _rescue_raw(sec["points"], m["states"], m["candidates"], m["shapes"], sec["start"])
        sec["states"] = m["states"]
        sec["candidates"] = m["candidates"]
        sec["match"] = m
    return {"fixes": fixes, "sections": sections}
