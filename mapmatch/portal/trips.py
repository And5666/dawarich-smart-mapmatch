"""Trips: segmentation of the archive, matching, results and edits."""
from __future__ import annotations

import json
import time
from typing import Any

from ..engine.api import ENGINE_VERSION, MODES, match_trip
from ..engine.assemble import simplify
from ..engine.quality import path_km
from ..engine.trips import split_trips
from . import archive
from .db import Database, now, pack, unpack

KEEP_RESULTS = 30
PREVIEW_TOLERANCE_M = 4.0
RECORDED_TOLERANCE_M = 1.0   # a line kept as recorded is drawn as recorded
PREVIEW_VERSION = "4"


# ---------------------------------------------------------------- segmentation
def segment(db: Database, uid: int, gap_min: int, new_ts: list[int] | None = None,
            now_ts: int | None = None, settle: bool = True) -> dict[str, int]:
    """Bring the trips table up to date with the archive.

    Closed trips are frozen. New points inside a closed trip mark it dirty;
    the open tail (after the last closed trip) and any history before the
    first trip are (re)split. settle=False is for a download still in
    progress: a trip then only closes when later points prove the gap, not
    because the clock says it is old (that cut long rides at every page).
    Closed trips that follow each other closer than the trip gap, and that
    nobody has pushed or edited, are split again, which joins the ones
    that should never have been cut.
    """
    now_ts = int(now_ts or time.time())
    gap = gap_min * 60
    stats = {"created": 0, "updated": 0, "removed": 0, "dirtied": 0}
    rows = db.all("SELECT * FROM trips WHERE user_id=? ORDER BY start_ts", (uid,))
    closed = [r for r in rows if r["closed"]]
    open_ = [dict(r) for r in rows if not r["closed"]]
    cut = _cut_chains(closed, gap)

    if new_ts:
        with db.tx() as c:
            for r in closed:
                if any(r["start_ts"] <= t <= r["end_ts"] for t in new_ts):
                    c.execute("UPDATE trips SET dirty=1, updated_at=? WHERE id=?", (now(), r["id"]))
                    stats["dirtied"] += 1

    last_closed_end = max((r["end_ts"] for r in closed), default=None)
    first_start = min((r["start_ts"] for r in closed), default=None)
    latest = db.one("SELECT MAX(ts) t FROM points WHERE user_id=?", (uid,))["t"] or 0

    def make(trip: list[dict[str, Any]]) -> dict[str, Any]:
        end = trip[-1]["timestamp"]
        # Until a trip is matched, its preview and distance come from the raw fixes.
        return {"start_ts": trip[0]["timestamp"], "end_ts": end, "n_points": len(trip),
                "closed": 1 if (latest > end + gap or (settle and now_ts > end + gap)) else 0,
                "preview": json.dumps(raw_preview(trip)), "km": round(path_km(trip), 3)}

    found: list[dict[str, Any]] = []
    if first_start is not None:
        head = archive.window(db, uid, None, first_start - 1)
        for t in split_trips(head, gap_s=gap, stay_s=gap):
            m = make(t)
            if m["end_ts"] < first_start - gap:
                m["closed"] = 1
                found.append(m)
    tail = archive.window(db, uid, (last_closed_end + 1) if last_closed_end is not None else None, None)
    found.extend(make(t) for t in split_trips(tail, gap_s=gap, stay_s=gap))
    for chain in cut:
        span = archive.window(db, uid, chain[0]["start_ts"], chain[-1]["end_ts"])
        found.extend({**make(t), "closed": 1} for t in split_trips(span, gap_s=gap, stay_s=gap))
        open_.extend(dict(r) for r in chain)

    with db.tx() as c:
        used: set[int] = set()
        for m in found:
            match = next((o for o in open_ if o["id"] not in used and
                          o["start_ts"] <= m["end_ts"] and m["start_ts"] <= o["end_ts"]), None)
            if match:
                used.add(match["id"])
                if (match["start_ts"], match["end_ts"], match["n_points"], match["closed"]) != \
                        (m["start_ts"], m["end_ts"], m["n_points"], m["closed"]):
                    c.execute("UPDATE trips SET start_ts=?, end_ts=?, n_points=?, closed=?, dirty=1, updated_at=?,"
                              " preview=CASE WHEN result_version=0 THEN ? ELSE preview END,"
                              " km=CASE WHEN result_version=0 THEN ? ELSE km END WHERE id=?",
                              (m["start_ts"], m["end_ts"], m["n_points"], m["closed"], now(),
                               m["preview"], m["km"], match["id"]))
                    stats["updated"] += 1
                continue
            c.execute("INSERT INTO trips(user_id, start_ts, end_ts, n_points, closed, preview, km, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (uid, m["start_ts"], m["end_ts"], m["n_points"], m["closed"], m["preview"], m["km"], now(), now()))
            stats["created"] += 1
        for o in open_:
            if o["id"] in used or o["pushed_version"] is not None:
                continue
            # An open trip that no longer exists (its points turned out to be a stay).
            c.execute("DELETE FROM trip_results WHERE trip_id=?", (o["id"],))
            c.execute("DELETE FROM trips WHERE id=?", (o["id"],))
            stats["removed"] += 1
    return stats


def _cut_chains(closed: list[Any], gap: int) -> list[list[Any]]:
    """Runs of closed trips less than a trip gap apart that can be re-split."""
    def free(r: Any) -> bool:
        return r["pushed_version"] is None and not r["locked"] and \
            (r["overrides"] or "[]") == "[]" and (r["point_edits"] or "{}") == "{}"
    out: list[list[Any]] = []
    cur: list[Any] = []
    for r in closed:
        if cur and free(r) and r["start_ts"] - cur[-1]["end_ts"] < gap:
            cur.append(r)
            continue
        if len(cur) > 1:
            out.append(cur)
        cur = [r] if free(r) else []
    if len(cur) > 1:
        out.append(cur)
    return out


# ---------------------------------------------------------------- matching
def settings(db: Database, uid: int) -> dict[str, Any]:
    row = db.one("SELECT * FROM settings WHERE user_id=?", (uid,))
    return dict(row) if row else {}


def get(db: Database, uid: int, trip_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, uid))
    return dict(row) if row else None


def points(db: Database, uid: int, trip: dict[str, Any]) -> list[dict[str, Any]]:
    """The trip's fixes as the matcher should see them: the archived originals
    with the user's corrections applied. The archive itself is never changed;
    a moved fix is trusted (5 m), a removed one is left out."""
    edits = json.loads(trip.get("point_edits") or "{}")
    out = []
    for p in archive.window(db, uid, trip["start_ts"], trip["end_ts"]):
        e = edits.get(str(p["id"]))
        if e is None:
            out.append(p)
        elif e != "x":
            out.append({**p, "lat": float(e[0]), "lon": float(e[1]), "accuracy": 5.0})
    return out


def set_point_edits(db: Database, uid: int, trip_id: int, edits: dict[str, Any]) -> None:
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    ids = {str(p["id"]) for p in archive.window(db, uid, trip["start_ts"], trip["end_ts"])}
    clean: dict[str, Any] = {}
    for k, v in (edits or {}).items():
        if str(k) not in ids:
            raise ValueError(f"point {k} is not part of this trip")
        if v == "x":
            clean[str(k)] = "x"
        else:
            lat, lon = float(v[0]), float(v[1])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("bad coordinates")
            clean[str(k)] = [round(lat, 7), round(lon, 7)]
    with db.tx() as c:
        c.execute("UPDATE trips SET point_edits=?, updated_at=? WHERE id=?", (json.dumps(clean), now(), trip_id))


def _summary(result: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in result["segments"]:
        if s["mode"] == "stay":
            continue
        if out and out[-1]["mode"] == s["mode"]:
            out[-1]["km"] = round(out[-1]["km"] + s["km"], 3)
        else:
            out.append({"mode": s["mode"], "km": s["km"]})
    return out


def _simplified(coords: list[Any], tol: float = PREVIEW_TOLERANCE_M) -> list[list[float]]:
    pts = simplify([(c[0], c[1]) for c in coords], tol)
    return [[round(lat, 6), round(lon, 6)] for lat, lon in pts]


def raw_preview(fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overview line for an unmatched trip: its raw fixes, one grey part."""
    return [{"m": "none", "c": _simplified([(p["lat"], p["lon"]) for p in fixes])}]


def matched_preview(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Overview line for a matched trip: one part per segment, so each is drawn
    in its own mode colour. Simplified by shape (within 4 m, or 1 m where the
    GPS is kept as recorded), never by count. A stop is a dot, unless its
    fixes are kept as recorded: then they are part of the line."""
    parts = []
    for s in result["segments"]:
        recorded = bool(s["pieces"]) and all(p["kind"] == "recorded" for p in s["pieces"])
        if s["mode"] == "stay" and not recorded:
            continue
        coords = [c for p in s["pieces"] for c in p["coords"]]
        if len(coords) >= 2:
            parts.append({"m": s["mode"], "c": _simplified(coords, RECORDED_TOLERANCE_M if recorded
                                                           else PREVIEW_TOLERANCE_M)})
    return parts


def refresh_previews(db: Database) -> int:
    """Recompute stored previews after the preview format changes."""
    row = db.one("SELECT value FROM site WHERE key='preview_version'")
    if row and row["value"] == PREVIEW_VERSION:
        return 0
    n = 0
    for t in db.all("SELECT * FROM trips"):
        t = dict(t)
        res = result(db, t)
        prev = matched_preview(res) if res else raw_preview(archive.window(db, t["user_id"], t["start_ts"], t["end_ts"]))
        with db.tx() as c:
            c.execute("UPDATE trips SET preview=? WHERE id=?", (json.dumps(prev), t["id"]))
        n += 1
    with db.tx() as c:
        c.execute("INSERT INTO site(key, value) VALUES ('preview_version', ?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PREVIEW_VERSION,))
    return n


CHOICES = ("gap_mode", "keep_gaps", "force_route", "force_gaps", "route_gaps", "point_edits", "snap_recorded")


def choices_of(trip: dict[str, Any]) -> dict[str, Any]:
    """The trip's own settings that shaped a match, kept with each version so
    going back to it brings them back too."""
    return {k: trip.get(k) for k in CHOICES if k in trip}


def store(db: Database, trip: dict[str, Any], result: dict[str, Any], overrides: list[dict[str, Any]],
          reason: str) -> int:
    with db.tx() as c:
        # After a trip is cleared its result_version is 0 while older versions
        # are still kept, so number on from the highest one.
        top = c.execute("SELECT MAX(version) v FROM trip_results WHERE trip_id=?", (trip["id"],)).fetchone()["v"]
        version = max(int(trip["result_version"]), int(top or 0)) + 1
        c.execute("INSERT INTO trip_results(trip_id, version, reason, engine_version, overrides, quality,"
                  " result_gz, created_at, choices) VALUES (?,?,?,?,?,?,?,?,?)",
                  (trip["id"], version, reason, result["engine_version"], json.dumps(overrides),
                   json.dumps(result["quality"]), pack(result), now(), json.dumps(choices_of(trip))))
        # Old versions go, except the one Dawarich holds: a later push of a
        # neighbouring trip re-posts it.
        c.execute("DELETE FROM trip_results WHERE trip_id=? AND version <= ? AND version IS NOT"
                  " (SELECT pushed_version FROM trips WHERE id=?)", (trip["id"], version - KEEP_RESULTS, trip["id"]))
        c.execute("UPDATE trips SET status='matched', dirty=0, error='', engine_version=?, result_version=?,"
                  " overrides=?, km=?, modes=?, quality=?, preview=?, updated_at=? WHERE id=?",
                  (result["engine_version"], version, json.dumps(overrides), result["quality"].get("km", 0.0),
                   json.dumps(_summary(result)), json.dumps(result["quality"]),
                   json.dumps(matched_preview(result)), now(), trip["id"]))
    return version


def clear(db: Database, uid: int, trip_id: int) -> dict[str, Any]:
    """Back to the original GPS in mapmatch: no match, no mode choices, no
    gap choices, no point corrections. Earlier versions stay in the History,
    with the choices they were made with, so this can be undone. Dawarich is
    not touched (a pushed trip then shows as changed since pushed)."""
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    pts = archive.window(db, uid, trip["start_ts"], trip["end_ts"])
    with db.tx() as c:
        c.execute("UPDATE trips SET status='new', dirty=0, error='', result_version=0, engine_version='',"
                  " overrides='[]', modes='[]', quality='{}', km=?, preview=?, gap_mode='', keep_gaps='[]',"
                  " force_route=0, force_gaps='[]', route_gaps='[]', snap_recorded=0, point_edits='{}',"
                  " updated_at=? WHERE id=?",
                  (round(path_km(pts), 3), json.dumps(raw_preview(pts)), now(), trip_id))
    return get(db, uid, trip_id)


MOVING = ("walk", "cycle", "drive", "train")
DETAILED = ("keep", "snap")


def match_modes(value: Any) -> list[str]:
    """The modes snapped to the map, from a stored "walk,cycle" string or a list."""
    items = value.split(",") if isinstance(value, str) else list(value or [])
    return [m for m in MOVING if m in {str(x).strip() for x in items}]


def match_choices(s: dict[str, Any], once: dict[str, Any] | None = None) -> dict[str, Any]:
    """What gets matched: the account's saved choices, or those of a one-off
    run. Each trip's own choices still win over either."""
    o = once or {}
    modes = o.get("match_modes")
    return {"match_modes": match_modes(modes if modes is not None else s.get("match_modes", ",".join(MOVING))),
            "detailed": o.get("detailed") or s.get("detailed") or "keep",
            "strength": o.get("strength") or s.get("strength") or "fill"}


def match(db: Database, uid: int, trip_id: int, reason: str = "auto",
          overrides: list[dict[str, Any]] | None = None, once: dict[str, Any] | None = None) -> dict[str, Any]:
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    ov = json.loads(trip["overrides"]) if overrides is None else overrides
    ov = validate_overrides(ov)
    pts = points(db, uid, trip)
    ch = match_choices(settings(db, uid), once)
    # The trip can choose for itself; otherwise the account's setting applies.
    strength = trip.get("gap_mode") or ch["strength"]
    own = int(trip.get("snap_recorded") or 0)
    snap = own == 1 or (own == 0 and ch["detailed"] == "snap")
    # Holes picked out one by one in the editor stay as recorded whatever the above says.
    keep = json.loads(trip.get("keep_gaps") or "[]")
    # Holes the user insisted on routing, past the speed and length checks.
    force_all = bool(trip.get("force_route"))
    force = json.loads(trip.get("force_gaps") or "[]")
    route = json.loads(trip.get("route_gaps") or "[]")
    try:
        result = match_trip(pts, overrides=ov, options={"strength": strength, "keep_gaps": keep,
                                                        "force_route": force_all, "force_gaps": force, "route_gaps": route,
                                                        "snap_recorded": snap, "match_modes": ch["match_modes"]})
    except Exception as exc:  # noqa: BLE001 - recorded on the trip and shown
        with db.tx() as c:
            c.execute("UPDATE trips SET status='failed', dirty=0, error=?, updated_at=? WHERE id=?",
                      (f"{type(exc).__name__}: {exc}"[:500], now(), trip_id))
        raise
    store(db, trip, result, ov, reason)
    return result


GAP_MODES = ("", "fill", "strict")


def set_gap_mode(db: Database, uid: int, trip_id: int, mode: str) -> dict[str, Any]:
    """Choose per trip what happens in the gaps: "fill", "strict", or "" to
    fall back to the account's setting."""
    if mode not in GAP_MODES:
        raise ValueError("gap mode must be fill, strict or empty")
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET gap_mode=?, updated_at=? WHERE id=?", (mode, now(), trip_id))
    return get(db, uid, trip_id)


def set_keep_gaps(db: Database, uid: int, trip_id: int, spans: list[Any]) -> dict[str, Any]:
    """Remember which individual holes stay as recorded: [[t0, t1], ...]."""
    clean: list[list[int]] = []
    for span in spans or []:
        t0, t1 = int(span[0]), int(span[1])
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        if not any(lo <= c[1] and c[0] <= hi for c in clean):
            clean.append([lo, hi])
    clean.sort()
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET keep_gaps=?, updated_at=? WHERE id=?", (json.dumps(clean), now(), trip_id))
    return get(db, uid, trip_id)


def set_force_route(db: Database, uid: int, trip_id: int, on: bool) -> None:
    """Route every gap on this trip even where the plausibility gate refuses it."""
    if not get(db, uid, trip_id):
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET force_route=?, updated_at=? WHERE id=?", (1 if on else 0, now(), trip_id))


def set_snap_recorded(db: Database, uid: int, trip_id: int, on: bool | None) -> None:
    """Snap this trip's detailed stretches to the map (True), keep them as
    recorded (False), or do what the account says (None)."""
    if not get(db, uid, trip_id):
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET snap_recorded=?, updated_at=? WHERE id=?",
                  (0 if on is None else 1 if on else -1, now(), trip_id))


def set_route_gaps(db: Database, uid: int, trip_id: int, spans: list[Any]) -> None:
    """The individual holes to route, with the usual checks, in a trip that
    otherwise keeps its gaps: [[t0, t1], ...]."""
    clean = sorted([min(int(a), int(b)), max(int(a), int(b))] for a, b in (spans or []))
    if not get(db, uid, trip_id):
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET route_gaps=?, updated_at=? WHERE id=?", (json.dumps(clean), now(), trip_id))


def set_force_gaps(db: Database, uid: int, trip_id: int, spans: list[Any]) -> None:
    """The individual holes to route regardless of the gate: [[t0, t1], ...]."""
    clean: list[list[int]] = []
    for span in spans or []:
        t0, t1 = int(span[0]), int(span[1])
        lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
        if not any(lo <= c[1] and c[0] <= hi for c in clean):
            clean.append([lo, hi])
    clean.sort()
    if not get(db, uid, trip_id):
        raise KeyError(trip_id)
    with db.tx() as c:
        c.execute("UPDATE trips SET force_gaps=?, updated_at=? WHERE id=?", (json.dumps(clean), now(), trip_id))


def validate_overrides(ov: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for o in ov or []:
        mode = str(o.get("mode", ""))
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        t0, t1 = int(o["t0"]), int(o["t1"])
        if t1 < t0:
            t0, t1 = t1, t0
        out.append({"t0": t0, "t1": t1, "mode": mode})
    return out


def result(db: Database, trip: dict[str, Any], version: int | None = None) -> dict[str, Any] | None:
    v = version or trip["result_version"]
    if not v:
        return None
    row = db.one("SELECT result_gz FROM trip_results WHERE trip_id=? AND version=?", (trip["id"], v))
    return unpack(row["result_gz"]) if row else None


def history(db: Database, trip_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in db.all(
        "SELECT version, reason, engine_version, overrides, quality, created_at FROM trip_results"
        " WHERE trip_id=? ORDER BY version DESC", (trip_id,))]


def version(db: Database, trip_id: int, v: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM trip_results WHERE trip_id=? AND version=?", (trip_id, v))
    if not row:
        return None
    return {"version": row["version"], "reason": row["reason"], "created_at": row["created_at"],
            "engine_version": row["engine_version"], "overrides": json.loads(row["overrides"] or "[]"),
            "choices": json.loads(row["choices"] or "{}"), "result": unpack(row["result_gz"])}


def restore(db: Database, uid: int, trip_id: int, v: int) -> int:
    """Make an earlier version current again, with the choices it was made
    with, as a new version (so this is undoable too)."""
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    old = version(db, trip_id, v)
    if not old:
        raise ValueError(f"version {v} is no longer kept")
    ch = old["choices"]
    if ch:
        sets = [k for k in CHOICES if k in ch and ch[k] is not None]
        if sets:
            with db.tx() as c:
                c.execute(f"UPDATE trips SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                          (*[ch[k] for k in sets], trip_id))
            trip = get(db, uid, trip_id)
    return store(db, trip, old["result"], old["overrides"], f"back to v{v}")


def undo(db: Database, uid: int, trip_id: int) -> int:
    """Make the previous result current again (as a new version, so undo is undoable)."""
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    # A cleared trip (result_version 0) undoes back to its last match.
    prev = db.one("SELECT version FROM trip_results WHERE trip_id=? AND version < ? ORDER BY version DESC LIMIT 1",
                  (trip_id, trip["result_version"] or 2**62))
    if not prev:
        raise ValueError("nothing to undo")
    return restore(db, uid, trip_id, int(prev["version"]))


def range_override(trip: dict[str, Any], t0: int, t1: int, mode: str) -> list[dict[str, Any]]:
    """Overrides after pinning [t0, t1] to a mode. Earlier overrides keep the
    parts that fall outside the new range."""
    t0, t1 = sorted((int(t0), int(t1)))
    out = []
    for o in json.loads(trip["overrides"]):
        if o["t1"] < t0 or o["t0"] > t1:
            out.append(o)
            continue
        if o["t0"] < t0:
            out.append({**o, "t1": t0 - 1})
        if o["t1"] > t1:
            out.append({**o, "t0": t1 + 1})
    out.append({"t0": t0, "t1": t1, "mode": mode})
    return sorted(out, key=lambda o: o["t0"])


def segment_override(trip: dict[str, Any], res: dict[str, Any], seg_id: int, mode: str) -> list[dict[str, Any]]:
    seg = next((s for s in res["segments"] if s["id"] == seg_id), None)
    if seg is None:
        raise KeyError(seg_id)
    return range_override(trip, seg["t0"], seg["t1"], mode)


def split_override(trip: dict[str, Any], res: dict[str, Any], ts: int) -> list[dict[str, Any]]:
    """Split the segment containing ts into two pinned halves of the same mode."""
    seg = next((s for s in res["segments"] if s["t0"] < ts < s["t1"]), None)
    if seg is None:
        raise ValueError("no segment spans that time")
    ov = range_override(trip, seg["t0"], ts - 1, seg["mode"])
    return range_override({**trip, "overrides": json.dumps(ov)}, ts, seg["t1"], seg["mode"])


def as_recorded(trip: Any) -> bool:
    """The trip's current match leaves it exactly as recorded (a watch or bike
    computer track): there is nothing to change in Dawarich."""
    try:
        return bool(json.loads(trip["quality"] or "{}").get("as_recorded"))
    except (KeyError, TypeError, ValueError):
        return False


NO_MAP_REVIEW = 0.25   # this much of a trip with no map under it: unmatched parts are expected


def needs_review(q: dict[str, Any]) -> bool:
    if q.get("no_map_frac", 0) >= NO_MAP_REVIEW:
        return False
    return bool(q) and (q.get("raw_frac", 0) > 0.15 or q.get("p95", 0) > 60)


def public(trip: dict[str, Any]) -> dict[str, Any]:
    """Trip summary for the API."""
    q = json.loads(trip["quality"] or "{}")
    if trip["locked"]:
        state = "locked"
    elif trip["status"] == "failed":
        state = "failed"
    elif not trip["result_version"]:
        state = "new"
    elif needs_review(q):
        state = "review"
    else:
        state = "matched"
    pushed = trip["pushed_version"]
    push_state = "original" if pushed is None else "pushed" if pushed == trip["result_version"] else "outdated"
    if q.get("as_recorded") and pushed is None:
        push_state = "recorded"     # Dawarich already has exactly this
    return {
        "id": trip["id"], "start": trip["start_ts"], "end": trip["end_ts"], "n_points": trip["n_points"],
        "closed": bool(trip["closed"]), "state": state, "push": push_state, "locked": bool(trip["locked"]),
        "dirty": bool(trip["dirty"]), "error": trip["error"], "km": trip["km"],
        "modes": json.loads(trip["modes"] or "[]"), "quality": q,
        "overrides": json.loads(trip["overrides"] or "[]"), "version": trip["result_version"],
        "point_edits": len(json.loads(trip.get("point_edits") or "{}")),
        "no_map": q.get("no_map_frac", 0) >= NO_MAP_REVIEW,
        "gap_mode": trip.get("gap_mode") or "",
        "keep_gaps": json.loads(trip.get("keep_gaps") or "[]"),
        "force_route": bool(trip.get("force_route")),
        "snap_recorded": int(trip.get("snap_recorded") or 0) == 1,
        "recorded_choice": {1: "snap", -1: "keep"}.get(int(trip.get("snap_recorded") or 0), ""),
        "force_gaps": json.loads(trip.get("force_gaps") or "[]"),
        "route_gaps": json.loads(trip.get("route_gaps") or "[]"),
        "pushed_version": pushed, "pushed_at": trip["pushed_at"],
        "engine_version": trip["engine_version"], "outdated_engine": bool(trip["result_version"]) and
        trip["engine_version"] != ENGINE_VERSION,
    }
