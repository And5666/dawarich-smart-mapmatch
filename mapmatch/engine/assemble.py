"""Turn matches into timed pieces, segments and the output point stream."""
from __future__ import annotations

import math
from typing import Any

from .fill import TRAIL_DEV_M, TRAIL_MODES, VMAX_KMH, fill, max_deviation
from .geo import haversine, point_at, subline
from .stays import centroid

SIMPLIFY_M = 2.0
TRAIL_ANCHOR_M = 15.0
SYNTH_MAX_KMH = 260.0


# ---------------------------------------------------------------- helpers
def _xy(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    return (lon * 111320.0 * math.cos(math.radians(lat0)), lat * 110540.0)


def simplify(coords: list[tuple[float, float]], tol: float = SIMPLIFY_M) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative, endpoints kept."""
    if len(coords) < 3:
        return list(coords)
    lat0 = coords[0][0]
    xy = [_xy(a, b, lat0) for a, b in coords]
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        ax, ay = xy[i]
        bx, by = xy[j]
        dx, dy = bx - ax, by - ay
        s2 = dx * dx + dy * dy
        best, bk = -1.0, -1
        for k in range(i + 1, j):
            px, py = xy[k]
            t = 0.0 if s2 <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / s2))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > best:
                best, bk = d, k
        if best > tol:
            keep[bk] = True
            stack.append((i, bk))
            stack.append((bk, j))
    return [c for c, k in zip(coords, keep) if k]


SPUR_MIN_M = 15.0     # shallower out-and-backs are just a wobble
SPUR_MAX_M = 300.0
SPUR_CLOSE_M = 12.0   # out and back legs this close count as the same road
SPUR_KEEP_ACC_M = 15.0


def remove_spurs(coords: list[list[float]], acc_by_ts: dict[int, float]) -> list[list[float]]:
    """Cut out-and-back excursions from a timed line.

    A poor fix that lands on a side road makes the matched line run down it
    and straight back. Where the line turns round sharply and retraces itself
    for 15-300 m, the excursion is removed, unless two or more accurate fixes
    (≤15 m) lie on it: then it was a real detour and is kept.
    """
    while len(coords) >= 3:
        lat0 = coords[0][0]
        xy = [_xy(c[0], c[1], lat0) for c in coords]
        cum = [0.0]
        for a, b in zip(xy, xy[1:]):
            cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        total = cum[-1]

        def pos(arc: float) -> tuple[float, float, int]:
            k = max(0, min(len(cum) - 2, next((i for i in range(1, len(cum)) if cum[i] >= arc), len(cum) - 1) - 1))
            span = cum[k + 1] - cum[k] or 1e-9
            f = max(0.0, min(1.0, (arc - cum[k]) / span))
            return xy[k][0] + (xy[k + 1][0] - xy[k][0]) * f, xy[k][1] + (xy[k + 1][1] - xy[k][1]) * f, k

        cut = None
        for i in range(1, len(xy) - 1):
            ux, uy = xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]
            vx, vy = xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]
            nu, nv = math.hypot(ux, uy), math.hypot(vx, vy)
            if nu < 1e-6 or nv < 1e-6 or (ux * vx + uy * vy) / (nu * nv) > -0.85:
                continue
            apex, depth, r = cum[i], 0.0, 3.0
            while r <= SPUR_MAX_M and apex - r >= 0 and apex + r <= total:
                ax, ay, _ = pos(apex - r)
                bx, by, _ = pos(apex + r)
                if math.hypot(ax - bx, ay - by) > SPUR_CLOSE_M:
                    break
                depth, r = r, r + 3.0
            if depth < SPUR_MIN_M:
                continue
            lo, hi = apex - depth, apex + depth
            inside = [c for c, a in zip(coords, cum) if c[3] and lo < a < hi]
            if sum(1 for c in inside if acc_by_ts.get(int(c[2]), 99.0) <= SPUR_KEEP_ACC_M) >= 2:
                continue
            cut = (lo, hi)
            break
        if cut is None:
            return coords
        lo, hi = cut
        i0 = max(k for k, a in enumerate(cum) if a <= lo)
        i1 = min(k for k, a in enumerate(cum) if a >= hi)
        f = (lo - cum[i0]) / ((cum[i0 + 1] - cum[i0]) or 1e-9)
        a, b = coords[i0], coords[i0 + 1]
        joint = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, 0]
        coords = coords[:i0 + 1] + [joint] + coords[i1:]
    return coords


def length_m(coords: list[Any]) -> float:
    return sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(coords, coords[1:]))


def timed(coords: list[tuple[float, float]], ta: float, tb: float) -> list[list[float]]:
    """[[lat, lon, ts, is_fix]]: ends are fixes, the rest interpolated by distance."""
    coords = simplify(coords)
    if len(coords) == 1:
        return [[coords[0][0], coords[0][1], ta, 1]]
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + haversine(a[0], a[1], b[0], b[1]))
    total = cum[-1]
    out = []
    for k, (lat, lon) in enumerate(coords):
        if k == 0:
            ts, fix = ta, 1
        elif k == len(coords) - 1:
            ts, fix = tb, 1
        else:
            ts = ta + (tb - ta) * (cum[k] / total if total > 0 else k / (len(coords) - 1))
            fix = 0
        out.append([lat, lon, ts, fix])
    return out


def _piece(kind: str, coords: list[list[float]], n_in: int) -> dict[str, Any]:
    return {"kind": kind, "coords": coords, "n_in": n_in, "km": round(length_m(coords) / 1000.0, 4)}


def _fixpt(p: dict[str, Any], lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    return {"lat": p["lat"] if lat is None else lat, "lon": p["lon"] if lon is None else lon,
            "timestamp": p["timestamp"], "accuracy": p.get("accuracy")}


def _filled(a: dict[str, Any], mids: list[dict[str, Any]], b: dict[str, Any], mode: str,
            strength: str, n_in: int, vmax: float | None = None,
            keep: Any = None, force: Any = None) -> dict[str, Any]:
    # keep(t0, t1) says whether this particular hole must stay as recorded, even
    # when the trip is otherwise happy to route across gaps. force(t0, t1) is the
    # explicit "route it anyway": the plausibility gate lets it through.
    forced = bool(keep and keep(a["timestamp"], b["timestamp"]))
    gate = bool(force and force(a["timestamp"], b["timestamp"]))
    f = fill(a, mids, b, mode, "strict" if forced else strength, vmax, gate)
    if f["kind"] == "raw":
        coords = [[p["lat"], p["lon"], p["timestamp"], 1] for p in (a, *mids, b)]
        pc = _piece("raw", coords, n_in)
    else:
        pc = _piece(f["kind"], timed(f["coords"], a["timestamp"], b["timestamp"]), n_in)
    if f.get("why"):
        pc["why"] = f["why"]
    # The hole this piece bridges, so the editor can offer it as a gap to choose.
    pc["gap"] = [int(a["timestamp"]), int(b["timestamp"])]
    if forced:
        pc["kept"] = True
    if gate and f["kind"] == "filled":
        pc["overrode_gate"] = True
    return pc


# ---------------------------------------------------------------- segments
def road_pieces(fixes: list[dict[str, Any]], mode: str, m: dict[str, Any],
                strength: str, keep: Any = None, force: Any = None) -> list[dict[str, Any]]:
    """Pieces for one matched movement segment, gaps already filled."""
    n = len(fixes)
    if n == 1:
        return [_piece("raw", [[fixes[0]["lat"], fixes[0]["lon"], fixes[0]["timestamp"], 1]], 1)]
    anchors = m["anchors"]
    geo = m["pieces"]
    # Walks always get the off-trail rule; rides only when they are mostly
    # on paths and tracks rather than roads (MTB), so road cycling matches fully.
    OFFROAD = ("path", "track", "footway", "bridleway", "cycleway", "steps", "pedestrian")
    kinds = [a["kind"] for a in anchors if a]
    offroad = sum(1 for k in kinds if any(w in k for w in OFFROAD)) > 0.5 * max(1, len(kinds))
    trail = mode == "walk" or (mode == "cycle" and offroad)

    def trusted(k: int) -> bool:
        # On foot or by bike, a fix snapped much further away than its own
        # accuracy usually landed on the wrong path.
        if not trail:
            return True
        acc = fixes[k].get("accuracy") or 20.0
        return anchors[k]["dist"] <= max(TRAIL_ANCHOR_M, min(1.5 * acc, 40.0))

    A = [k for k, a in enumerate(anchors) if a and trusted(k)]
    if not A:
        return [_filled(_fixpt(fixes[0]), fixes[1:-1], _fixpt(fixes[-1]), mode, strength, n, None, keep, force)]

    def snapped(k: int) -> dict[str, Any]:
        a = anchors[k]
        pc = geo[a["piece"]]
        lat, lon, _ = point_at(pc["poly"], pc["cum"], a["arc"])
        return _fixpt(fixes[k], lat, lon)

    out: list[dict[str, Any]] = []
    vmax = VMAX_KMH.get(mode, 200.0)
    # There is no rail router: the ends of a train segment are the walk to
    # and from the platform, and gaps inside it are filled on foot or not at all.
    edge_mode = "walk" if mode == "train" else mode
    if A[0] > 0:
        out.append(_filled(_fixpt(fixes[0]), fixes[1:A[0]], snapped(A[0]), edge_mode, strength, A[0], vmax, keep, force))
    for k1, k2 in zip(A, A[1:]):
        a1, a2 = anchors[k1], anchors[k2]
        ok = False
        if a1["piece"] == a2["piece"]:
            pc = geo[a1["piece"]]
            along = a2["arc"] - a1["arc"]
            span = fixes[k1:k2 + 1]
            raw_path = length_m([(p["lat"], p["lon"]) for p in span])
            chord = haversine(fixes[k1]["lat"], fixes[k1]["lon"], fixes[k2]["lat"], fixes[k2]["lon"])
            dt = fixes[k2]["timestamp"] - fixes[k1]["timestamp"]
            # Train fixes often arrive with batched timestamps; the rail shape
            # itself is the evidence there, so no speed check.
            ok = along <= max(2.0 * raw_path + 100.0, 1.5 * chord + 200.0) and \
                (mode == "train" or dt <= 0 or along / dt * 3.6 <= vmax * 1.3 + 10.0)
            if ok:
                seg = subline(pc["poly"], pc["cum"], a1["arc"], a2["arc"]) if along > 0.5 else \
                    [point_at(pc["poly"], pc["cum"], a1["arc"])[:2], point_at(pc["poly"], pc["cum"], a2["arc"])[:2]]
                if trail:
                    ref = [(p["lat"], p["lon"]) for p in span]
                    slack = min(max(p.get("accuracy") or 15.0 for p in span), 30.0)
                    ok = along <= 1.4 * raw_path + 40.0 and max_deviation(seg, ref) <= TRAIL_DEV_M + slack
            if ok:
                out.append(_piece("matched", timed(seg, fixes[k1]["timestamp"], fixes[k2]["timestamp"]), k2 - k1))
        if not ok:
            out.append(_filled(snapped(k1), fixes[k1 + 1:k2], snapped(k2), mode, strength, k2 - k1, None, keep, force))
    if A[-1] < n - 1:
        out.append(_filled(snapped(A[-1]), fixes[A[-1] + 1:-1], _fixpt(fixes[-1]), edge_mode, strength,
                           n - 1 - A[-1], vmax, keep, force))
    out = merge_pieces(out)
    acc = {int(p["timestamp"]): (p.get("accuracy") or 99.0) for p in fixes}
    for pc in out:
        if pc["kind"] in ("matched", "filled") and len(pc["coords"]) >= 3:
            pc["coords"] = remove_spurs(pc["coords"], acc)
            pc["km"] = round(length_m(pc["coords"]) / 1000.0, 4)
    return out


def raw_pieces(fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_piece("raw", [[p["lat"], p["lon"], p["timestamp"], 1] for p in fixes], len(fixes))]


def stay_pieces(fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lat, lon = centroid(fixes)
    t0, t1 = fixes[0]["timestamp"], fixes[-1]["timestamp"]
    coords = [[lat, lon, t0, 1]] + ([[lat, lon, t1, 1]] if t1 > t0 else [])
    return [{"kind": "stay", "coords": coords, "n_in": len(fixes), "km": 0.0}]


def merge_pieces(pieces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in pieces:
        if not p["coords"]:
            continue
        if out and out[-1]["kind"] == p["kind"]:
            q = out[-1]
            c = p["coords"]
            if q["coords"] and abs(q["coords"][-1][0] - c[0][0]) < 1e-9 and abs(q["coords"][-1][1] - c[0][1]) < 1e-9:
                c = c[1:]
            q["coords"].extend(c)
            q["n_in"] += p["n_in"]
            q["km"] = round(q["km"] + p["km"], 4)
            if p.get("why"):
                q["why"] = q.get("why", []) + p["why"]
        else:
            out.append({**p, "coords": list(p["coords"])})
    return out


def seam_mode(modes: set[str]) -> str:
    modes = set(modes) - {"stay", "raw"}
    if not modes:
        return "walk"
    if "walk" in modes or "train" in modes:
        return "walk"
    if "cycle" in modes:
        return "cycle"
    return "drive"


def _end(seg: dict[str, Any], first: bool) -> dict[str, Any]:
    c = seg["pieces"][0]["coords"][0] if first else seg["pieces"][-1]["coords"][-1]
    return {"lat": c[0], "lon": c[1], "timestamp": c[2]}


def connect(segments: list[dict[str, Any]], strength: str, keep: Any = None,
            force: Any = None) -> list[dict[str, Any]]:
    """Fill the seam between consecutive segments, attached to the moving side.

    Segments carrying only "via" fixes (a lone fix between two stays) are not
    kept as segments; their fixes become waypoints of the seam they sit in.
    """
    real: list[dict[str, Any]] = []
    vias: list[dict[str, Any]] = []
    via_modes: set[str] = set()
    for t in segments:
        if t.get("via"):
            vias.extend(t["via"])
            via_modes.add(t["mode"])
            continue
        if not t["pieces"]:
            continue
        if not real:
            if vias:
                mode = seam_mode({t["mode"], *via_modes})
                pc = _filled(_fixpt(vias[0]), vias[1:], _end(t, True), mode, strength, len(vias), None, keep, force)
                t["pieces"] = merge_pieces([pc] + t["pieces"])
        else:
            s = real[-1]
            a, b = _end(s, False), _end(t, True)
            if vias or abs(a["lat"] - b["lat"]) > 1e-9 or abs(a["lon"] - b["lon"]) > 1e-9:
                modes = {s["mode"], t["mode"], *via_modes}
                moving = modes - {"stay", "raw"}
                if moving:
                    mode = seam_mode(modes)
                    vmax = max(VMAX_KMH.get(m, 16.0) for m in moving)
                else:
                    # Between two stays with nothing recorded: the mode is
                    # unknown, so judge it by distance and allow any speed.
                    chord = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                    mode = "walk" if chord < 1500 else "drive"
                    vmax = VMAX_KMH["drive"]
                pc = _filled(a, vias, b, mode, strength, len(vias), vmax, keep, force)
                if t["mode"] == "stay" and s["mode"] != "stay":
                    s["pieces"] = merge_pieces(s["pieces"] + [pc])
                else:
                    t["pieces"] = merge_pieces([pc] + t["pieces"])
        vias, via_modes = [], set()
        real.append(t)
    if vias:
        if real:
            s = real[-1]
            mode = seam_mode({s["mode"], *via_modes})
            pc = _filled(_end(s, False), vias[:-1], _fixpt(vias[-1]), mode, strength, len(vias))
            s["pieces"] = merge_pieces(s["pieces"] + [pc])
        else:
            real.append({"mode": "raw", "forced": False, "profile": None, "n_fixes": len(vias),
                         "pieces": raw_pieces(vias)})
    return real


def merge_same_mode(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in segments:
        if out and out[-1]["mode"] == s["mode"] and s["mode"] != "stay" and \
                out[-1].get("key") == s.get("key"):
            q = out[-1]
            q["pieces"] = merge_pieces(q["pieces"] + s["pieces"])
            q["n_fixes"] += s["n_fixes"]
        else:
            out.append(s)
    return out


def finish(segments: list[dict[str, Any]]) -> None:
    for k, s in enumerate(segments):
        s["id"] = k
        coords = [c for p in s["pieces"] for c in p["coords"]]
        s["t0"] = int(round(coords[0][2])) if coords else 0
        s["t1"] = int(round(coords[-1][2])) if coords else 0
        km = {}
        for p in s["pieces"]:
            km[p["kind"]] = km.get(p["kind"], 0.0) + p["km"]
        total = sum(km.values())
        s["km"] = round(total, 3)
        s["provenance"] = {k: round(v / total, 3) for k, v in km.items()} if total > 0 else {}


# ---------------------------------------------------------------- stream
def stream(segments: list[dict[str, Any]], accuracy: float = 5.0) -> list[dict[str, Any]]:
    """One point list for Dawarich: strictly increasing whole seconds."""
    out: list[dict[str, Any]] = []
    for s in segments:
        for p in s["pieces"]:
            for lat, lon, ts, fix in p["coords"]:
                t = int(round(ts))
                if out:
                    q = out[-1]
                    if abs(q["lat"] - lat) < 1e-9 and abs(q["lon"] - lon) < 1e-9:
                        continue
                    if t <= q["timestamp"]:
                        if not fix:
                            continue
                        # A real fix wins over interpolated vertices before it.
                        while out and not out[-1]["_fix"] and out[-1]["timestamp"] >= t:
                            out.pop()
                        if out and out[-1]["timestamp"] >= t:
                            t = out[-1]["timestamp"] + 1
                    elif not fix:
                        dt = t - q["timestamp"]
                        if haversine(q["lat"], q["lon"], lat, lon) / dt * 3.6 > SYNTH_MAX_KMH:
                            continue
                out.append({"lat": lat, "lon": lon, "timestamp": t, "_fix": bool(fix),
                            "mode": s["mode"], "kind": p["kind"]})
    for p in out:
        p["inserted"] = not p.pop("_fix")
        p["accuracy"] = accuracy
    return out
