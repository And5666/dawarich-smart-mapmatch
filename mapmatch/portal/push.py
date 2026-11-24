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
            shown[t["id"]] = t["result_version"] if changes[t["id"]] else None
        else:
            shown[t["id"]] = t["pushed_version"]
    seeds = [(byid[t]["start_ts"], byid[t]["end_ts"]) for t in [*changes, *(also or [])] if t in byid]
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
                stream = [{**p, "tracker": dev, "ours": True} for p in res["stream"]
                          if t["start_ts"] <= p["timestamp"] <= t["end_ts"]]
                if len(stream) < 2:
                    raise ValueError(f"the match of trip {t['id']} is empty")
                posts += stream
                n_matched += len(stream)
                covered.append((t["start_ts"], t["end_ts"]))
            for p in fixes:
                ts = p["timestamp"]
                if not any(c <= ts <= d for c, d in covered):
                    posts.append({**p, "tracker": p["tracker_id"], "ours": False})
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
                on = changes[t["id"]]
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
    rows = db.all("SELECT id FROM trips WHERE user_id=? AND closed=1 AND locked=0 AND status='matched'"
                  " AND result_version > 0 AND (pushed_version IS NULL OR pushed_version != result_version)"
                  " ORDER BY start_ts", (uid,))
    return [r["id"] for r in rows]
