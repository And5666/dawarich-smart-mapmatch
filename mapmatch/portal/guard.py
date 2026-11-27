"""The last check before Dawarich is changed.

A push builds what Dawarich should hold for a span: matched trips, tidied stops,
short links between them, and recorded fixes put back. This module checks the
result against the two things that may justify a point, and nothing else:

- what the portal shows: a matched point lies on the trip's line as drawn;
- what was recorded: a stop point sits where fixes were, between two fixes
  not too far apart in time; a fix put back is one in the archive; a link is
  a short step between two of the above, close in time and place.

It does not reuse the code that builds the points, so a mistake there (like
stop points stamped through six weeks with the phone off, or a walk invented
for an hour nobody logged) is refused here instead of reaching Dawarich. A
refused span is left exactly as it is.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any

from ..engine.geo import dist_point_segment, haversine

QUIET_S = 12 * 3600      # stop points never bridge more than this with nothing recorded
LINK_S = 900             # a link spans at most this long with nothing recorded
LINK_M = 1500.0          # and joins two places at most this far apart
LINK_ROUTE_M = 2.5 * LINK_M + 300.0   # how far a link's road may wander from its start
LINE_M = 30.0            # a matched point is at most this far off the line the portal draws
STOP_NEAR_M = 300.0      # a stop point is this close to a fix either side of it
NEAR_S = 900             # matched points are compared with the line drawn this close in time


def _line_index(coords: list[list[float]]) -> tuple[list[float], list[int], list[list[float]]]:
    order = sorted(range(len(coords)), key=lambda i: coords[i][2])
    return [coords[i][2] for i in order], order, coords


def _off_line(p: dict[str, Any], idx: tuple[list[float], list[int], list[list[float]]]) -> float:
    times, order, coords = idx
    ts = float(p["timestamp"])
    lo, hi = bisect_left(times, ts - NEAR_S), bisect_right(times, ts + NEAR_S)
    near = {order[k] for k in range(max(0, lo - 1), min(len(order), hi + 1))}
    best = float("inf")
    for i in near:
        for j in (i - 1, i):
            if 0 <= j and j + 1 < len(coords):
                a, b = coords[j], coords[j + 1]
                best = min(best, dist_point_segment(p["lat"], p["lon"], a[0], a[1], b[0], b[1]))
        if len(coords) == 1:
            best = min(best, haversine(p["lat"], p["lon"], coords[0][0], coords[0][1]))
    return best


def check(posts: list[dict[str, Any]], fixes: list[dict[str, Any]],
          lines: dict[int, tuple[int, int, list[list[float]]]], window: tuple[int, int]) -> list[str]:
    """Every reason `posts` must not be written (empty when they may).

    posts: what the push would write, each marked with `src` (match, stop, link)
    when it is ours. fixes: the recorded GPS of the span. lines: per pushed trip,
    its window and the coordinates the portal draws ([lat, lon, ts, fix], ...)."""
    a, b = window
    real = sorted(fixes, key=lambda p: p["timestamp"])
    times = [int(p["timestamp"]) for p in real]
    recorded = {(int(p["timestamp"]), round(p["lat"], 6), round(p["lon"], 6)) for p in real}
    anchors = sorted((p for p in posts if p.get("src") != "link"), key=lambda p: int(p["timestamp"]))
    anchor_ts = [int(p["timestamp"]) for p in anchors]
    idx = {tid: (s, e, _line_index(coords)) for tid, (s, e, coords) in lines.items()}
    problems: dict[str, int] = {}

    def bad(why: str) -> None:
        problems[why] = problems.get(why, 0) + 1

    for p in posts:
        ts = int(p["timestamp"])
        if not a - 1 <= ts <= b + 1:
            bad("a point outside the span being rewritten")
            continue
        if not p.get("ours"):
            if (ts, round(p["lat"], 6), round(p["lon"], 6)) not in recorded:
                bad("a point put back that was never recorded")
            continue
        src = p.get("src")
        if src == "match":
            own = [i for s, e, i in idx.values() if s <= ts <= e]
            if not own:
                bad("a matched point outside every pushed trip")
            elif min(_off_line(p, i) for i in own) > LINE_M:
                bad(f"a matched point more than {LINE_M:.0f} m off the line the portal shows")
        elif src == "stop":
            k = bisect_left(times, ts)
            if k < len(times) and times[k] == ts:
                side = [real[k]]
            elif 0 < k < len(times):
                side = [real[k - 1], real[k]]
                if times[k] - times[k - 1] > QUIET_S:
                    bad(f"a stop point in {(times[k] - times[k - 1]) / 3600:.0f} h with nothing recorded")
                    continue
            else:
                bad("a stop point before the first or after the last recorded fix")
                continue
            if min(haversine(p["lat"], p["lon"], q["lat"], q["lon"]) - 1.5 * (q.get("accuracy") or 0.0)
                   for q in side) > STOP_NEAR_M:
                bad("a stop point away from where the fixes were")
        elif src == "link":
            k = bisect_left(anchor_ts, ts)
            if not 0 < k < len(anchors):
                bad("a link with nothing on one side of it")
                continue
            before, after = anchors[k - 1], anchors[k]
            if anchor_ts[k] - anchor_ts[k - 1] > LINK_S:
                bad(f"a route guessed across {(anchor_ts[k] - anchor_ts[k - 1]) / 60:.0f} min with nothing recorded")
            elif haversine(before["lat"], before["lon"], after["lat"], after["lon"]) > LINK_M:
                bad("a route guessed between places too far apart")
            elif haversine(before["lat"], before["lon"], p["lat"], p["lon"]) > LINK_ROUTE_M:
                bad("a link that wanders far from where it starts")
        else:
            bad("a point of ours with no source")
    return [f"{why} (x{n})" if n > 1 else why for why, n in problems.items()]
