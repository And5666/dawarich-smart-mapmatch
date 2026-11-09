"""Geodesy and polyline helpers. Standard library only."""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

R = 6371008.8


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _decode_chunk(s: str, index: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = ord(s[index]) - 63
        index += 1
        result |= (b & 0x1F) << shift
        shift += 5
        if b < 0x20:
            break
    delta = ~(result >> 1) if result & 1 else result >> 1
    return delta, index


def decode_polyline(s: str, precision: int = 6) -> list[tuple[float, float]]:
    if not s:
        return []
    factor = 10 ** precision
    lat = lon = index = 0
    out: list[tuple[float, float]] = []
    n = len(s)
    while index < n:
        dlat, index = _decode_chunk(s, index)
        dlon, index = _decode_chunk(s, index)
        lat += dlat
        lon += dlon
        out.append((lat / factor, lon / factor))
    return out


def cumulative(points: list[tuple[float, float]]) -> list[float]:
    if not points:
        return []
    out = [0.0]
    for (a_lat, a_lon), (b_lat, b_lon) in zip(points, points[1:]):
        out.append(out[-1] + haversine(a_lat, a_lon, b_lat, b_lon))
    return out


def polyline_length(points: list[tuple[float, float]]) -> float:
    return cumulative(points)[-1] if points else 0.0


def _xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    return (math.radians(lon - lon0) * math.cos(math.radians(lat0)) * R,
            math.radians(lat - lat0) * R)


def point_at(poly: list[tuple[float, float]], cum: list[float], arc: float) -> tuple[float, float, float]:
    if not poly:
        raise ValueError("empty polyline")
    if len(poly) == 1:
        return poly[0][0], poly[0][1], 0.0
    if arc <= 0:
        return poly[0][0], poly[0][1], bearing(poly[0][0], poly[0][1], poly[1][0], poly[1][1])
    if arc >= cum[-1]:
        a, b = poly[-2], poly[-1]
        return b[0], b[1], bearing(a[0], a[1], b[0], b[1])
    lo, hi = 0, len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= arc:
            lo = mid
        else:
            hi = mid
    span = cum[hi] - cum[lo] or 1e-9
    t = (arc - cum[lo]) / span
    a, b = poly[lo], poly[hi]
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            bearing(a[0], a[1], b[0], b[1]))


def project_arc(poly: list[tuple[float, float]], cum: list[float], lat: float, lon: float,
                lo_arc: float | None = None, hi_arc: float | None = None) -> tuple[float, float]:
    """Nearest arc and perpendicular distance, optionally within a cum window."""
    if len(poly) < 2:
        return 0.0, haversine(lat, lon, poly[0][0], poly[0][1]) if poly else 1e9
    lo = 0 if lo_arc is None else max(0, bisect_left(cum, lo_arc) - 2)
    hi = len(poly) - 1 if hi_arc is None else min(len(poly) - 1, bisect_right(cum, hi_arc) + 2)
    if hi <= lo:
        lo, hi = 0, len(poly) - 1
    lat0, lon0 = poly[lo]
    px, py = _xy(lat, lon, lat0, lon0)
    best_d = 1e30
    best_arc = cum[lo]
    for i in range(lo, hi):
        ax, ay = _xy(poly[i][0], poly[i][1], lat0, lon0)
        bx, by = _xy(poly[i + 1][0], poly[i + 1][1], lat0, lon0)
        dx, dy = bx - ax, by - ay
        s2 = dx * dx + dy * dy
        t = 0.0 if s2 <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / s2))
        qx, qy = ax + t * dx, ay + t * dy
        d = math.hypot(px - qx, py - qy)
        if d < best_d:
            best_d = d
            best_arc = cum[i] + t * (cum[i + 1] - cum[i])
    return best_arc, best_d


def subline(poly: list[tuple[float, float]], cum: list[float], a: float, b: float) -> list[tuple[float, float]]:
    """Polyline between two arcs, inclusive, following vertices in between."""
    if not poly or b < a - 1.0:
        return []
    a = max(0.0, min(a, cum[-1]))
    b = max(0.0, min(b, cum[-1]))
    out: list[tuple[float, float]] = []
    out.append(point_at(poly, cum, a)[:2])
    i = bisect_right(cum, a)
    while i < len(poly) and cum[i] < b - 1e-6:
        out.append(poly[i])
        i += 1
    out.append(point_at(poly, cum, b)[:2])
    return out


def dist_point_segment(lat: float, lon: float, a_lat: float, a_lon: float,
                       b_lat: float, b_lon: float) -> float:
    lat0 = math.radians(lat)
    kx = 111320.0 * math.cos(lat0)
    ax, ay = a_lon * kx, a_lat * 110540.0
    bx, by = b_lon * kx, b_lat * 110540.0
    px, py = lon * kx, lat * 110540.0
    dx, dy = bx - ax, by - ay
    s2 = dx * dx + dy * dy
    t = 0.0 if s2 <= 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / s2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))
