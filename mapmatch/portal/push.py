"""Write matched trips into Dawarich, in place, and put originals back.

Dawarich draws its tracks from the points it holds, per device, and only drops
a track once fewer than two of its points remain. So a trip cannot be swapped
on its own: the stray fixes either side of it share a track with it, and
removing the trip leaves that track as a straight line between them. Every
change therefore works on a *span*: the trip windows involved, widened to
cover each Dawarich track that touches them (and any trip those tracks reach).
Inside a span everything is deleted and written again as it should be:

- a trip that is pushed gets its matched stream, on the device its GPS came
  from, each point marked as ours (Wi-Fi name "mapmatch");
- everything else gets its original fixes back from the archive, on their own
  device.

Safety rules:
- Before anything is deleted, every Dawarich point in the span (anomalies
  included) is archived; if any original cannot be confirmed in the archive
  the span is left untouched.
- The archive is never modified beyond adding points.
- PUSH_DRY_RUN computes and records the plan without changing Dawarich.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any, Callable

from ..engine.io import normalize
from . import archive, trips
from .dawarich import Client
from .db import Database, now


def client_for(db: Database, cfg: Any, uid: int, transport: Any = None) -> Client:
    s = trips.settings(db, uid)
    return Client(s.get("dawarich_url") or cfg.dawarich_url, s.get("api_key") or "",
                  cfg.dawarich_host_header, transport=transport)


def _record(db: Database, uid: int, trip_id: int, kind: str, version: int | None, deleted: int,
            posted: int, dry: bool, detail: dict[str, Any]) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO pushes(user_id, trip_id, kind, version, deleted, posted, dry_run, detail, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (uid, trip_id, kind, version, deleted, posted, 1 if dry else 0, json.dumps(detail), now()))


def _merge(ivs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(ivs):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def spans(seeds: list[tuple[int, int]], reach: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Grow each seed window over every `reach` interval that overlaps it, until
    nothing more overlaps; return the merged result."""
    cur = _merge(seeds)
    reach = sorted(reach)
    while True:
        grown = []
        for a, b in cur:
            for c, d in reach:
                if c > b:
                    break
                if d >= a:
                    a, b = min(a, c), max(b, d)
            grown.append((a, b))
        grown = _merge(grown)
        if grown == cur:
            return cur
        cur = grown


def _tracker(fixes: list[dict[str, Any]]) -> str:
    """The device most of these fixes came from ('' for none)."""
    c = Counter(str(p.get("tracker_id") or "") for p in fixes)
    return c.most_common(1)[0][0] if c else ""


STOP_REACH_S = 6 * 3600     # how far a stop at either end of the history reaches
STOP_JOIN_M = 150.0         # fixes this close to a stop's centre belong to it
STOP_EVERY_S = 1800         # one point per half hour while stopped
BAD_FIX_M = 100.0           # a lone fix worse than this is noise, not a place


def _runs(loose: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Fixes outside every trip, split wherever a trip lies between them."""
    wins = sorted((t["start_ts"], t["end_ts"]) for t in rows)
    out: list[list[dict[str, Any]]] = []
    for p in loose:
        ts = p["timestamp"]
        if any(a <= ts <= b for a, b in wins):
            continue
        if out and not any(out[-1][-1]["timestamp"] < a and b < ts for a, b in wins):
            out[-1].append(p)
        else:
            out.append([p])
    return out


def tidy_stops(run: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The fixes recorded while stopped, as clean points, one list per place:
    each place you were becomes its centre, at arrival and departure and every
    half hour between. A lone fix too inaccurate to be a place is dropped (the
    archive keeps it)."""
    from ..engine.geo import haversine
    from ..engine.stays import centroid
    clusters: list[list[dict[str, Any]]] = []
    for p in run:
        if clusters:
            good = [q for q in clusters[-1] if (q.get("accuracy") or 20.0) <= BAD_FIX_M] or clusters[-1]
            lat, lon = centroid(good)
            if haversine(lat, lon, p["lat"], p["lon"]) <= max(STOP_JOIN_M, 1.5 * (p.get("accuracy") or 20.0)):
                clusters[-1].append(p)
                continue
        clusters.append([p])
    out: list[list[dict[str, Any]]] = []
    last = -1
    for c in clusters:
        good = [q for q in c if (q.get("accuracy") or 20.0) <= BAD_FIX_M]
        if not good:
            continue
        lat, lon = centroid(good)
        dev = _tracker(c)
        t0, t1 = c[0]["timestamp"], c[-1]["timestamp"]
        stamps = [t for t in sorted({t0, t1, *range(t0 + STOP_EVERY_S, t1, STOP_EVERY_S)}) if t > last]
        if not stamps:
            continue
        last = stamps[-1]
        out.append([{"lat": lat, "lon": lon, "timestamp": ts, "accuracy": 10.0, "tracker": dev, "ours": True,
                     "mode": "stay"} for ts in stamps])
    return out


LINK_MIN_M = 25.0
DENSE_M = 150.0   # longest step between pushed points along a matched line


def densify(pts: list[dict[str, Any]], max_m: float = DENSE_M) -> list[dict[str, Any]]:
    """Extra points on the long straight stretches of a matched line, so no
    two consecutive points are more than max_m apart. Dawarich can then be
    told to break its lines at gaps longer than a few hundred metres without
    breaking a matched road. Straight jumps the matcher couldn't route, and
    recorded GPS kept as is, are left alone."""
    from ..engine.geo import haversine
    out: list[dict[str, Any]] = []
    for a, b in zip(pts, pts[1:]):
        out.append(a)
        if a.get("kind") in ("straight", "raw") or b.get("kind") in ("straight", "raw"):
            continue
        d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
        dt = int(b["timestamp"]) - int(a["timestamp"])
        n = min(int(d // max_m), dt - 1)
        for k in range(1, n + 1):
            f = k / (n + 1)
            out.append({**a, "lat": a["lat"] + (b["lat"] - a["lat"]) * f, "lon": a["lon"] + (b["lon"] - a["lon"]) * f,
                        "timestamp": int(a["timestamp"]) + int(round(dt * f))})
    if pts:
        out.append(pts[-1])
    # whole seconds, strictly increasing
    clean: list[dict[str, Any]] = []
    for p in out:
        if not clean or int(p["timestamp"]) > int(clean[-1]["timestamp"]):
            clean.append(p)
    return clean


def link(a: dict[str, Any], b: dict[str, Any], mode: str | None = None) -> list[dict[str, Any]]:
    """Points along the road from a to b, strictly between their times, so
    Dawarich doesn't draw a straight line from a stop to where a trip begins.
    Empty when no believable route exists (the straight line then stays)."""
    from ..engine.assemble import timed
    from ..engine.geo import haversine
    dt = int(b["timestamp"]) - int(a["timestamp"])
    d = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    if dt < 2 or d < LINK_MIN_M:
        return []
    if mode not in ("walk", "cycle", "drive"):
        v = d / dt * 3.6
        mode = "drive" if v > 25 else "cycle" if v > 9 else "walk"
    from ..engine import valhalla
    from ..engine.fill import PROFILE, VMAX_KMH
    try:
        r = valhalla.route([a, b], PROFILE[mode])
    except Exception:  # noqa: BLE001 - a missing link is only cosmetic
        return []
    shape = (r or {}).get("shape") or []
    if len(shape) < 2:
        return []
    length = sum(haversine(*p, *q) for p, q in zip(shape, shape[1:]))
    # The ends are a stop and a trip, not GPS to follow, so the only checks
    # are that the route reaches them, isn't a long detour, and fits the time.
    if haversine(a["lat"], a["lon"], *shape[0]) > 150 or haversine(b["lat"], b["lon"], *shape[-1]) > 150 \
            or length > 2.5 * d + 300 or length / dt * 3.6 > VMAX_KMH[mode] * 1.3 + 10:
        return []
    coords = [(a["lat"], a["lon"]), *shape, (b["lat"], b["lon"])]
    out, last = [], int(a["timestamp"])
    for lat, lon, ts, _ in timed(coords, a["timestamp"], b["timestamp"])[1:-1]:
        t = int(round(ts))
        if last < t < int(b["timestamp"]):
            out.append({"lat": lat, "lon": lon, "timestamp": t, "accuracy": 10.0, "tracker": a.get("tracker"),
                        "ours": True, "mode": mode})
            last = t
    ends = [{"lat": x["lat"], "lon": x["lon"], "timestamp": int(x["timestamp"]), "kind": "filled", "accuracy": 10.0,
             "tracker": a.get("tracker"), "ours": True, "mode": mode} for x in (a, b)]
    return [p for p in densify([ends[0], *out, ends[1]]) if p is not ends[0] and p is not ends[1]]


def _bridge_stops(pts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Route the step into and out of each stop inside a trip. A stop is one
    point at its centre; the portal draws it as a dot, but Dawarich would join
    it to the next part of the trip with a straight line."""
    from ..engine.geo import haversine
    out: list[dict[str, Any]] = []
    for a, b in zip(pts, pts[1:]):
        out.append(a)
        if "stay" in (a.get("mode"), b.get("mode")) and a.get("mode") != b.get("mode") \
                and haversine(a["lat"], a["lon"], b["lat"], b["lon"]) > 4 * LINK_MIN_M:
            moving = b if a.get("mode") == "stay" else a
            m = moving.get("mode")
            out += link(a, b, "walk" if m == "train" else m)
    if pts:
        out.append(pts[-1])
    return out


def _first_mode(res: dict[str, Any], last: bool = False) -> str:
    segs = [sg["mode"] for sg in res.get("segments", []) if sg["mode"] not in ("stay", "raw")]
    if not segs:
        return "walk"
    m = segs[-1] if last else segs[0]
    return "walk" if m == "train" else m


def apply(db: Database, cfg: Any, uid: int, changes: dict[int, bool], transport: Any = None,
          also: list[int] | None = None, progress: Callable[[int, str], None] | None = None,
          keep_going: bool = False, extra_spans: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    """Make Dawarich show `changes` ({trip id: True to push its current match,
    False to restore its originals}). Trips in `also` are rewritten as they
    already are (used to repair what earlier pushes left behind).

    keep_going: a span that fails is noted on its trips and the rest go on."""
    rows = [dict(r) for r in db.all("SELECT * FROM trips WHERE user_id=? ORDER BY start_ts", (uid,))]
    byid = {r["id"]: r for r in rows}
    for tid, on in changes.items():
        t = byid.get(tid)
        if not t:
            raise KeyError(tid)
        if on:
            if t["locked"]:
                raise ValueError("trip is locked; unlock it to push")
            if not t["closed"]:
                raise ValueError("trip is still in progress")
            if not t["result_version"]:
                raise ValueError("trip has no match yet")
    # What Dawarich should show for each trip: a result version, or None for the originals.
    shown: dict[int, int | None] = {}
    for t in rows:
        if t["id"] in changes:
            # A trip kept as recorded is never written: Dawarich keeps its originals.
            shown[t["id"]] = t["result_version"] if changes[t["id"]] and not trips.as_recorded(t) else None
        else:
            shown[t["id"]] = t["pushed_version"]
    # Each trip's window reaches back to the trip before and on to the one
    # after, so the stops either side of it are rewritten with it.
    order = [t["id"] for t in rows]
    pos = {tid: i for i, tid in enumerate(order)}

    def reach_of(tid: int) -> tuple[int, int]:
        i = pos[tid]
        a = rows[i - 1]["end_ts"] if i > 0 else rows[i]["start_ts"] - STOP_REACH_S
        b = rows[i + 1]["start_ts"] if i + 1 < len(rows) else rows[i]["end_ts"] + STOP_REACH_S
        return min(a, rows[i]["start_ts"]), max(b, rows[i]["end_ts"])

    seeds = [reach_of(t) for t in [*changes, *(also or [])] if t in byid]
    seeds += [(int(a), int(b)) for a, b in (extra_spans or [])]
    dry = cfg.push_dry_run
    summary = {"dry_run": dry, "spans": 0, "deleted": 0, "posted": 0, "matched_points": 0,
               "original_points": 0, "failed_spans": 0}
    if not seeds:
        return summary
    say = progress or (lambda *_: None)
    with client_for(db, cfg, uid, transport) as dw:
        lo, hi = min(a for a, _ in seeds), max(b for _, b in seeds)
        tracks = dw.tracks(lo - 86400, hi + 86400)
        groups = spans(seeds, tracks + [(t["start_ts"], t["end_ts"]) for t in rows])
        summary["spans"] = len(groups)
        def _span(a: int, b: int) -> None:
            remote = dw.points(a, b)
            originals, ours = archive.split_remote(db, uid, remote)
            archive.add_api(db, uid, originals)
            missing = sum(1 for raw in originals
                          if not ((p := normalize(raw)) and archive.contains(db, uid, p)))
            if missing:
                # Something we could not archive: refuse rather than lose it.
                raise RuntimeError(f"{missing} Dawarich points could not be archived; nothing changed")
            fixes = archive.window(db, uid, a, b)
            inside = [t for t in rows if t["start_ts"] <= b and a <= t["end_ts"]]
            posts: list[dict[str, Any]] = []
            covered: list[tuple[int, int]] = []
            streams: dict[int, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
            n_matched = 0
            for t in inside:
                ver = shown.get(t["id"])
                if not ver:
                    continue
                res = trips.result(db, t, ver)
                if not res:
                    raise RuntimeError(f"the pushed match of trip {t['id']} is gone; push it again")
                own = [p for p in fixes if t["start_ts"] <= p["timestamp"] <= t["end_ts"]]
                dev = _tracker(own)
                # Built afresh from the matched shapes, so a fix to how shapes
                # become points reaches Dawarich without matching again.
                from ..engine.assemble import stream as to_stream
                try:
                    pts = to_stream(res["segments"])
                except (KeyError, TypeError, ValueError):
                    pts = res["stream"]
                pts = [{**p, "tracker": dev, "ours": True} for p in pts if t["start_ts"] <= p["timestamp"] <= t["end_ts"]]
                stream = densify(_bridge_stops(pts))
                if len(stream) < 2:
                    raise ValueError(f"the match of trip {t['id']} is empty")
                posts += stream
                streams[t["id"]] = (stream, res)
                n_matched += len(stream)
                covered.append((t["start_ts"], t["end_ts"]))
            loose = [p for p in fixes if not any(c <= p["timestamp"] <= d for c, d in covered)]
            # GPS inside a trip that isn't pushed goes back exactly as recorded.
            posts += [{**p, "tracker": p["tracker_id"], "ours": False} for p in loose
                      if any(t["start_ts"] <= p["timestamp"] <= t["end_ts"] for t in rows)]
            for run in _runs(loose, rows):
                # A stop next to a pushed trip is tidied: one clean point where
                # you were, at arrival, departure and every half hour. A stop
                # between trips that both show their original GPS stays as is.
                before = max((t for t in rows if t["end_ts"] < run[0]["timestamp"]), key=lambda t: t["end_ts"], default=None)
                after = min((t for t in rows if t["start_ts"] > run[-1]["timestamp"]), key=lambda t: t["start_ts"], default=None)
                if any(t is not None and shown.get(t["id"]) for t in (before, after)):
                    places = tidy_stops(run)
                    tidy = [q for c in places for q in c]
                    # Join the places to each other and to the pushed trips
                    # either side along the road, not with straight lines.
                    for c0, c1 in zip(places, places[1:]):
                        tidy += link(c0[-1], c1[0])
                    if places and before is not None and before["id"] in streams:
                        st, res = streams[before["id"]]
                        tidy += link(st[-1], places[0][0], _first_mode(res, last=True))
                    if places and after is not None and after["id"] in streams:
                        st, res = streams[after["id"]]
                        tidy += link(places[-1][-1], st[0], _first_mode(res))
                    posts += tidy
                    n_matched += len(tidy)
                else:
                    posts += [{**p, "tracker": p["tracker_id"], "ours": False} for p in run]
            ids = [int(p["id"]) for p in remote if p.get("id") is not None]
            summary["deleted"] += len(ids)
            summary["posted"] += len(posts)
            summary["matched_points"] += n_matched
            summary["original_points"] += len(posts) - n_matched
            if not dry:
                # Delete first: Dawarich merges a posted point into an existing
                # one at the same place and second, so posting first and then
                # deleting would delete what was just written.
                dw.delete(ids)
                for attempt in range(3):
                    try:
                        dw.post(posts)
                        break
                    except Exception:
                        if attempt == 2:
                            # Every point is in the archive; note the span so a
                            # repair can write it back once Dawarich answers.
                            _record(db, uid, 0, "incomplete", None, len(ids), 0, False, {"span": [a, b]})
                            raise
                        time.sleep(1.5 * (attempt + 1))
                archive.remember_written(db, uid, [p for p in posts if p["ours"]])
            for t in inside:
                if t["id"] not in changes:
                    continue
                on = shown[t["id"]] is not None
                _record(db, uid, t["id"], "push" if on else "revert", shown[t["id"]], len(ids), len(posts), dry,
                        {"span": [a, b], "matched_points": n_matched, "original_points": len(posts) - n_matched})
                if not dry:
                    with db.tx() as c:
                        if on:
                            c.execute("UPDATE trips SET pushed_version=?, pushed_at=?, updated_at=? WHERE id=?",
                                      (t["result_version"], now(), now(), t["id"]))
                        else:
                            c.execute("UPDATE trips SET pushed_version=NULL, pushed_at='', updated_at=? WHERE id=?",
                                      (now(), t["id"]))

        for gi, (a, b) in enumerate(groups):
            say(int(100 * gi / len(groups)), f"rewriting span {gi + 1} of {len(groups)}")
            try:
                _span(a, b)
            except Exception as exc:  # noqa: BLE001
                if not keep_going:
                    raise
                summary["failed_spans"] += 1
                with db.tx() as c:
                    for t in rows:
                        if t["id"] in changes and t["start_ts"] <= b and a <= t["end_ts"]:
                            c.execute("UPDATE trips SET error=? WHERE id=?", (f"push: {exc}"[:500], t["id"]))
    say(100, "done")
    return summary


def push_trip(db: Database, cfg: Any, uid: int, trip_id: int, transport: Any = None) -> dict[str, Any]:
    t = trips.get(db, uid, trip_id)
    if t and trips.as_recorded(t) and t["pushed_version"] is None:
        raise ValueError("this trip is kept as recorded, so Dawarich already has it as it should be")
    return apply(db, cfg, uid, {trip_id: True}, transport)


def revert_trip(db: Database, cfg: Any, uid: int, trip_id: int, transport: Any = None) -> dict[str, Any]:
    if not trips.get(db, uid, trip_id):
        raise KeyError(trip_id)
    return apply(db, cfg, uid, {trip_id: False}, transport)


def repair(db: Database, cfg: Any, uid: int, transport: Any = None,
           progress: Callable[[int, str], None] | None = None) -> dict[str, Any]:
    """Rewrite every trip Dawarich has ever been changed for, as it should be.

    Fixes what pushes from before 2.4 left behind: matched points and restored
    originals on their own devices, and old tracks bridging the gap."""
    say = progress or (lambda *_: None)
    say(1, "reading every point in Dawarich")
    with client_for(db, cfg, uid, transport) as dw:
        everything = dw.points()
    originals, _ = archive.split_remote(db, uid, everything)
    added = archive.add_api(db, uid, originals)
    touched = [r["trip_id"] for r in db.all("SELECT DISTINCT trip_id FROM pushes WHERE user_id=? AND trip_id>0", (uid,))]
    touched += [r["id"] for r in db.all("SELECT id FROM trips WHERE user_id=? AND pushed_version IS NOT NULL", (uid,))]
    holes = [json.loads(r["detail"] or "{}").get("span") for r in db.all(
        "SELECT detail FROM pushes WHERE user_id=? AND kind='incomplete'", (uid,))]
    out = apply(db, cfg, uid, {}, transport, also=sorted(set(touched)),
                extra_spans=[tuple(h) for h in holes if h],
                progress=lambda p, m: say(5 + int(p * 0.94), m))
    if not cfg.push_dry_run:
        with db.tx() as c:
            c.execute("UPDATE pushes SET kind='incomplete-repaired' WHERE user_id=? AND kind='incomplete'", (uid,))
    out["archived_new"] = added
    return out


def needs_repair(db: Database, uid: int) -> bool:
    """A rewrite stopped between deleting and posting: Dawarich is missing points."""
    return db.one("SELECT 1 FROM pushes WHERE user_id=? AND kind='incomplete' LIMIT 1", (uid,)) is not None


def pending_push(db: Database, uid: int) -> list[int]:
    rows = db.all("SELECT id, quality, pushed_version FROM trips WHERE user_id=? AND closed=1 AND locked=0 AND status='matched'"
                  " AND result_version > 0 AND (pushed_version IS NULL OR pushed_version != result_version)"
                  " ORDER BY start_ts", (uid,))
    # A trip kept as recorded only needs pushing to take an older match back out.
    return [r["id"] for r in rows if not (trips.as_recorded(r) and r["pushed_version"] is None)]
