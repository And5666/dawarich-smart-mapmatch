"""Railway networks from OpenStreetMap, for trains anywhere.

The GB timetable shapes (rail.py) cover Great Britain only. Anywhere else a
train is matched on a railway network built from an OpenStreetMap extract
(mapmatch.tools.railways): every running line as a graph. A train journey is
snapped fix by fix to the nearest track and routed along the rails between
consecutive fixes, so it follows the line through junctions and stations.

Networks are files named railways-*.json.gz:
    {"name": str, "nodes": [[lat, lon], ...], "ways": [[node, ...], ...],
     "stations": [[lat, lon], ...]}
read from RAIL_DATA_DIR (shipped with mapmatch) and from any directory added
with configure() (the portal's own data directory, where an admin builds them).
"""
from __future__ import annotations

import glob
import gzip
import heapq
import json
import math
import os
import threading
from typing import Any

from .geo import cumulative

CELL = 0.01
SNAP_M = 110.0          # a fix further than this from any track is not on the railway
R = 6371008.8
PATTERN = "railways-*.json.gz"

_dirs: list[str] = []
_nets: list["Net"] | None = None
_lock = threading.Lock()


def _hav(a: float, b: float, c: float, d: float) -> float:
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(x)))


def _project(lat: float, lon: float, a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float]:
    kx = 111320.0 * math.cos(math.radians(lat))
    ky = 110540.0
    ax, ay, bx, by = a[1] * kx, a[0] * ky, b[1] * kx, b[0] * ky
    px, py = lon * kx, lat * ky
    dx, dy = bx - ax, by - ay
    s2 = dx * dx + dy * dy
    t = 0.0 if s2 <= 1e-9 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / s2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


class Net:
    def __init__(self, path: str, raw: dict[str, Any]):
        self.path = path
        self.name = str(raw.get("name") or os.path.basename(path))
        self.built = str(raw.get("built") or "")
        self.nodes: list[tuple[float, float]] = [(float(a), float(b)) for a, b in raw["nodes"]]
        self.stations: list[tuple[float, float]] = [(float(a), float(b)) for a, b in raw.get("stations", [])]
        self.adj: list[list[tuple[int, float]]] = [[] for _ in self.nodes]
        self.index: dict[tuple[int, int], list[tuple[int, int]]] = {}
        self.km = 0.0
        for way in raw["ways"]:
            for a, b in zip(way, way[1:]):
                if a == b:
                    continue
                pa, pb = self.nodes[a], self.nodes[b]
                d = _hav(pa[0], pa[1], pb[0], pb[1])
                self.adj[a].append((b, d))
                self.adj[b].append((a, d))
                self.km += d / 1000.0
                cell = (int((pa[0] + pb[0]) / 2 / CELL), int((pa[1] + pb[1]) / 2 / CELL))
                self.index.setdefault(cell, []).append((a, b))

    def nearest(self, lat: float, lon: float) -> dict[str, Any] | None:
        ci, cj = int(lat / CELL), int(lon / CELL)
        best = None
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b in self.index.get((ci + di, cj + dj), ()):
                    d, t = _project(lat, lon, self.nodes[a], self.nodes[b])
                    if best is None or d < best[0]:
                        best = (d, a, b, t)
        if best is None:
            return None
        d, a, b, t = best
        pa, pb = self.nodes[a], self.nodes[b]
        return {"net": self, "a": a, "b": b, "t": t, "dist": d,
                "lat": pa[0] + (pb[0] - pa[0]) * t, "lon": pa[1] + (pb[1] - pa[1]) * t}

    def near(self, lat: float, lon: float, reach: float, most: int = 6) -> list[dict[str, Any]]:
        """Snaps on the tracks within `reach`, nearest first: at a station
        several lines pass, and only one of them goes where the train went."""
        ci, cj = int(lat / CELL), int(lon / CELL)
        found = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for a, b in self.index.get((ci + di, cj + dj), ()):
                    d, t = _project(lat, lon, self.nodes[a], self.nodes[b])
                    if d <= reach:
                        found.append((d, a, b, t))
        found.sort()
        out: list[dict[str, Any]] = []
        for d, a, b, t in found:
            pa, pb = self.nodes[a], self.nodes[b]
            la, lo = pa[0] + (pb[0] - pa[0]) * t, pa[1] + (pb[1] - pa[1]) * t
            # The same spot twice (two segments of one track meeting at a vertex) is one candidate.
            # Parallel tracks, 3-4 m apart, are different candidates.
            if any(_hav(la, lo, o["lat"], o["lon"]) < 1.5 for o in out):
                continue
            out.append({"net": self, "a": a, "b": b, "t": t, "dist": d, "lat": la, "lon": lo})
            if len(out) >= most:
                break
        return out

    def route(self, s: dict[str, Any], g: dict[str, Any], limit_m: float) -> list[tuple[float, float]] | None:
        """Track positions strictly between two snaps, along the rails; None
        when the rails don't join them within limit_m."""
        if {s["a"], s["b"]} == {g["a"], g["b"]}:
            return []
        la = _hav(*self.nodes[s["a"]], *self.nodes[s["b"]])
        lb = _hav(*self.nodes[g["a"]], *self.nodes[g["b"]])
        goal = {g["a"]: g["t"] * lb, g["b"]: (1 - g["t"]) * lb}
        dist = {s["a"]: s["t"] * la, s["b"]: (1 - s["t"]) * la}
        prev: dict[int, int] = {}
        heap = [(c, n) for n, c in dist.items()]
        heapq.heapify(heap)
        best: tuple[float, int] | None = None
        while heap:
            c, n = heapq.heappop(heap)
            if c > dist.get(n, 1e18) or c > limit_m:
                continue
            if best is not None and c >= best[0]:
                break
            if n in goal and (best is None or c + goal[n] < best[0]):
                best = (c + goal[n], n)
            for m, w in self.adj[n]:
                nc = c + w
                if nc < dist.get(m, 1e18) and nc <= limit_m:
                    dist[m] = nc
                    prev[m] = n
                    heapq.heappush(heap, (nc, m))
        if best is None:
            return None
        path = [best[1]]
        while path[-1] in prev:
            path.append(prev[path[-1]])
        return [self.nodes[k] for k in reversed(path)]


def configure(*dirs: Any) -> None:
    """Also read networks from these directories (the portal's data dir)."""
    global _nets
    with _lock:
        for d in dirs:
            d = str(d)
            if d and d not in _dirs:
                _dirs.append(d)
        _nets = None


def reload() -> None:
    global _nets
    with _lock:
        _nets = None


def _shipped_dir() -> str:
    return os.environ.get("RAIL_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")


def files() -> list[str]:
    out: list[str] = []
    for d in [_shipped_dir(), *_dirs]:
        out += sorted(glob.glob(os.path.join(d, PATTERN)))
    return out


def networks() -> list[Net]:
    global _nets
    if _nets is not None:
        return _nets
    with _lock:
        if _nets is None:
            nets = []
            for path in files():
                try:
                    with gzip.open(path, "rt") as f:
                        nets.append(Net(path, json.load(f)))
                except (OSError, ValueError, KeyError):
                    continue     # a broken or half-written file is skipped, not fatal
            _nets = nets
    return _nets


def nearest(lat: float, lon: float) -> dict[str, Any] | None:
    best = None
    for net in networks():
        n = net.nearest(lat, lon)
        if n and (best is None or n["dist"] < best["dist"]):
            best = n
    return best


def between(lat_a: float, lon_a: float, lat_b: float, lon_b: float,
            reach: float = 200.0) -> list[tuple[float, float]] | None:
    """The rails from a to b: the shortest way along one network between any
    track within `reach` of each end, or None. For a ride with nothing
    recorded on the way (the Underground, a tunnel)."""
    chord = _hav(lat_a, lon_a, lat_b, lon_b)
    best: tuple[float, list[tuple[float, float]]] | None = None
    # A big station has many tracks of several lines within reach: all of them are tried,
    # since only one line goes where the train went.
    for net in networks():
        for sa in net.near(lat_a, lon_a, reach, BETWEEN_CANDIDATES):
            for sb in net.near(lat_b, lon_b, reach, BETWEEN_CANDIDATES):
                path = net.route(sa, sb, max(2000.0, 2.0 * chord + 1000.0))
                if path is None:
                    continue
                line = [(sa["lat"], sa["lon"]), *path, (sb["lat"], sb["lon"])]
                length = cumulative(line)[-1] + sa["dist"] + sb["dist"]
                if best is None or length < best[0]:
                    best = (length, line)
    return best[1] if best else None


def station_distance(lat: float, lon: float) -> float:
    return min((_hav(lat, lon, s[0], s[1]) for net in networks() for s in net.stations), default=1e9)


def distance(lat: float, lon: float) -> float:
    n = nearest(lat, lon)
    return n["dist"] if n else 1e9


CANDIDATES = 6           # tracks tried per fix
BETWEEN_CANDIDATES = 20  # tracks tried at each end of a ride with nothing recorded between
DETOUR_FREE_M = 150.0    # rails longer than the straight hop by this much cost nothing (curves)


def _route_len(net: "Net", s: dict[str, Any], g: dict[str, Any], limit: float) -> tuple[float, list[tuple[float, float]]] | None:
    """Length along the rails from one snap to another, and the track positions between them."""
    path = net.route(s, g, limit)
    if path is None:
        return None
    line = [(s["lat"], s["lon"]), *path, (g["lat"], g["lon"])]
    return cumulative(line)[-1], path


def match(fixes: list[dict[str, Any]]) -> dict[str, Any]:
    """Match a train journey on the railway networks. Same shape as the road
    matcher's result: pieces of track, and per fix an anchor on one of them.

    A main line is several tracks a few metres apart, joined only at
    crossovers. Snapping each fix to its nearest track and routing between
    them sends the line through a crossover and back whenever two fixes fall
    on different tracks (seen as 38 km of line for a 30 km journey, in seven
    pieces). So each fix gets several candidate tracks and the journey is the
    sequence of candidates whose rails are nearest in length to the ground
    covered between the fixes: one track followed, as a train does."""
    n = len(fixes)
    anchors: list[dict[str, Any] | None] = [None] * n
    pieces: list[dict[str, Any]] = []
    cands: list[list[dict[str, Any]]] = []
    for p in fixes:
        near = nearest(p["lat"], p["lon"])
        cands.append(near["net"].near(p["lat"], p["lon"], SNAP_M, CANDIDATES) if near and near["dist"] <= SNAP_M else [])
    idx = [k for k in range(n) if cands[k]]

    def emit(run: list[tuple[int, dict[str, Any], list[tuple[float, float]]]]) -> None:
        """One unbroken stretch of track: [(fix index, snap, rails from the previous snap)]."""
        poly: list[tuple[float, float]] = []
        at: list[tuple[int, int]] = []
        for k, c, path in run:
            here = (c["lat"], c["lon"])
            if poly and _hav(poly[-1][0], poly[-1][1], here[0], here[1]) < 15.0 and not path:
                at.append((k, len(poly) - 1))        # standing still, or GPS jitter: no new track position
                continue
            poly.extend(path)
            poly.append(here)
            at.append((k, len(poly) - 1))
        if len(poly) < 2:
            return
        cum = cumulative(poly)
        for k, pos in at:
            lat, lon = poly[pos]
            anchors[k] = {"piece": len(pieces), "arc": cum[pos], "lat": lat, "lon": lon,
                          "dist": _hav(lat, lon, fixes[k]["lat"], fixes[k]["lon"]), "kind": "rail"}
        pieces.append({"poly": poly, "cum": cum})

    start = 0
    while start < len(idx):
        # cost[j], and for each fix the candidate it came from and the rails between
        k0 = idx[start]
        cost = [c["dist"] for c in cands[k0]]
        back: list[list[tuple[int, list[tuple[float, float]]] | None]] = [[None] * len(cands[k0])]
        end = start
        while end + 1 < len(idx):
            kp, kc = idx[end], idx[end + 1]
            hop = _hav(fixes[kp]["lat"], fixes[kp]["lon"], fixes[kc]["lat"], fixes[kc]["lon"])
            limit = max(2000.0, 2.0 * hop + 1000.0)
            new_cost: list[float] = []
            new_back: list[tuple[int, list[tuple[float, float]]] | None] = []
            for g in cands[kc]:
                best: tuple[float, int, list[tuple[float, float]]] | None = None
                for i, s in enumerate(cands[kp]):
                    if s["net"] is not g["net"] or cost[i] >= 1e17:
                        continue
                    if _hav(s["lat"], s["lon"], g["lat"], g["lon"]) < 15.0 and hop < 30.0:
                        length, path = 0.0, []                       # not moving
                    else:
                        r = _route_len(s["net"], s, g, limit)
                        if r is None:
                            continue
                        length, path = r
                    c = cost[i] + max(0.0, abs(length - hop) - DETOUR_FREE_M) + g["dist"]
                    if best is None or c < best[0]:
                        best = (c, i, path)
                new_cost.append(best[0] if best else 1e18)
                new_back.append((best[1], best[2]) if best else None)
            if min(new_cost) >= 1e17:
                break                                               # the rails do not join here: a new piece
            cost = new_cost
            back.append(new_back)
            end += 1
        # walk back from the cheapest end
        j = min(range(len(cost)), key=lambda x: cost[x])
        run: list[tuple[int, dict[str, Any], list[tuple[float, float]]]] = []
        for step in range(end, start - 1, -1):
            k = idx[step]
            link = back[step - start][j]
            run.append((k, cands[k][j], link[1] if link else []))
            if link is None:
                break
            j = link[0]
        run.reverse()
        emit(run)
        start = end + 1
    return {"pieces": pieces, "anchors": anchors, "profile": "rail"}
