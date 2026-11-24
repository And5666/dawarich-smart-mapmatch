"""Travel-mode labelling.

Labels are walk, cycle, drive, train and stay. Stays come from stay detection.
Each movement leg gets a Viterbi pass over robust per-fix speed and railway
proximity with a high switch cost, then runs that are too short to be an
activity merge into a neighbour. Valhalla is not consulted here; the matcher
settles walk/cycle/drive ambiguity later by comparing fits.
"""
from __future__ import annotations

import math
from typing import Any

from . import rail
from .geo import haversine
from .stays import find_stays

MODES = ("walk", "cycle", "drive", "train")
SWITCH_COST = 6.0
MIN_RUN_S = 90
MIN_RUN_FIXES = 4
RAIL_NEAR_M = 40.0
VMAX_KMH = {"walk": 16.0, "cycle": 50.0, "drive": 200.0, "train": 260.0}


def speeds(fixes: list[dict[str, Any]]) -> list[float]:
    """Per-fix speed in km/h, then a 5-fix median.

    The phone's own velocity (m/s, Doppler) is preferred when it is positive:
    on this data it agrees with position-derived speed (median ratio 1.06)
    and is not fooled by sparse or batched timestamps. Zero means "unknown"
    as often as "still", so it falls back to the central difference.
    """
    n = len(fixes)
    if n < 2:
        v = fixes[0].get("velocity") if fixes else None
        return [float(v) * 3.6 if v and v > 0 else 0.0] * n
    raw = []
    for i in range(n):
        a = fixes[max(0, i - 1)]
        b = fixes[min(n - 1, i + 1)]
        dt = b["timestamp"] - a["timestamp"]
        d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        pos = d / dt * 3.6 if dt > 0 else 0.0
        dv = fixes[i].get("velocity")
        if dv is not None and dv > 0:
            dev = float(dv) * 3.6
            # A stale phone reading (seen on trains: 20 km/h reported while
            # accurate fixes move 3 km every half minute) loses to positions,
            # but only at train-like speeds: a single GPS jump must not turn a
            # bike ride into a drive.
            acc = max(a.get("accuracy") or 50.0, b.get("accuracy") or 50.0)
            if acc <= 30 and dt >= 20 and pos > 100 and pos > 3 * dev:
                raw.append(pos)
            else:
                raw.append(dev)
            continue
        raw.append(pos)
    out = []
    for i in range(n):
        w = sorted(raw[max(0, i - 2): i + 3])
        out.append(w[len(w) // 2])
    return out


def near_rail(fixes: list[dict[str, Any]]) -> list[bool]:
    """Per fix: on a railway? Smoothed over 5 fixes so one fix in a gap of the
    GTFS shapes (or one poor fix) does not break a train run."""
    hits = []
    for p in fixes:
        try:
            nr = rail.nearest(p["lat"], p["lon"])
        except FileNotFoundError:
            nr = None
        hits.append(bool(nr and nr["dist"] <= RAIL_NEAR_M))
    out = []
    for i in range(len(hits)):
        w = hits[max(0, i - 2): i + 3]
        out.append(hits[i] or sum(w) * 2 > len(w))
    return out


def _cost(mode: str, v: float, on_rail: bool) -> float:
    if mode == "walk":
        return 0.0 if v <= 7 else min(40.0, ((v - 7) / 3.0) ** 2)
    if mode == "cycle":
        if v < 5:
            return 1.6
        if v < 8:
            return 0.7
        if v <= 30:
            return 0.15
        return min(40.0, 0.15 + ((v - 30) / 6.0) ** 2)
    if mode == "drive":
        if v < 5:
            return 2.2
        if v < 15:
            return 1.2
        if v < 25:
            return 0.5
        if v > 180:
            return min(40.0, ((v - 180) / 15.0) ** 2)   # no car does 300 km/h; a train can
        return 0.0
    # train: being on a railway at speed is strong evidence. Roads that run
    # beside a railway are settled later by comparing rail and road fits.
    if not on_rail:
        return 6.0
    if v < 10:
        return 1.2
    if v < 30:
        return -0.5
    return -1.5


def _viterbi(v: list[float], rails: list[bool]) -> list[str]:
    n = len(v)
    cost = {m: _cost(m, v[0], rails[0]) for m in MODES}
    back: list[dict[str, str]] = []
    for i in range(1, n):
        new: dict[str, float] = {}
        bp: dict[str, str] = {}
        for m in MODES:
            best_prev = min(MODES, key=lambda p: cost[p] + (0.0 if p == m else SWITCH_COST))
            new[m] = cost[best_prev] + (0.0 if best_prev == m else SWITCH_COST) + _cost(m, v[i], rails[i])
            bp[m] = best_prev
        back.append(bp)
        cost = new
    m = min(MODES, key=lambda k: cost[k])
    out = [m]
    for bp in reversed(back):
        m = bp[m]
        out.append(m)
    return out[::-1]


def runs(labels: list[str]) -> list[tuple[str, int, int]]:
    out = []
    i = 0
    while i < len(labels):
        j = i + 1
        while j < len(labels) and labels[j] == labels[i]:
            j += 1
        out.append((labels[i], i, j))
        i = j
    return out


def _merge_short(fixes: list[dict[str, Any]], labels: list[str], forced: list[bool]) -> None:
    """Merge movement runs too short to be an activity into a neighbour."""
    changed = True
    while changed:
        changed = False
        rs = runs(labels)
        for k, (m, i, j) in enumerate(rs):
            if m == "stay" or any(forced[i:j]):
                continue
            dur = fixes[j - 1]["timestamp"] - fixes[i]["timestamp"]
            if j - i >= MIN_RUN_FIXES and dur >= MIN_RUN_S:
                continue
            # Only merge into a mode that could physically cover this stretch:
            # a short fast burst before a walk is a ride, not a fast walk.
            v = sorted(speeds(fixes[max(0, i - 1):min(len(fixes), j + 1)]))
            fast = v[len(v) // 2] if v else 0.0
            cands = []
            for kk in (k - 1, k + 1):
                if 0 <= kk < len(rs) and rs[kk][0] != "stay":
                    nm, ni, nj = rs[kk]
                    if fast > VMAX_KMH.get(nm, 200.0):
                        continue
                    cands.append((fixes[nj - 1]["timestamp"] - fixes[ni]["timestamp"], nm))
            if not cands:
                continue
            pick = max(cands)[1]
            if pick != m:
                for x in range(i, j):
                    labels[x] = pick
                changed = True
                break


def label(fixes: list[dict[str, Any]], overrides: list[dict[str, Any]] | None = None
          ) -> tuple[list[str], list[bool], list[str]]:
    """Return (labels, forced, keys) per fix.

    keys separate runs: two different stays next to each other (a data gap
    between two places) share the label "stay" but not the key.
    """
    n = len(fixes)
    labels = ["walk"] * n
    forced = [False] * n
    stay_id = [-1] * n
    for sid, (a, b) in enumerate(find_stays(fixes)):
        for k in range(a, b):
            labels[k] = "stay"
            stay_id[k] = sid
    ov_id = [-1] * n
    for oi, ov in enumerate(overrides or []):
        for k, p in enumerate(fixes):
            if int(ov["t0"]) <= p["timestamp"] <= int(ov["t1"]):
                labels[k] = ov["mode"]
                forced[k] = True
                ov_id[k] = oi

    rails = near_rail(fixes)
    # Viterbi per leg: maximal runs that are neither stay nor forced. Speeds
    # are computed per leg so the smoothing never averages in a stay.
    i = 0
    while i < n:
        if labels[i] == "stay" or forced[i]:
            i += 1
            continue
        j = i
        while j < n and labels[j] != "stay" and not forced[j]:
            j += 1
        labels[i:j] = _viterbi(speeds(fixes[i:j]), rails[i:j])
        i = j
    _merge_short(fixes, labels, forced)
    _walk_through_pauses(fixes, labels, forced, stay_id)
    # Each override is its own run, so a split segment keeps both halves even
    # while they share a mode.
    keys = [f"ov{ov_id[k]}:{labels[k]}" if forced[k] else
            f"stay{stay_id[k]}" if labels[k] == "stay" else labels[k] for k in range(n)]
    return labels, forced, keys


WALK_PAUSE_S = 1500


def _walk_through_pauses(fixes: list[dict[str, Any]], labels: list[str], forced: list[bool],
                         stay_id: list[int]) -> None:
    """A short stop with walking on both sides is part of the walk.

    A hike is a string of pauses (a view, a scramble, lunch on a ridge); as
    stays they cut the line into pieces and the day reads as mostly stationary.
    Stops at the ends of a trip, or next to anything but walking, stay stays.
    """
    n = len(labels)
    i = 0
    while i < n:
        if labels[i] != "stay" or forced[i]:
            i += 1
            continue
        j = i
        while j < n and labels[j] == "stay" and stay_id[j] == stay_id[i] and not forced[j]:
            j += 1
        before = labels[i - 1] if i > 0 else ""
        after = labels[j] if j < n else ""
        if before == "walk" and after == "walk" and \
                fixes[j - 1]["timestamp"] - fixes[i]["timestamp"] < WALK_PAUSE_S:
            for k in range(i, j):
                labels[k] = "walk"
                stay_id[k] = -1
        i = j


def summary_speed(fixes: list[dict[str, Any]]) -> dict[str, float]:
    v = sorted(speeds(fixes))
    if not v:
        return {"median": 0.0, "p90": 0.0}
    return {"median": v[len(v) // 2], "p90": v[min(len(v) - 1, int(math.ceil(len(v) * 0.9)) - 1)]}
