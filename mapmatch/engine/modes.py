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

from . import rail, railnet
from .fill import at_station, rail_path
from .geo import haversine
from .stays import find_dwells, find_stays

MODES = ("walk", "cycle", "drive", "train")
SWITCH_COST = 6.0
MIN_RUN_S = 90
MIN_RUN_FIXES = 4
RAIL_NEAR_M = 40.0
VMAX_KMH = {"walk": 16.0, "cycle": 50.0, "drive": 200.0, "train": 260.0}
# Evidence is counted per half-minute, not per fix: a phone logging every 30 s
# and a bike computer logging every second must need the same stretch of
# riding to change mode. Fixes further apart than this count once.
REF_DT_S = 30.0
# A train that never gets going is a ride or a drive beside the railway.
TRAIN_MIN_P90_KMH = 40.0
# A car gets above 45 km/h somewhere; a road bike holds 25-35 km/h for an
# hour without doing so. Only a closely logged drive shows its top speed:
# a fix every 30 s can miss every fast moment of a drive through town.
CYCLE_TOP_KMH = 45.0
DENSE_DT_S = 10
DENSE_MIN_FIXES = 20
# Walking, however brisk, moves at 4-6 km/h; a closely logged "walk" that
# moves at 7 km/h and more for most of its moving time is a slow ride
# (climbing, a towpath, riding with someone) with stops in it.
FAST_WALK_P50_KMH = 7.0
FAST_WALK_P75_KMH = 8.5
MOVING_KMH = 3.0
SPARSE_S = 20             # neighbours this far apart in time: sparse logging
# One vehicle between stops: a bike does not turn into a car, or a car into a
# bike, without stopping. Where a ride and a drive touch with no stop between
# them, and the drive really reached car speed, the "ride" is the car slowing
# down (the last streets into town, a car park, a queue).
CAR_PROOF_KMH = 45.0
# A short, quick hop next to a car journey with only stops in between (to the
# shop, to the next car park) is the car too. A real ride after driving
# somewhere is longer and slower than this.
HOP_MAX_S = 900
HOP_P50_KMH = 20.0
HOP_P90_KMH = 27.0


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
            # The reading is the speed at one instant. With a fix every half
            # minute or more that instant can be a junction or a bend while
            # the minute around it was driven at 50 (a drive then reads like a
            # bike ride), and some phones repeat a stale reading. The ground
            # covered cannot lie that way: it is a floor on the average speed,
            # less what the position error could add. The floor is the slower
            # of the hop before and the hop after, so that one GPS jump, which
            # shows in only one of them, does not turn a ride into a drive.
            if dt >= SPARSE_S and 0 < i < n - 1:
                raw.append(max(dev, min(_hop_floor(a, fixes[i]), _hop_floor(fixes[i], b))))
            else:
                raw.append(dev)
            continue
        raw.append(pos)
    out = []
    for i in range(n):
        w = sorted(raw[max(0, i - 2): i + 3])
        out.append(w[len(w) // 2])
    return out


def _hop_floor(p: dict[str, Any], q: dict[str, Any]) -> float:
    """The least average speed (km/h) between two fixes that their positions allow."""
    dt = q["timestamp"] - p["timestamp"]
    if dt <= 0:
        return 0.0
    d = haversine(p["lat"], p["lon"], q["lat"], q["lon"])
    slack = min(p.get("accuracy") or 25.0, 60.0) + min(q.get("accuracy") or 25.0, 60.0)
    return max(0.0, d - slack) / dt * 3.6


def near_rail(fixes: list[dict[str, Any]]) -> list[bool]:
    """Per fix: on a railway? Smoothed over 5 fixes so one fix in a gap of the
    GTFS shapes (or one poor fix) does not break a train run."""
    hits = []
    for p in fixes:
        try:
            nr = rail.nearest(p["lat"], p["lon"])
        except FileNotFoundError:
            nr = None
        # The GB timetable shapes, or a railway network built from OpenStreetMap.
        hits.append(bool(nr and nr["dist"] <= RAIL_NEAR_M) or railnet.distance(p["lat"], p["lon"]) <= RAIL_NEAR_M)
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


def weights(fixes: list[dict[str, Any]]) -> list[float]:
    """How much of the half-minute evidence unit each fix stands for."""
    n = len(fixes)
    if n < 2:
        return [1.0] * n
    out = []
    for i in range(n):
        a = fixes[max(0, i - 1)]["timestamp"]
        b = fixes[min(n - 1, i + 1)]["timestamp"]
        span = (b - a) / (2.0 if 0 < i < n - 1 else 1.0)
        out.append(max(0.02, min(1.0, span / REF_DT_S)))
    return out


def _viterbi(v: list[float], rails: list[bool], w: list[float] | None = None,
             modes: tuple[str, ...] = MODES) -> list[str]:
    n = len(v)
    w = w or [1.0] * n
    cost = {m: w[0] * _cost(m, v[0], rails[0]) for m in modes}
    back: list[dict[str, str]] = []
    for i in range(1, n):
        new: dict[str, float] = {}
        bp: dict[str, str] = {}
        for m in modes:
            best_prev = min(modes, key=lambda p: cost[p] + (0.0 if p == m else SWITCH_COST))
            new[m] = cost[best_prev] + (0.0 if best_prev == m else SWITCH_COST) + w[i] * _cost(m, v[i], rails[i])
            bp[m] = best_prev
        back.append(bp)
        cost = new
    m = min(modes, key=lambda k: cost[k])
    out = [m]
    for bp in reversed(back):
        m = bp[m]
        out.append(m)
    return out[::-1]


def _fast_trains(leg: list[dict[str, Any]], out: list[str]) -> bool:
    """Every train run in the leg reaches train speed."""
    for m, a, b in runs(out):
        if m == "train" and summary_speed(leg[a:b])["p90"] < TRAIN_MIN_P90_KMH:
            return False
    return True


def _slow_drives_are_rides(leg: list[dict[str, Any]], v: list[float], out: list[str]) -> None:
    """Closely logged "driving" that never passes cycling speed is a ride."""
    idx = [k for k, m in enumerate(out) if m == "drive"]
    if len(idx) < DENSE_MIN_FIXES:
        return
    dts = sorted(leg[k]["timestamp"] - leg[k - 1]["timestamp"] for k in idx if k > 0)
    if not dts or dts[len(dts) // 2] > DENSE_DT_S:
        return
    top = sorted(v[k] for k in idx)
    if top[min(len(top) - 1, int(len(top) * 0.98))] <= CYCLE_TOP_KMH:
        for k in idx:
            out[k] = "cycle"


def _fast_walks_are_rides(leg: list[dict[str, Any]], v: list[float], out: list[str],
                          forced: list[bool]) -> None:
    """Closely logged "walking" that moves at riding speed is a ride."""
    for m, a, b in runs(out):
        if m != "walk" or b - a < DENSE_MIN_FIXES or any(forced[a:b]):
            continue
        dts = sorted(leg[k]["timestamp"] - leg[k - 1]["timestamp"] for k in range(a + 1, b))
        if dts[len(dts) // 2] > DENSE_DT_S:
            continue
        moving = sorted(v[k] for k in range(a, b) if v[k] > MOVING_KMH)
        if len(moving) < DENSE_MIN_FIXES:
            continue
        if moving[len(moving) // 2] >= FAST_WALK_P50_KMH and moving[int(len(moving) * 0.75)] >= FAST_WALK_P75_KMH:
            out[a:b] = ["cycle"] * (b - a)


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


TRAIN_HOLE_MIN_M = 800.0      # a hole at least this long between two fixes (one Underground stop)
TRAIN_HOLE_MIN_KMH = 9.0      # crossed faster than anyone walks that far (fill.LONG_WALK_KMH)
TRAIN_HOLE_MAX_KMH = 220.0
TRAIN_HOLE_STATION_M = 300.0


def _near_station(p: dict[str, Any]) -> bool:
    return at_station(p, TRAIN_HOLE_STATION_M)


def _train_holes(fixes: list[dict[str, Any]], labels: list[str], forced: list[bool], pinned: list[bool]) -> None:
    """A ride with nothing recorded on the way (the Underground, a phone in a
    pocket on a train) shows as two fixes kilometres apart. With no speeds in
    between it was labelled a walk and got a walking route. It is a train
    when a railway joins the two ends, the timing fits one, and the ends are
    at stations (or the train carries on from there): the two fixes are then
    labelled train, and the gap between them is filled along the rails."""
    steps = sorted(b["timestamp"] - a["timestamp"] for a, b in zip(fixes, fixes[1:]))
    usual = steps[len(steps) // 2] if steps else 0.0
    for k in range(len(fixes) - 1):
        a, b = fixes[k], fixes[k + 1]
        if forced[k] or forced[k + 1] or (labels[k] == "train" and labels[k + 1] == "train"):
            continue
        dt = b["timestamp"] - a["timestamp"]
        # Only where the speeds said nothing, or where the logging really
        # stopped: a drive or a ride logged once a minute also has fixes a
        # kilometre apart, and passes stations.
        quiet = labels[k] in ("walk", "stay", "train") and labels[k + 1] in ("walk", "stay", "train")
        if not quiet and dt < max(180.0, 4.0 * usual):
            continue
        chord = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        if dt < 60 or chord < TRAIN_HOLE_MIN_M or chord / dt * 3.6 < TRAIN_HOLE_MIN_KMH:
            continue
        if not ((_near_station(a) or labels[k] == "train") and (_near_station(b) or labels[k + 1] == "train")):
            continue
        path = rail_path(a, b)
        if not path:
            continue
        length = sum(haversine(p[0], p[1], q[0], q[1]) for p, q in zip(path, path[1:]))
        if length > 1.6 * chord + 500.0 or length / dt * 3.6 > TRAIN_HOLE_MAX_KMH:
            continue
        for x in (k, k + 1):
            labels[x] = "train"
            pinned[x] = True


def _detailed(fixes: list[dict[str, Any]], i: int, j: int) -> bool:
    """Logged every few seconds (a watch, a bike computer)."""
    if j - i < DENSE_MIN_FIXES:
        return False
    dts = sorted(fixes[k]["timestamp"] - fixes[k - 1]["timestamp"] for k in range(i + 1, j))
    return dts[len(dts) // 2] <= DENSE_DT_S


def _one_vehicle(fixes: list[dict[str, Any]], labels: list[str], held: list[bool]) -> None:
    """Rides that are really the car: see CAR_PROOF_KMH and HOP_MAX_S."""
    n = len(labels)
    if n < 2:
        return
    v = speeds(fixes)

    def pct(i: int, j: int, f: float) -> float:
        s = sorted(v[i:j])
        return s[min(len(s) - 1, int(len(s) * f))] if s else 0.0

    changed = True
    while changed:
        changed = False
        rs = runs(labels)
        for k, (m, i, j) in enumerate(rs):
            if m != "cycle" or any(held[i:j]) or _detailed(fixes, i, j):
                continue
            # Touching a drive that reached car speed, no stop between.
            for kk in (k - 1, k + 1):
                if 0 <= kk < len(rs) and rs[kk][0] == "drive" and max(v[rs[kk][1]:rs[kk][2]], default=0.0) >= CAR_PROOF_KMH:
                    labels[i:j] = ["drive"] * (j - i)
                    changed = True
                    break
            if changed:
                break
            # A short quick hop, with a drive on the other side of a stop.
            dur = fixes[j - 1]["timestamp"] - fixes[i]["timestamp"]
            if dur > HOP_MAX_S or not (pct(i, j, 0.5) >= HOP_P50_KMH or pct(i, j, 0.9) >= HOP_P90_KMH):
                continue
            for step in (-1, 1):
                kk = k + step
                while 0 <= kk < len(rs) and rs[kk][0] == "stay":
                    kk += step
                if 0 <= kk < len(rs) and kk != k + step and rs[kk][0] == "drive":
                    labels[i:j] = ["drive"] * (j - i)
                    changed = True
                    break
            if changed:
                break


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
    stays = find_stays(fixes, trim=True)
    for sid, (a, b) in enumerate(stays):
        for k in range(a, b):
            labels[k] = "stay"
            stay_id[k] = sid
    dwell = [False] * n
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
        leg = fixes[i:j]
        v, w = speeds(leg), weights(leg)
        out = _viterbi(v, rails[i:j], w)
        if any(m == "train" for m, _, _ in runs(out)) and not _fast_trains(leg, out):
            out = _viterbi(v, rails[i:j], w, tuple(m for m in MODES if m != "train"))
        _slow_drives_are_rides(leg, v, out)
        labels[i:j] = out
        i = j
    pinned = [False] * n
    _train_holes(fixes, labels, forced, pinned)
    # A pinned fix is kept like one set by hand, without being reported as such.
    held = [f or p for f, p in zip(forced, pinned)]
    _merge_short(fixes, labels, held)
    _walk_through_pauses(fixes, labels, forced, stay_id, dwell)
    # Wider, slower stops the stay radius misses (see stays.find_dwells),
    # looked for in what is still movement at this point: ordinary stays keep
    # their own fixes and stay single dots, and a short pause that was just
    # folded into its walk can be part of a long one. Only where the trip
    # starts or ends (nothing but stays beyond it): that is arriving somewhere
    # and lingering. In the middle of a trip a slow stretch is left to the
    # rules above (a scramble at 0.6 km/h with a rest on the ridge is a walk).
    sid = len(stays)
    i = 0
    found = False
    while i < n:
        if labels[i] == "stay" or forced[i]:
            i += 1
            continue
        j = i
        while j < n and labels[j] != "stay" and not forced[j]:
            j += 1
        for a, b in find_dwells(fixes[i:j]):
            if not (all(m == "stay" for m in labels[:i + a]) or all(m == "stay" for m in labels[i + b:])):
                continue
            for k in range(i + a, i + b):
                labels[k] = "stay"
                stay_id[k] = sid
                dwell[k] = True
            sid += 1
            found = True
        i = j
    if found:
        # One or two fixes left between a drive and the stop it ends at.
        _merge_short(fixes, labels, held)
    _one_vehicle(fixes, labels, held)
    # Judged over the whole walk, pauses included, not leg by leg.
    _fast_walks_are_rides(fixes, speeds(fixes) if n else [], labels, forced)
    # Each override is its own run, so a split segment keeps both halves even
    # while they share a mode.
    # A train run that contains such a hole is one ride: keep it one segment.
    for m, a, b in runs(labels):
        if m == "train" and any(pinned[a:b]):
            for x in range(a, b):
                pinned[x] = not forced[x]
    keys = [f"ov{ov_id[k]}:{labels[k]}" if forced[k] else
            f"auto:{labels[k]}" if pinned[k] and labels[k] == "train" else
            f"{'dwell' if dwell[k] else 'stay'}{stay_id[k]}" if labels[k] == "stay" else labels[k] for k in range(n)]
    return labels, forced, keys


WALK_PAUSE_S = 1500


def _walk_through_pauses(fixes: list[dict[str, Any]], labels: list[str], forced: list[bool],
                         stay_id: list[int], dwell: list[bool]) -> None:
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
                dwell[k] = False
        i = j


def summary_speed(fixes: list[dict[str, Any]]) -> dict[str, float]:
    v = sorted(speeds(fixes))
    if not v:
        return {"median": 0.0, "p90": 0.0}
    return {"median": v[len(v) // 2], "p90": v[min(len(v) - 1, int(math.ceil(len(v) * 0.9)) - 1)]}
