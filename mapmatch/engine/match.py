"""Match one mode segment to the network.

Returns continuous pieces of geometry and, per fix, an anchor on one piece
(or None). Geometry between anchors on the same piece is the matched path;
everything else is left for fill.py.
"""
from __future__ import annotations

from typing import Any

from . import rail, railnet, valhalla
from .geo import cumulative, haversine, point_at, project_arc

PROFILE = {"walk": "pedestrian", "cycle": "bicycle", "drive": "auto"}
CHUNK = 200
OVERLAP = 6
RUN_GAP_S = 600
MIN_SPLIT = 6
# Share of fixes the narrow search (fixes without an accuracy) may leave
# unmatched before the chunk is also tried at the profile's wide radius.
WIDE_RETRY_UNMATCHED = 0.2


def _runs_by_gap(fixes: list[dict[str, Any]], profile: str) -> list[tuple[int, int]]:
    """Split where Valhalla would break anyway: long time or distance gaps."""
    brk = valhalla.breakage(profile)
    out, s = [], 0
    for i in range(1, len(fixes)):
        a, b = fixes[i - 1], fixes[i]
        if b["timestamp"] - a["timestamp"] > RUN_GAP_S or \
                haversine(a["lat"], a["lon"], b["lat"], b["lon"]) > brk:
            out.append((s, i))
            s = i
    out.append((s, len(fixes)))
    return out


def _narrow_or_wide(fixes: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """GPX-style chunks are searched narrowly first (see
    valhalla.NO_ACCURACY_RADIUS); if that loses much of the trace, the wide
    search is tried too and the better fit kept."""
    if not valhalla.unreported(fixes):
        return valhalla.match(fixes, profile)
    try:
        res = valhalla.match(fixes, profile)
    except valhalla.ValhallaError:
        return valhalla.match(fixes, profile, wide=True)
    lost = sum(1 for a in res["anchors"] if a is None) / max(1, len(fixes))
    if lost <= WIDE_RETRY_UNMATCHED:
        return res
    try:
        wide = valhalla.match(fixes, profile, wide=True)
    except valhalla.ValhallaError:
        return res
    return wide if fit_score(fixes, wide) > fit_score(fixes, res) else res


def _match_chunk(fixes: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Match, halving the chunk when Valhalla cannot snap it at all."""
    if len(fixes) < 2:
        return {"pieces": [], "anchors": [None] * len(fixes)}
    try:
        return _narrow_or_wide(fixes, profile)
    except valhalla.ValhallaError:
        if len(fixes) <= MIN_SPLIT:
            return {"pieces": [], "anchors": [None] * len(fixes)}
        mid = len(fixes) // 2
        left = _match_chunk(fixes[:mid], profile)
        right = _match_chunk(fixes[mid:], profile)
        off = len(left["pieces"])
        ra = [None if a is None else {**a, "piece": a["piece"] + off} for a in right["anchors"]]
        return {"pieces": left["pieces"] + right["pieces"], "anchors": left["anchors"] + ra}


def _match_run(fixes: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Chunk long runs with overlap and splice at the overlap midpoint."""
    n = len(fixes)
    if n <= CHUNK:
        return _match_chunk(fixes, profile)
    pieces: list[dict[str, Any]] = []
    anchors: list[dict[str, Any] | None] = [None] * n
    start = 0
    keep_from = 0
    while start < n:
        end = min(n, start + CHUNK)
        res = _match_chunk(fixes[start:end], profile)
        off = len(pieces)
        pieces.extend(res["pieces"])
        keep_to = end if end >= n else end - OVERLAP // 2
        for k in range(keep_from, keep_to):
            a = res["anchors"][k - start]
            anchors[k] = None if a is None else {**a, "piece": a["piece"] + off}
        if end >= n:
            break
        keep_from = keep_to
        start = end - OVERLAP
    return {"pieces": pieces, "anchors": anchors}


def match_road(fixes: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    profile = PROFILE[mode]
    pieces: list[dict[str, Any]] = []
    anchors: list[dict[str, Any] | None] = []
    for s, e in _runs_by_gap(fixes, profile):
        res = _match_run(fixes[s:e], profile)
        off = len(pieces)
        pieces.extend(res["pieces"])
        anchors.extend(None if a is None else {**a, "piece": a["piece"] + off} for a in res["anchors"])
    return {"pieces": pieces, "anchors": anchors, "profile": profile}


def match_rail(fixes: list[dict[str, Any]]) -> dict[str, Any]:
    """A train: on the GB timetable shapes where they explain it, otherwise on
    the OpenStreetMap railway networks (railnet), whichever fits better."""
    gb = match_gtfs(fixes)
    try:
        osm = railnet.match(fixes)
    except (OSError, ValueError):
        osm = {"pieces": [], "anchors": [None] * len(fixes), "profile": "rail"}
    if not any(osm["anchors"]):
        return gb
    if not any(gb["anchors"]):
        return osm
    # The timetable shape is one clean line; prefer it unless clearly worse.
    return osm if fit_score(fixes, osm) > fit_score(fixes, gb) + 0.1 else gb


def match_gtfs(fixes: list[dict[str, Any]]) -> dict[str, Any]:
    """Project the whole segment onto the single rail alignment that explains it.

    Ported from smartmap.infer.rail_candidates: per-fix nearest shapes jump
    between parallel route patterns, so score whole shapes by coverage and by
    how well their arc span matches the travelled distance.
    """
    empty = {"pieces": [], "anchors": [None] * len(fixes), "profile": "rail"}
    try:
        nearest = [rail.nearest(p["lat"], p["lon"]) for p in fixes]
    except FileNotFoundError:
        return empty
    counts: dict[int, int] = {}
    for nr in nearest:
        if nr and nr["dist"] <= 110:
            counts[nr["shape"]] = counts.get(nr["shape"], 0) + 1
    if not counts:
        return empty
    path = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(fixes, fixes[1:]))
    best = None
    for shape in sorted(counts, key=lambda s: -counts[s])[:25]:
        poly, cum = rail.shape_line(shape), rail.shape_cum(shape)
        arcs: list[float | None] = []
        dists = []
        for p in fixes:
            arc, d = project_arc(poly, cum, p["lat"], p["lon"])
            if d > 110:
                arcs.append(None)
            else:
                arcs.append(arc)
                dists.append(d)
        got = [a for a in arcs if a is not None]
        if len(got) < 2:
            continue
        span = abs(got[-1] - got[0])
        ratio = span / max(1.0, path)
        if not 0.5 <= ratio <= 1.4:
            continue
        med = sorted(dists)[len(dists) // 2]
        score = len(got) / len(fixes) - 0.25 * abs(ratio - 1.0) - 0.0003 * med
        if best is None or score > best[0]:
            best = (score, poly, cum, arcs, dists)
    if best is None:
        return empty
    _, poly, cum, arcs, _ = best
    got = [a for a in arcs if a is not None]
    if got[-1] < got[0]:
        # Travel runs against the shape's direction: flip it.
        total = cum[-1]
        poly = list(reversed(poly))
        cum = cumulative(poly)
        arcs = [None if a is None else total - a for a in arcs]
    anchors: list[dict[str, Any] | None] = []
    last = 0.0
    for p, arc in zip(fixes, arcs):
        if arc is None:
            anchors.append(None)
            continue
        arc = max(arc, last)
        last = arc
        lat, lon, _ = point_at(poly, cum, arc)
        anchors.append({"piece": 0, "arc": arc, "lat": lat, "lon": lon,
                        "dist": haversine(lat, lon, p["lat"], p["lon"]), "kind": "rail"})
    return {"pieces": [{"poly": poly, "cum": cum}], "anchors": anchors, "profile": "rail"}


def match_segment(fixes: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    if mode == "train":
        return match_rail(fixes)
    return match_road(fixes, mode)


def fit_score(fixes: list[dict[str, Any]], m: dict[str, Any]) -> float:
    """Higher is better: coverage, closeness, and path length agreement."""
    anchors = m["anchors"]
    got = [a for a in anchors if a]
    if not got:
        return -1.0
    cov = len(got) / max(1, len(fixes))
    med = sorted(a["dist"] for a in got)[len(got) // 2]
    return cov - 0.01 * med
