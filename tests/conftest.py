"""Shared fixtures: a fake Valhalla and synthetic traces (no network)."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.pop("VALHALLA_CACHE_DIR", None)

from mapmatch.engine import valhalla  # noqa: E402

# Far out in the North Sea: no rail shapes nearby, so mode tests are not
# influenced by the GB rail index.
LAT0, LON0 = 54.0, 2.5
T0 = 1_780_000_000


def encode_polyline(points, precision=6):
    factor = 10 ** precision
    out = []
    plat = plon = 0
    for lat, lon in points:
        ilat, ilon = round(lat * factor), round(lon * factor)
        for d in (ilat - plat, ilon - plon):
            v = ~(d << 1) if d < 0 else d << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def offset(lat, lon, east_m, north_m):
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


def track(steps, start=(LAT0, LON0), t0=T0, dt=30, velocity=None, accuracy=8.0):
    """Fixes moving `steps` = [(east_m, north_m), ...] per interval."""
    lat, lon = start
    out = [{"lat": lat, "lon": lon, "timestamp": t0, "accuracy": accuracy,
            "velocity": velocity, "altitude": None, "tracker_id": "t"}]
    t = t0
    for e, n in steps:
        lat, lon = offset(lat, lon, e, n)
        t += dt
        out.append({"lat": lat, "lon": lon, "timestamp": t, "accuracy": accuracy,
                    "velocity": velocity, "altitude": None, "tracker_id": "t"})
    return out


class FakeValhalla:
    """Matches every point onto a 'road' through the points themselves, and
    routes as a straight line, unless told otherwise."""

    def __init__(self):
        self.calls = []
        self.unmatched = set()     # indices (within a request) to report unmatched
        self.route_detour = 1.0    # >1 makes routes longer than the chord
        self.fail_route = False
        self.no_map = False         # the server has no tiles here

    def __call__(self, path, body, timeout=120.0):
        self.calls.append(path)
        if path == "/trace_attributes":
            return self._trace(body)
        if path == "/route":
            return self._route(body)
        if path == "/locate":
            return [{"edges": [] if self.no_map else [{"way_id": 1}]} for _ in body["locations"]]
        raise AssertionError(path)

    def _trace(self, body):
        pts = [(p["lat"], p["lon"]) for p in body["shape"]]
        # Like Valhalla, the path breaks around unmatched points: no edge
        # touches them, so the shape indices jump and a new piece starts.
        edges, matched = [], []
        edge_from: dict[int, int] = {}
        edge_to: dict[int, int] = {}
        for i in range(len(pts) - 1):
            if i in self.unmatched or i + 1 in self.unmatched:
                continue
            edge_from[i] = edge_to[i + 1] = len(edges)
            edges.append({"begin_shape_index": i, "end_shape_index": i + 1,
                          "road_class": "residential", "use": "road"})
        for i, (lat, lon) in enumerate(pts):
            if i in self.unmatched or (i not in edge_from and i not in edge_to):
                matched.append({"type": "unmatched", "lat": lat, "lon": lon})
                continue
            ei, along = (edge_from[i], 0.0) if i in edge_from else (edge_to[i], 1.0)
            matched.append({"type": "matched", "lat": lat, "lon": lon, "edge_index": ei,
                            "distance_along_edge": along, "distance_from_trace_point": 0.0})
        return {"shape": encode_polyline(pts), "edges": edges, "matched_points": matched}

    def _route(self, body):
        if self.fail_route:
            raise valhalla.ValhallaError("HTTP 400: no route")
        locs = [(p["lat"], p["lon"]) for p in body["locations"]]
        shape = [locs[0]]
        for a, b in zip(locs, locs[1:]):
            if self.route_detour > 1.0:
                # A sideways bump that makes the route longer than the chord.
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                dist = math.hypot((b[0] - a[0]) * 111_320, (b[1] - a[1]) * 111_320 * math.cos(math.radians(a[0])))
                side = offset(mid[0], mid[1], 0, dist * (self.route_detour - 1.0))
                shape.append(side)
            shape.append(b)
        return {"trip": {"legs": [{"shape": encode_polyline(shape)}],
                         "summary": {"length": 0.0, "time": 0.0}}}


@pytest.fixture
def fake_valhalla(monkeypatch):
    fake = FakeValhalla()
    monkeypatch.setattr(valhalla, "post", fake)
    return fake
