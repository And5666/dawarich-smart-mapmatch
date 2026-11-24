"""The archive of original GPS fixes.

Insert-only by design: there is no function here, or anywhere in the portal,
that deletes archived points. Re-importing the same fix is a no-op apart from
recording its current Dawarich id.
"""
from __future__ import annotations

from typing import Any, Iterable

from ..engine import io as gio
from .db import Database, now


def key(lat: float, lon: float) -> tuple[int, int]:
    return round(float(lat) * 1e6), round(float(lon) * 1e6)


def add(db: Database, user_id: int, points: Iterable[dict[str, Any]], source: str) -> int:
    """Archive normalized points. Returns how many were new."""
    stamp = now()
    rows = []
    for p in points:
        la, lo = key(p["lat"], p["lon"])
        did = p.get("id")
        rows.append((user_id, int(p["timestamp"]), float(p["lat"]), float(p["lon"]), la, lo,
                     p.get("accuracy"), p.get("velocity"), p.get("altitude"),
                     str(p.get("tracker_id") or ""), int(did) if did not in (None, "") else None,
                     source, stamp))
    if not rows:
        return 0
    with db.tx() as c:
        before = c.execute("SELECT COUNT(*) FROM points WHERE user_id=?", (user_id,)).fetchone()[0]
        c.executemany(
            "INSERT INTO points(user_id, ts, lat, lon, lat_e6, lon_e6, accuracy, velocity, altitude,"
            " tracker_id, dawarich_id, source, archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user_id, ts, lat_e6, lon_e6) DO UPDATE SET"
            " dawarich_id = COALESCE(excluded.dawarich_id, points.dawarich_id)",
            rows)
        after = c.execute("SELECT COUNT(*) FROM points WHERE user_id=?", (user_id,)).fetchone()[0]
    return after - before


def add_api(db: Database, user_id: int, raw: Iterable[dict[str, Any]]) -> int:
    return add(db, user_id, gio.from_api(raw), "api")


def add_csv(db: Database, user_id: int, text: str) -> int:
    return add(db, user_id, gio.read_csv_text(text), "csv")


def _row(r: Any) -> dict[str, Any]:
    return {"lat": r["lat"], "lon": r["lon"], "timestamp": r["ts"], "accuracy": r["accuracy"],
            "velocity": r["velocity"], "altitude": r["altitude"], "tracker_id": r["tracker_id"],
            "dawarich_id": r["dawarich_id"], "id": r["id"]}


def window(db: Database, user_id: int, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM points WHERE user_id=?"
    args: list[Any] = [user_id]
    if start is not None:
        sql += " AND ts >= ?"
        args.append(int(start))
    if end is not None:
        sql += " AND ts <= ?"
        args.append(int(end))
    return [_row(r) for r in db.all(sql + " ORDER BY ts, lat, lon", tuple(args))]


def contains(db: Database, user_id: int, p: dict[str, Any]) -> bool:
    la, lo = key(p["lat"], p["lon"])
    return db.one("SELECT 1 FROM points WHERE user_id=? AND ts=? AND lat_e6=? AND lon_e6=?",
                  (user_id, int(p["timestamp"]), la, lo)) is not None


def stats(db: Database, user_id: int) -> dict[str, Any]:
    r = db.one("SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM points WHERE user_id=?", (user_id,))
    return {"points": r["n"], "first": r["a"], "last": r["b"]}
