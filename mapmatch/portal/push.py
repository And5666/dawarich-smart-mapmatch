"""Write matched trips into Dawarich, in place, and restore originals.

Safety rules:
- Before anything is deleted, every Dawarich point in the trip window is
  (re)archived, and only originals that are confirmed in the archive are
  deleted. Points the portal itself wrote are tagged with tracker
  "mapmatch" and are the only other points ever deleted.
- Revert re-posts the archived originals of the window; the archive is
  never modified by a push or a revert.
- PUSH_DRY_RUN computes and records the plan without changing Dawarich.
"""
from __future__ import annotations

import json
from typing import Any

from . import archive, trips
from .dawarich import Client, is_mapmatch
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


def _split_remote(db: Database, uid: int, remote: list[dict[str, Any]]) -> tuple[list[int], list[int], int]:
    """(original ids confirmed archived, mapmatch ids, originals NOT archived)."""
    originals = [p for p in remote if not is_mapmatch(p)]
    archive.add_api(db, uid, originals)
    from ..engine.io import normalize
    safe, missing = [], 0
    for raw in originals:
        p = normalize(raw)
        if p and raw.get("id") is not None and archive.contains(db, uid, p):
            safe.append(int(raw["id"]))
        else:
            missing += 1
    ours = [int(p["id"]) for p in remote if is_mapmatch(p) and p.get("id") is not None]
    return safe, ours, missing


def push_trip(db: Database, cfg: Any, uid: int, trip_id: int, transport: Any = None) -> dict[str, Any]:
    trip = trips.get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    if trip["locked"]:
        raise ValueError("trip is locked; unlock it to push")
    if not trip["closed"]:
        raise ValueError("trip is still in progress")
    res = trips.result(db, trip)
    if not res:
        raise ValueError("trip has no match yet")
    stream = [p for p in res["stream"] if trip["start_ts"] <= p["timestamp"] <= trip["end_ts"]]
    if len(stream) < 2:
        raise ValueError("matched trip is empty")
    dry = cfg.push_dry_run
    with client_for(db, cfg, uid, transport) as dw:
        remote = dw.points(trip["start_ts"], trip["end_ts"])
        safe, ours, missing = _split_remote(db, uid, remote)
        if missing:
            # Something we could not archive: refuse rather than lose it.
            raise RuntimeError(f"{missing} Dawarich points in this trip could not be archived; nothing changed")
        plan = {"delete_originals": len(safe), "delete_previous_mapmatch": len(ours), "post": len(stream)}
        if not dry:
            dw.post(stream)
            dw.delete(safe + ours)
    _record(db, uid, trip_id, "push", trip["result_version"], len(safe) + len(ours), len(stream), dry, plan)
    if not dry:
        with db.tx() as c:
            c.execute("UPDATE trips SET pushed_version=?, pushed_at=?, updated_at=? WHERE id=?",
                      (trip["result_version"], now(), now(), trip_id))
    return {"dry_run": dry, **plan}


def revert_trip(db: Database, cfg: Any, uid: int, trip_id: int, transport: Any = None) -> dict[str, Any]:
    trip = trips.get(db, uid, trip_id)
    if not trip:
        raise KeyError(trip_id)
    originals = archive.window(db, uid, trip["start_ts"], trip["end_ts"])
    dry = cfg.push_dry_run
    with client_for(db, cfg, uid, transport) as dw:
        remote = dw.points(trip["start_ts"], trip["end_ts"])
        ours = [int(p["id"]) for p in remote if is_mapmatch(p) and p.get("id") is not None]
        present = {archive.key(p.get("latitude", p.get("lat")), p.get("longitude", p.get("lon"))) +
                   (int(p["timestamp"]),) for p in remote if not is_mapmatch(p)}
        # Only re-post originals that are not already in Dawarich.
        todo = [{**p, "device": p["tracker_id"] or "restored"} for p in originals
                if archive.key(p["lat"], p["lon"]) + (int(p["timestamp"]),) not in present]
        plan = {"delete_mapmatch": len(ours), "restore": len(todo), "already_present": len(originals) - len(todo)}
        if not dry:
            # Originals first, then remove ours: a failure in between leaves
            # duplicates, never a hole.
            if todo:
                for dev in sorted({p["device"] for p in todo}):
                    dw.post([p for p in todo if p["device"] == dev], device=dev)
            dw.delete(ours)
    _record(db, uid, trip_id, "revert", None, len(ours), len(todo), dry, plan)
    if not dry:
        with db.tx() as c:
            c.execute("UPDATE trips SET pushed_version=NULL, pushed_at='', updated_at=? WHERE id=?", (now(), trip_id))
    return {"dry_run": dry, **plan}


def pending_push(db: Database, uid: int) -> list[int]:
    rows = db.all("SELECT id FROM trips WHERE user_id=? AND closed=1 AND locked=0 AND status='matched'"
                  " AND result_version > 0 AND (pushed_version IS NULL OR pushed_version != result_version)"
                  " ORDER BY start_ts", (uid,))
    return [r["id"] for r in rows]
