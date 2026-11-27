"""Remove fixes that must not steer matching.

Nothing is deleted from the record: dropped fixes are returned with a reason
so callers can show them. Matching only sees the kept fixes.
"""
from __future__ import annotations

import math
from typing import Any

from .geo import haversine

MAX_ACCURACY_M = 150.0
TELEPORT_KMH = 400.0
ISOLATED_S = 30 * 86400  # a fix a month away from both neighbours is a bad clock
MIN_TS = 946684800       # 2000-01-01


def _kmh(a: dict[str, Any], b: dict[str, Any]) -> float:
    dt = abs(b["timestamp"] - a["timestamp"])
    d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if dt <= 0:
        return 0.0 if d < 1 else 1e9
    return d / dt * 3.6


def clean(points: list[dict[str, Any]], jitter: bool = False
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (kept, dropped). Kept fixes are sorted copies; dropped carry `_drop`.

    jitter=True also drops to-and-fro hops (see dejitter). Matching asks for
    it; trip splitting does not, so trips stay cut exactly where they were."""
    pts = sorted((dict(p) for p in points), key=lambda p: (int(p["timestamp"]), p["lat"], p["lon"]))
    dropped: list[dict[str, Any]] = []

    def drop(p: dict[str, Any], why: str) -> None:
        p["_drop"] = why
        dropped.append(p)

    # Exact duplicates and same-second duplicates: keep the most accurate.
    out: list[dict[str, Any]] = []
    for p in pts:
        p["timestamp"] = int(p["timestamp"])
        if p["timestamp"] < MIN_TS:
            drop(p, "bad_time")
            continue
        acc = p.get("accuracy")
        if acc is not None and acc > MAX_ACCURACY_M:
            drop(p, "low_accuracy")
            continue
        if out and out[-1]["timestamp"] == p["timestamp"]:
            q = out[-1]
            if (p.get("accuracy") or 99.0) < (q.get("accuracy") or 99.0):
                out[-1] = p
                drop(q, "duplicate")
            else:
                drop(p, "duplicate")
            continue
        out.append(p)

    # A fix far in time from both neighbours is a bad clock, not a trip.
    kept: list[dict[str, Any]] = []
    for i, p in enumerate(out):
        before = p["timestamp"] - out[i - 1]["timestamp"] if i else None
        after = out[i + 1]["timestamp"] - p["timestamp"] if i + 1 < len(out) else None
        if len(out) > 1 and (before is None or before > ISOLATED_S) and (after is None or after > ISOLATED_S):
            drop(p, "bad_time")
            continue
        kept.append(p)
    out = kept

    # To-and-fro hops go first: between two false positions the true one looks
    # like the spike, and the spike test below would throw away the wrong fix.
    if jitter:
        out, hops = dejitter(out)
        dropped.extend(hops)
        out, hops = _side_steps(out)
        dropped.extend(hops)

    # Spikes: one fix that sticks out from an otherwise straight-ish path.
    changed = True
    while changed:
        changed = False
        kept = []
        for i, b in enumerate(out):
            if 0 < i < len(out) - 1:
                a, c = kept[-1] if kept else out[i - 1], out[i + 1]
                d1 = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                d2 = haversine(b["lat"], b["lon"], c["lat"], c["lon"])
                d3 = haversine(a["lat"], a["lon"], c["lat"], c["lon"])
                spike = d1 > 150 and d2 > 150 and d1 + d2 > 2.0 * d3 + 500
                tele = _kmh(a, b) > TELEPORT_KMH and _kmh(b, c) > TELEPORT_KMH and _kmh(a, c) < TELEPORT_KMH
                if spike or tele:
                    drop(b, "spike")
                    changed = True
                    continue
            kept.append(b)
        out = kept

    # Teleports: a pair with an impossible speed. Drop whichever fix, once
    # removed, leaves its neighbours consistent. A short excursion (up to
    # three fixes away and back) is dropped whole. If it is still ambiguous,
    # keep both; trip and leg splitting break the line there instead.
    kept = []
    i = 0
    while i < len(out):
        p = out[i]
        if kept and _kmh(kept[-1], p) > TELEPORT_KMH and \
                haversine(kept[-1]["lat"], kept[-1]["lon"], p["lat"], p["lon"]) > 1000:
            a = kept[-1]
            prev = kept[-2] if len(kept) > 1 else None
            nxt = out[i + 1] if i + 1 < len(out) else None
            a_bad = prev is not None and _kmh(prev, p) < TELEPORT_KMH
            p_bad = nxt is not None and _kmh(a, nxt) < TELEPORT_KMH
            if a_bad and not p_bad:
                drop(kept.pop(), "teleport")
                continue
            if p_bad and not a_bad:
                drop(p, "teleport")
                i += 1
                continue
            back = next((k for k in range(i + 1, min(len(out), i + 4))
                         if _kmh(a, out[k]) < TELEPORT_KMH and
                         haversine(a["lat"], a["lon"], out[k]["lat"], out[k]["lon"]) <
                         haversine(a["lat"], a["lon"], p["lat"], p["lon"]) / 2), None)
            if back is not None:
                for k in range(i, back):
                    drop(out[k], "teleport")
                i = back
                continue
        kept.append(p)
        i += 1
    for k, p in enumerate(kept):
        p["_i"] = k
    dropped.sort(key=lambda p: p["timestamp"])
    return kept, dropped


# A phone left indoors can report positions that hop hundreds of metres to and
# fro every few seconds (seen in a supermarket: 15 minutes of 150-650 m jumps
# at 20-115 km/h, reversing at nearly every fix, while a watch showed the stop).
# Nothing travels like that, but the test is deliberately narrow, because a
# drive that turns round, or a moving phone that keeps flashing one stale
# position, also reverses a lot: fixes are only dropped when the track comes
# to a place at an ordinary speed, keeps returning to it between at least
# JITTER_MIN_HOPS separate hops away, and leaves it at an ordinary speed.
JITTER_LEG_M = 100.0        # both legs of a reversal are at least this long
JITTER_LEG_KMH = 10.0       # and at least this fast (not a person turning round)
JITTER_TURN_COS = -0.7      # a reversal: the heading turns by more than ~135 degrees
JITTER_CHAIN_S = 240        # reversals this close together belong to one episode
JITTER_MIN_TURNS = 3        # an episode needs this many reversals
JITTER_FAST_KMH = 40.0      # and this many legs at a speed no shuttle run reaches
JITTER_FAST_LEGS = 2
JITTER_RETURN_S = 600       # home fixes this close in time are one sitting
JITTER_HOME_M = 75.0        # fixes this close to where the phone really was are kept
JITTER_MIN_HOPS = 3


def dejitter(fixes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (kept, dropped): the fixes of to-and-fro hops away from the place
    the phone sat at, marked "jitter"."""
    out = list(fixes)
    dropped: list[dict[str, Any]] = []
    for _ in range(6):
        gone = _hops(out)
        if not gone:
            break
        for i in sorted(gone):
            out[i]["_drop"] = "jitter"
            dropped.append(out[i])
        out = [p for i, p in enumerate(out) if i not in gone]
    return out, dropped


def _hops(out: list[dict[str, Any]]) -> set[int]:
    """Indexes of the hop fixes of the first sitting found, or an empty set."""
    n = len(out)
    if n < JITTER_MIN_TURNS + 2:
        return set()
    k0 = math.cos(math.radians(out[0]["lat"])) * 111320.0
    xy = [(p["lon"] * k0, p["lat"] * 110540.0) for p in out]

    def dist(i: int, j: int) -> float:
        return math.hypot(xy[i][0] - xy[j][0], xy[i][1] - xy[j][1])

    leg = [dist(i, i + 1) for i in range(n - 1)]
    kmh = [leg[i] / max(1, out[i + 1]["timestamp"] - out[i]["timestamp"]) * 3.6 for i in range(n - 1)]
    turns = []
    for i in range(1, n - 1):
        d1, d2 = leg[i - 1], leg[i]
        if d1 < JITTER_LEG_M or d2 < JITTER_LEG_M or kmh[i - 1] < JITTER_LEG_KMH or kmh[i] < JITTER_LEG_KMH:
            continue
        ux, uy = xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]
        vx, vy = xy[i + 1][0] - xy[i][0], xy[i + 1][1] - xy[i][1]
        if (ux * vx + uy * vy) / (d1 * d2) < JITTER_TURN_COS:
            turns.append(i)
    k = 0
    while k < len(turns):
        m = k
        while m + 1 < len(turns) and out[turns[m + 1]]["timestamp"] - out[turns[m]]["timestamp"] <= JITTER_CHAIN_S:
            m += 1
        lo, hi = turns[k] - 1, turns[m] + 1          # fixes lo..hi inclusive
        nturns = m - k + 1
        k = m + 1
        if nturns < JITTER_MIN_TURNS or sum(1 for i in range(lo, hi) if kmh[i] >= JITTER_FAST_KMH) < JITTER_FAST_LEGS:
            continue
        # Home: the spot the episode keeps returning to. A false position can
        # recur as often as the true one, so the track around the episode has
        # a say: the fixes of the five minutes before and after it count too,
        # and so does the place the first fast hop set off from.
        t_lo, t_hi = out[lo]["timestamp"] - 300, out[hi]["timestamp"] + 300
        ctx = [j for j in range(max(0, lo - 30), lo) if out[j]["timestamp"] >= t_lo] + list(range(lo, hi + 1)) + \
              [j for j in range(hi + 1, min(n, hi + 31)) if out[j]["timestamp"] <= t_hi]
        origin = next((i for i in range(max(0, lo - 1), hi) if kmh[i] >= JITTER_FAST_KMH), lo)

        def weight(i: int) -> int:
            return sum(1 for j in ctx if dist(i, j) <= 50.0) + (5 if dist(i, origin) <= 50.0 else 0)

        home = max(range(lo, hi + 1), key=lambda i: (weight(i), -i))
        # The sitting: from the first to the last fix at home, for as long as
        # fixes keep turning up there.
        first = last = home
        j = home - 1
        while j >= 0 and out[first]["timestamp"] - out[j]["timestamp"] <= JITTER_RETURN_S:
            if dist(j, home) <= JITTER_HOME_M:
                first = j
            j -= 1
        j = home + 1
        while j < n and out[j]["timestamp"] - out[last]["timestamp"] <= JITTER_RETURN_S:
            if dist(j, home) <= JITTER_HOME_M:
                last = j
            j += 1
        # Arrived and left like a person, not in a hop. Measured over ten
        # seconds or more: one-second fixes are too noisy to give a speed.
        def pace(i: int, step: int) -> float:
            j = i + step
            while 0 < j < n - 1 and abs(out[j]["timestamp"] - out[i]["timestamp"]) < 10:
                j += step
            if not 0 <= j < n:
                return 0.0
            return dist(i, j) / max(1, abs(out[j]["timestamp"] - out[i]["timestamp"])) * 3.6

        if pace(first, -1) >= JITTER_FAST_KMH or pace(last, 1) >= JITTER_FAST_KMH:
            continue
        away = [i for i in range(first + 1, last) if dist(i, home) > JITTER_HOME_M]
        hops = sum(1 for a, i in enumerate(away) if a == 0 or away[a - 1] != i - 1)
        if hops >= JITTER_MIN_HOPS:
            return set(away)
    return set()


# One fix that jumps sideways off a walk and straight back (seen 90 m out over
# a harbour, both legs at 14 km/h in a 5 km/h walk). Too small for the spike
# test above, which is sized for drives.
SIDE_LEG_M = 60.0          # both legs at least this long
SIDE_EXTRA_M = 100.0       # out and back adds at least this much to the path
SIDE_RATIO = 2.5           # and makes it this many times the direct step
SIDE_KMH = 10.0            # both legs at least this fast
SIDE_FASTER = 2.2          # and this many times the pace of the fixes around


def _side_steps(out: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    n = len(out)
    if n < 8:
        return out, []
    gone: set[int] = set()
    for i in range(1, n - 1):
        if i - 1 in gone:
            continue
        a, b, c = out[i - 1], out[i], out[i + 1]
        d1 = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        d2 = haversine(b["lat"], b["lon"], c["lat"], c["lon"])
        if d1 < SIDE_LEG_M or d2 < SIDE_LEG_M:
            continue
        d3 = haversine(a["lat"], a["lon"], c["lat"], c["lon"])
        if d1 + d2 - d3 < SIDE_EXTRA_M or d1 + d2 < SIDE_RATIO * d3:
            continue
        v1, v2 = _kmh(a, b), _kmh(b, c)
        if min(v1, v2) < SIDE_KMH:
            continue
        around = sorted(_kmh(out[k], out[k + 1]) for k in (*range(max(0, i - 4), i - 1), *range(i + 1, min(n - 1, i + 4))))
        if len(around) < 4:
            continue
        if min(v1, v2) >= SIDE_FASTER * max(around[len(around) // 2], 2.0):
            gone.add(i)
    if not gone:
        return out, []
    hops = []
    for i in sorted(gone):
        out[i]["_drop"] = "spike"
        hops.append(out[i])
    return [p for i, p in enumerate(out) if i not in gone], hops
