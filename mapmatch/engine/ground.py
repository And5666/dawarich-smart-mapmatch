"""What the ground is, from the offline basemap: water, buildings and bridges.

The map-matcher knows ways, not what lies between them. A fix out in a harbour, a
straight line through a block of flats and a walk along a floating pontoon (a
footway, on the map) all look fine to it. The basemap the portal already draws
(a Protomaps PMTiles file) says where the water and the buildings are, so the
same file is read here: point MAPMATCH_GROUND_PMTILES at it. Without it every
question below is answered "no", and nothing changes.

Pure Python: a PMTiles v3 reader and just enough of the vector-tile format for
polygons and lines. Tiles are read at the file's highest zoom (15 at most) and
kept decoded, a few hundred at a time.
"""
from __future__ import annotations

import bisect
import gzip
import math
import os
import struct
from collections import OrderedDict
from typing import Any

BRIDGE_M = 12.0            # this close to a bridge or tunnel line, water is not water
NOT_WATER = ("fountain", "swimming_pool")   # blue on the map, but walked across or beside
INSIDE_M = 20.0            # a straight step with this much of it under roofs goes through buildings
STEP_M = 5.0
MAX_ZOOM = 15
CACHE = 400

_path: str | None = None
_file: Any = None
_tiles: "OrderedDict[tuple[int, int], Any]" = OrderedDict()


def configure(path: str | None) -> None:
    global _path, _file
    _path = path or None
    _file = None
    _tiles.clear()


def _open() -> Any:
    global _file, _path
    if _file is None:
        path = _path if _path is not None else os.environ.get("MAPMATCH_GROUND_PMTILES", "")
        _path = path
        try:
            _file = _Archive(path) if path else False
        except (OSError, ValueError, AssertionError):
            _file = False
    return _file


def available() -> bool:
    return bool(_open())


# ------------------------------------------------------------------ PMTiles v3
def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        if c < 0x80:
            return r, i
        s += 7


def _tile_id(z: int, x: int, y: int) -> int:
    acc = sum((1 << t) * (1 << t) for t in range(z))
    d = 0
    s = (1 << z) // 2
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s //= 2
    return acc + d


class _Archive:
    def __init__(self, path: str) -> None:
        self.f = open(path, "rb")
        h = self.f.read(127)
        if h[:7] != b"PMTiles" or h[7] != 3:
            raise ValueError("not a PMTiles v3 file")
        (self.root_off, self.root_len, _, _, self.leaf_off, _, self.data_off, _) = struct.unpack_from("<8Q", h, 8)
        self.internal, self.tilecomp, tiletype, _, maxz = h[97], h[98], h[99], h[100], h[101]
        if tiletype != 1 or self.internal not in (1, 2) or self.tilecomp not in (1, 2):
            raise ValueError("not gzip or plain vector tiles")
        self.z = min(maxz, MAX_ZOOM)
        self.dirs: dict[tuple[int, int], Any] = {}

    def _read(self, off: int, n: int) -> bytes:
        self.f.seek(off)
        return self.f.read(n)

    def _dir(self, off: int, n: int) -> Any:
        d = self.dirs.get((off, n))
        if d is None:
            b = self._read(off, n)
            if self.internal == 2:
                b = gzip.decompress(b)
            cnt, i = _varint(b, 0)
            cols: list[list[int]] = []
            for c in range(4):
                col = []
                last = 0
                for k in range(cnt):
                    v, i = _varint(b, i)
                    if c == 0:
                        last += v
                        v = last
                    col.append(v)
                cols.append(col)
            ids, runs, lens, raw = cols
            offs: list[int] = []
            for k in range(cnt):
                offs.append(offs[k - 1] + lens[k - 1] if raw[k] == 0 and k > 0 else raw[k] - 1)
            d = (ids, runs, lens, offs)
            if len(self.dirs) > 64:
                self.dirs.clear()
            self.dirs[(off, n)] = d
        return d

    def tile(self, x: int, y: int) -> bytes | None:
        tid = _tile_id(self.z, x, y)
        off, n = self.root_off, self.root_len
        for _ in range(4):
            ids, runs, lens, offs = self._dir(off, n)
            k = bisect.bisect_right(ids, tid) - 1
            if k < 0:
                return None
            if runs[k] == 0:
                off, n = self.leaf_off + offs[k], lens[k]
                continue
            if tid - ids[k] >= runs[k]:
                return None
            b = self._read(self.data_off + offs[k], lens[k])
            return gzip.decompress(b) if self.tilecomp == 2 else b
        return None


# ---------------------------------------------------------------- vector tiles
def _fields(b: bytes) -> Any:
    i, n = 0, len(b)
    while i < n:
        key, i = _varint(b, i)
        w = key & 7
        if w == 0:
            v, i = _varint(b, i)
        elif w == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif w == 5:
            v = b[i:i + 4]
            i += 4
        elif w == 1:
            v = b[i:i + 8]
            i += 8
        else:
            raise ValueError("bad tile")
        yield key >> 3, v


def _value(b: bytes) -> Any:
    for f, v in _fields(b):
        if f == 1:
            return v.decode("utf8", "replace")
        if f == 7:
            return bool(v)
        return v
    return None


def _feature(b: bytes) -> tuple[int, list[int], list[list[tuple[int, int]]]]:
    tags: list[int] = []
    typ = 0
    geom: list[int] = []
    for f, v in _fields(b):
        if f == 2 or f == 4:
            out = []
            i = 0
            while i < len(v):
                x, i = _varint(v, i)
                out.append(x)
            if f == 2:
                tags = out
            else:
                geom = out
        elif f == 3:
            typ = v
    rings: list[list[tuple[int, int]]] = []
    cur: list[tuple[int, int]] = []
    x = y = i = 0
    while i < len(geom):
        cmd, cnt = geom[i] & 7, geom[i] >> 3
        i += 1
        if cmd == 7:
            continue
        for _ in range(cnt):
            x += (geom[i] >> 1) ^ -(geom[i] & 1)
            y += (geom[i + 1] >> 1) ^ -(geom[i + 1] & 1)
            i += 2
            if cmd == 1:
                if cur:
                    rings.append(cur)
                cur = [(x, y)]
            else:
                cur.append((x, y))
    if cur:
        rings.append(cur)
    return typ, tags, rings


GRID = 32


class _Tile:
    """Water and building polygons and bridge lines of one tile, in tile units, bucketed on a grid."""

    def __init__(self, data: bytes | None) -> None:
        self.extent = 4096
        self.water: list[list[int]] = [[] for _ in range(GRID * GRID)]
        self.roofs: list[list[int]] = [[] for _ in range(GRID * GRID)]
        self.polys: list[list[list[tuple[int, int]]]] = []
        self.bridges: list[tuple[int, int, int, int]] = []
        if not data:
            return
        for f, layer in _fields(data):
            if f != 3:
                continue
            name = ""
            feats: list[bytes] = []
            keys: list[str] = []
            vals: list[Any] = []
            for f2, v in _fields(layer):
                if f2 == 1:
                    name = v.decode()
                elif f2 == 2:
                    feats.append(v)
                elif f2 == 3:
                    keys.append(v.decode())
                elif f2 == 4:
                    vals.append(_value(v))
                elif f2 == 5:
                    self.extent = int(v)
            if name not in ("water", "buildings", "roads"):
                continue
            for fb in feats:
                typ, tags, rings = _feature(fb)
                props = {keys[tags[k]]: vals[tags[k + 1]] for k in range(0, len(tags) - 1, 2)}
                if name == "roads":
                    if typ == 2 and (props.get("is_bridge") is True or props.get("is_tunnel") is True):
                        for r in rings:
                            self.bridges += [(a[0], a[1], b[0], b[1]) for a, b in zip(r, r[1:])]
                    continue
                if typ != 3 or not rings or (name == "water" and props.get("kind") in NOT_WATER):
                    continue
                self._add(self.water if name == "water" else self.roofs, rings)

    def _add(self, grid: list[list[int]], rings: list[list[tuple[int, int]]]) -> None:
        pid = len(self.polys)
        self.polys.append(rings)
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        g = self.extent / GRID
        for gx in range(max(0, int(min(xs) // g)), min(GRID - 1, int(max(xs) // g)) + 1):
            for gy in range(max(0, int(min(ys) // g)), min(GRID - 1, int(max(ys) // g)) + 1):
                grid[gy * GRID + gx].append(pid)

    def _inside(self, grid: list[list[int]], x: float, y: float) -> bool:
        if not 0 <= x < self.extent or not 0 <= y < self.extent:
            return False
        g = self.extent / GRID
        for pid in grid[int(y // g) * GRID + int(x // g)]:
            odd = False
            for ring in self.polys[pid]:
                x0, y0 = ring[-1]
                for x1, y1 in ring:
                    if (y0 > y) != (y1 > y) and x < (x1 - x0) * (y - y0) / (y1 - y0) + x0:
                        odd = not odd
                    x0, y0 = x1, y1
            if odd:
                return True
        return False

    def near_bridge(self, x: float, y: float, reach: float) -> bool:
        r2 = reach * reach
        for x0, y0, x1, y1 in self.bridges:
            dx, dy = x1 - x0, y1 - y0
            t = 0.0 if not (dx or dy) else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
            ex, ey = x0 + t * dx - x, y0 + t * dy - y
            if ex * ex + ey * ey <= r2:
                return True
        return False


def _at(lat: float, lon: float) -> tuple[Any, float, float, float] | None:
    ar = _open()
    if not ar or not -85.0 < lat < 85.0:
        return None
    n = 1 << ar.z
    fx = (lon + 180.0) / 360.0 * n
    fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    key = (int(fx), int(fy))
    t = _tiles.get(key)
    if t is None:
        try:
            t = _Tile(ar.tile(*key))
        except (ValueError, IndexError, OSError, EOFError):
            t = _Tile(None)
        _tiles[key] = t
        if len(_tiles) > CACHE:
            _tiles.popitem(last=False)
    else:
        _tiles.move_to_end(key)
    per_m = t.extent / (40075016.686 * math.cos(math.radians(lat)) / n)     # tile units per metre
    return t, (fx - key[0]) * t.extent, (fy - key[1]) * t.extent, per_m


def wet(lat: float, lon: float) -> bool:
    """On water, and not on a bridge over it (or in a tunnel under it)."""
    at = _at(lat, lon)
    if not at:
        return False
    t, x, y, per_m = at
    return t._inside(t.water, x, y) and not t.near_bridge(x, y, BRIDGE_M * per_m)


def indoors(lat: float, lon: float) -> bool:
    at = _at(lat, lon)
    return bool(at) and at[0]._inside(at[0].roofs, at[1], at[2])


def blocked(lat0: float, lon0: float, lat1: float, lon1: float) -> str:
    """Why nobody went straight from one point to the other: "water" when the
    line crosses water, "buildings" when INSIDE_M of it or more is under roofs
    (clipping a corner is GPS; going through a terrace is not), else ""."""
    if not available():
        return ""
    d = math.hypot((lat1 - lat0) * 110540.0, (lon1 - lon0) * 111320.0 * math.cos(math.radians(lat0)))
    k = max(1, int(d // STEP_M))
    under = 0
    for i in range(1, k):
        lat, lon = lat0 + (lat1 - lat0) * i / k, lon0 + (lon1 - lon0) * i / k
        if wet(lat, lon):
            return "water"
        if indoors(lat, lon):
            under += 1
    return "buildings" if under * d / k >= INSIDE_M else ""
