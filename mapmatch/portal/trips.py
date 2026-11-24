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

KEEP_RESULTS = 15
PREVIEW_TOLERANCE_M = 4.0
PREVIEW_VERSION = "2"


# ---------------------------------------------------------------- segmentation
def segment(db: Database, uid: int, gap_min: int, new_ts: list[int] | None = None,
            now_ts: int | None = None) -> dict[str, int]:
    """Bring the trips table up to date with the archive.

    Closed trips are frozen. New points inside a closed trip mark it dirty;
    the open tail (after the last closed trip) and any history before the
    first trip are (re)split.
    """
    now_ts = int(now_ts or time.time())
    gap = gap_min * 60
    stats = {"created": 0, "updated": 0, "removed": 0, "dirtied": 0}
    rows = db.all("SELECT * FROM trips WHERE user_id=? ORDER BY start_ts", (uid,))
    closed = [r for r in rows if r["closed"]]
    open_ = [dict(r) for r in rows if not r["closed"]]

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
                "closed": 1 if (latest > end + gap or now_ts > end + gap) else 0,
                "preview": json.dumps(_preview(trip)), "km": round(path_km(trip), 3)}

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


# ---------------------------------------------------------------- matching
def settings(db: Database, uid: int) -> dict[str, Any]:
    row = db.one("SELECT * FROM settings WHERE user_id=?", (uid,))
    return dict(row) if row else {}


def get(db: Database, uid: int, trip_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM trips WHERE id=? AND user_id=?", (trip_id, uid))
    return dict(row) if row else None


def points(db: Database, uid: int, trip: dict[str, Any]) -> list[dict[str, Any]]:
    return archive.window(db, uid, trip["start_ts"], trip["end_ts"])


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


def _preview(stream: list[dict[str, Any]]) -> list[list[float]]:
    """The trip's line for the overview map: simplified by shape (every point
    within PREVIEW_TOLERANCE_M of the real route), never by count, so a
    matched route still follows the roads when zoomed in."""
    pts = simplify([(p["lat"], p["lon"]) for p in stream], PREVIEW_TOLERANCE_M)
    return [[round(lat, 6), round(lon, 6)] for lat, lon in pts]


def refresh_previews(db: Database) -> int:
    """Recompute stored previews after the preview format changes."""
    row = db.one("SELECT value FROM site WHERE key='preview_version'")
    if row and row["value"] == PREVIEW_VERSION:
        return 0
    n = 0
    for t in db.all("SELECT * FROM trips"):
        t = dict(t)
        res = result(db, t)
        pts = res["stream"] if res else archive.window(db, t["user_id"], t["start_ts"], t["end_ts"])
        with db.tx() as c:
            c.execute("UPDATE trips SET preview=? WHERE id=?", (json.dumps(_preview(pts)), t["id"]))
        n += 1
    with db.tx() as c:
        c.execute("INSERT INTO site(key, value) VALUES ('preview_version', ?)"
                  " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (PREVIEW_VERSION,))
    return n


def store(db: Database, trip: dict[str, Any], result: dict[str, Any], overrides: list[dict[str, Any]],
          reason: str) -> int:
    version = int(trip["result_version"]) + 1
    with db.tx() as c:
        c.execute("INSERT INTO trip_results(trip_id, version, reason, engine_version, overrides, quality,"
                  " result_gz, created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (trip["id"], version, reason, result["engine_version"], json.dumps(overrides),
                   json.dumps(result["quality"]), pack(result), now()))
        c.execute("DELETE FROM trip_results WHERE trip_id=? AND version <= ?", (trip["id"], version - KEEP_RESULTS))
        c.execute("UPDATE trips SET status='matched', dirty=0, error='', engine_version=?, result_version=?,"
                  " overrides=?, km=?, modes=?, quality=?, preview=?, updated_at=? WHERE id=?",
                  (result["engine_version"], version, json.dumps(overrides), result["quality"].get("km", 0.0),
                   json.dumps(_summary(result)), json.dumps(result["quality"]),
                   json.dumps(_preview(result["stream"])), now(), trip["id"]))
    return version


def match(db: Database, uid: int, trip_id: int, reason: str = "auto",
          overrides: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    ov = json.loads(trip["overrides"]) if overrides is None else overrides
    ov = validate_overrides(ov)
    pts = points(db, uid, trip)
    strength = settings(db, uid).get("strength") or "fill"
    try:
        result = match_trip(pts, overrides=ov, options={"strength": strength})
    except Exception as exc:  # noqa: BLE001 - recorded on the trip and shown
        with db.tx() as c:
            c.execute("UPDATE trips SET status='failed', dirty=0, error=?, updated_at=? WHERE id=?",
                      (f"{type(exc).__name__}: {exc}"[:500], now(), trip_id))
        raise
    store(db, trip, result, ov, reason)
    return result


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


def undo(db: Database, uid: int, trip_id: int) -> int:
    """Make the previous result current again (as a new version, so undo is undoable)."""
    trip = get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    prev = db.one("SELECT * FROM trip_results WHERE trip_id=? AND version < ? ORDER BY version DESC LIMIT 1",
                  (trip_id, trip["result_version"]))
    if not prev:
        raise ValueError("nothing to undo")
    return store(db, trip, unpack(prev["result_gz"]), json.loads(prev["overrides"]), f"undo to v{prev['version']}")


def segment_override(trip: dict[str, Any], res: dict[str, Any], seg_id: int, mode: str) -> list[dict[str, Any]]:
    """Overrides after setting one segment's mode. Existing overrides that the
    segment covers are replaced; the rest are kept."""
    seg = next((s for s in res["segments"] if s["id"] == seg_id), None)
    if seg is None:
        raise KeyError(seg_id)
    t0, t1 = int(seg["t0"]), int(seg["t1"])
    ov = [o for o in json.loads(trip["overrides"]) if o["t1"] < t0 or o["t0"] > t1]
    ov.append({"t0": t0, "t1": t1, "mode": mode})
    return sorted(ov, key=lambda o: o["t0"])


def split_override(trip: dict[str, Any], res: dict[str, Any], ts: int) -> list[dict[str, Any]]:
    """Split the segment containing ts into two pinned halves of the same mode."""
    seg = next((s for s in res["segments"] if s["t0"] < ts < s["t1"]), None)
    if seg is None:
        raise ValueError("no segment spans that time")
    ov = [o for o in json.loads(trip["overrides"]) if o["t1"] < seg["t0"] or o["t0"] > seg["t1"]]
    ov += [{"t0": seg["t0"], "t1": ts - 1, "mode": seg["mode"]},
           {"t0": ts, "t1": seg["t1"], "mode": seg["mode"]}]
    return sorted(ov, key=lambda o: o["t0"])


def needs_review(q: dict[str, Any]) -> bool:
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
    return {
        "id": trip["id"], "start": trip["start_ts"], "end": trip["end_ts"], "n_points": trip["n_points"],
        "closed": bool(trip["closed"]), "state": state, "push": push_state, "locked": bool(trip["locked"]),
        "dirty": bool(trip["dirty"]), "error": trip["error"], "km": trip["km"],
        "modes": json.loads(trip["modes"] or "[]"), "quality": q,
        "overrides": json.loads(trip["overrides"] or "[]"), "version": trip["result_version"],
        "pushed_version": pushed, "pushed_at": trip["pushed_at"],
        "engine_version": trip["engine_version"], "outdated_engine": bool(trip["result_version"]) and
        trip["engine_version"] != ENGINE_VERSION,
    }
