"""Background work: a persisted job queue, one worker, and the auto scheduler."""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from . import archive, push, trips
from .db import Database, now

log = logging.getLogger("mapmatch.jobs")
KINDS = ("sync", "auto", "import_csv", "rematch", "push_all", "revert_all", "repair")


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self, db: Database, cfg: Any, transport: Any = None):
        self.db = db
        self.cfg = cfg
        self.transport = transport  # tests inject a fake Dawarich
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._uploads: dict[str, str] = {}
        with db.tx() as c:
            c.execute("UPDATE jobs SET status='failed', error='interrupted by restart', finished_at=?"
                      " WHERE status IN ('running', 'queued')", (now(),))

    # ------------------------------------------------------------ lifecycle
    def start(self, scheduler: bool = True) -> None:
        t = threading.Thread(target=self._worker, name="mapmatch-worker", daemon=True)
        t.start()
        self._threads.append(t)
        if scheduler:
            s = threading.Thread(target=self._scheduler, name="mapmatch-scheduler", daemon=True)
            s.start()
            self._threads.append(s)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------ queue
    def enqueue(self, uid: int, kind: str, params: dict[str, Any] | None = None, upload: str | None = None) -> str:
        if kind not in KINDS:
            raise ValueError(kind)
        active = self.db.one("SELECT id FROM jobs WHERE user_id=? AND kind=? AND status IN ('queued','running')",
                             (uid, kind))
        if active:
            return active["id"]
        jid = uuid.uuid4().hex[:12]
        if upload is not None:
            self._uploads[jid] = upload
        with self.db.tx() as c:
            c.execute("INSERT INTO jobs(id, user_id, kind, params, created_at) VALUES (?,?,?,?,?)",
                      (jid, uid, kind, json.dumps(params or {}), now()))
        self._wake.set()
        return jid

    def cancel(self, uid: int, jid: str) -> bool:
        with self.db.tx() as c:
            cur = c.execute("UPDATE jobs SET cancel=1, message='cancelling' WHERE id=? AND user_id=?"
                            " AND status IN ('queued','running')", (jid, uid))
        return cur.rowcount > 0

    def run_pending(self) -> int:
        """Run queued jobs now, in this thread (used by tests)."""
        n = 0
        while self._next():
            n += 1
        return n

    def _worker(self) -> None:
        while not self._stop.is_set():
            if not self._next():
                self._wake.wait(5)
                self._wake.clear()

    def _next(self) -> bool:
        row = self.db.one("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1")
        if not row:
            return False
        jid = row["id"]
        with self.db.tx() as c:
            if row["cancel"]:
                c.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?", (now(), jid))
                return True
            c.execute("UPDATE jobs SET status='running', started_at=?, message='starting' WHERE id=?", (now(), jid))
        progress = self._progress(jid)
        try:
            getattr(self, f"_job_{row['kind']}")(row["user_id"], json.loads(row["params"]), progress, jid)
            status, err = "done", ""
        except Cancelled:
            status, err = "cancelled", ""
        except Exception as exc:  # noqa: BLE001 - shown in the Activity view
            log.exception("job %s failed", jid)
            status, err = "failed", f"{type(exc).__name__}: {exc}"
            with self.db.tx() as c:
                c.execute("UPDATE settings SET last_error=? WHERE user_id=?", (err[:500], row["user_id"]))
        self._uploads.pop(jid, None)
        with self.db.tx() as c:
            c.execute("UPDATE jobs SET status=?, error=?, finished_at=?, progress=CASE WHEN ?='done' THEN 100"
                      " ELSE progress END, message=CASE WHEN ?='done' AND message='' THEN 'done' ELSE message END"
                      " WHERE id=?", (status, err[:2000], now(), status, status, jid))
        return True

    def _progress(self, jid: str) -> Callable[[int, str], None]:
        def report(pct: int, msg: str) -> None:
            with self.db.tx() as c:
                c.execute("UPDATE jobs SET progress=?, message=? WHERE id=?", (int(pct), msg[:300], jid))
            row = self.db.one("SELECT cancel FROM jobs WHERE id=?", (jid,))
            if row and row["cancel"]:
                raise Cancelled()
        return report

    # ------------------------------------------------------------ scheduler
    def _scheduler(self) -> None:
        while not self._stop.wait(60):
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")

    def tick(self, now_dt: datetime | None = None) -> list[str]:
        now_dt = now_dt or datetime.now(timezone.utc)
        started = []
        for s in self.db.all("SELECT * FROM settings WHERE auto_enabled=1 AND api_key != ''"):
            last = datetime.fromisoformat(s["last_run"]) if s["last_run"] else None
            if last and (now_dt - last).total_seconds() < max(15, s["interval_min"]) * 60:
                continue
            started.append(self.enqueue(s["user_id"], "auto"))
        return started

    # ------------------------------------------------------------ steps
    def _start_ts(self, uid: int) -> int | None:
        sd = trips.settings(self.db, uid).get("start_date") or ""
        if not sd:
            return None
        return int(datetime.fromisoformat(sd).replace(tzinfo=timezone.utc).timestamp())

    def _sync(self, uid: int, progress: Callable[[int, str], None]) -> dict[str, Any]:
        # Record the attempt up front so a failing account is retried at its
        # interval, not every scheduler tick.
        with self.db.tx() as c:
            c.execute("UPDATE settings SET last_run=? WHERE user_id=?", (now(), uid))
        s = trips.settings(self.db, uid)
        start = self._start_ts(uid)
        synced_to = int(s.get("synced_to") or 0)
        frm = max(synced_to - 48 * 3600, start or 0) if synced_to else start
        progress(2, "downloading points from Dawarich")
        gap = int(s.get("trip_gap_min") or 30)
        totals = {"new_points": 0, "created": 0, "updated": 0, "removed": 0, "dirtied": 0}
        latest = synced_to

        def page(batch: list[dict[str, Any]], p: int, n: int) -> None:
            # Archive and split each page as it arrives, so trips appear on the
            # map while a long first download is still running.
            nonlocal latest
            originals, _ = archive.split_remote(self.db, uid, batch)
            new = archive.add_api(self.db, uid, originals)
            latest = max([latest] + [int(x["timestamp"]) for x in batch])
            seg = trips.segment(self.db, uid, gap, [int(x["timestamp"]) for x in originals] if new else [])
            totals["new_points"] += new
            for k, v in seg.items():
                totals[k] += v
            progress(min(97, 2 + int(95 * p / max(1, n))), f"downloaded {p}/{n} pages, {totals['created']} trips so far")

        with push.client_for(self.db, self.cfg, uid, self.transport) as dw:
            dw.points(frm, None, on_page=page)
        with self.db.tx() as c:
            c.execute("UPDATE settings SET synced_to=? WHERE user_id=?", (max(latest, synced_to), uid))
        return totals

    def _match_pending(self, uid: int, progress: Callable[[int, str], None], lo: int, hi: int,
                       only_ids: list[int] | None = None) -> dict[str, int]:
        start = self._start_ts(uid) or 0
        if only_ids is None:
            rows = self.db.all("SELECT id FROM trips WHERE user_id=? AND locked=0 AND dirty=1 AND end_ts >= ?"
                               " ORDER BY start_ts", (uid, start))
            ids = [r["id"] for r in rows]
        else:
            ids = only_ids
        done = failed = 0
        if not ids:
            return {"matched": 0, "failed": 0}
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.match_workers)) as pool:
            futs = {pool.submit(trips.match, self.db, uid, tid, "auto"): tid for tid in ids}
            for k, fut in enumerate(as_completed(futs), 1):
                try:
                    fut.result()
                    done += 1
                except Exception:  # noqa: BLE001 - recorded on the trip
                    failed += 1
                try:
                    progress(lo + int((hi - lo) * k / len(ids)), f"matched {k}/{len(ids)} trips")
                except Cancelled:
                    for f in futs:
                        f.cancel()
                    raise
        return {"matched": done, "failed": failed}

    def _push_pending(self, uid: int, progress: Callable[[int, str], None], lo: int, hi: int,
                      only: set[int] | None = None) -> dict[str, int]:
        start = self._start_ts(uid) or 0
        ids = [t for t in push.pending_push(self.db, uid)
               if (trips.get(self.db, uid, t) or {}).get("start_ts", 0) >= start and (only is None or t in only)]
        if not ids:
            return {"pushed": 0}
        push.apply(self.db, self.cfg, uid, {t: True for t in ids}, self.transport, keep_going=True,
                   progress=lambda p, m: progress(lo + int((hi - lo) * p / 100), m))
        failed = self.db.one(f"SELECT COUNT(*) n FROM trips WHERE id IN ({','.join('?' * len(ids))})"
                             " AND (pushed_version IS NULL OR pushed_version != result_version)", tuple(ids))["n"]
        return {"pushed": len(ids) - failed, "push_failed": failed}

    def _finish(self, uid: int, progress: Callable[[int, str], None], summary: dict[str, Any]) -> None:
        with self.db.tx() as c:
            c.execute("UPDATE settings SET last_error='' WHERE user_id=?", (uid,))
        progress(100, ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in summary.items() if v))

    # ------------------------------------------------------------ job kinds
    def _job_sync(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        # A manual sync only stores the raw points and splits them into trips.
        # Matching is a separate step (per trip, "Match", or the automatic run).
        self._finish(uid, progress, self._sync(uid, progress))

    def _job_auto(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        summary = self._sync(uid, progress)
        summary.update(self._match_pending(uid, progress, 45, 80))
        if trips.settings(self.db, uid).get("auto_push"):
            summary.update(self._push_pending(uid, progress, 80, 99))
        self._finish(uid, progress, summary)

    def _job_import_csv(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        text = self._uploads.get(jid)
        if text is None:
            raise RuntimeError("upload is gone (restart?); upload the file again")
        progress(5, "reading CSV")
        new = archive.add_csv(self.db, uid, text)
        progress(30, f"archived {new} new points; splitting trips")
        s = trips.settings(self.db, uid)
        seg = trips.segment(self.db, uid, int(s.get("trip_gap_min") or 30), None)
        self._finish(uid, progress, {"new_points": new, **seg})

    def _job_rematch(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        scope = params.get("scope", "all")
        sql = "SELECT id FROM trips WHERE user_id=? AND locked=0"
        if scope == "ids":
            # Chosen trips: only the caller's own, never locked ones.
            want = {int(x) for x in params.get("ids") or []}
            rows = [r for r in self.db.all(sql + " ORDER BY start_ts", (uid,)) if r["id"] in want]
            self._finish(uid, progress, self._match_pending(uid, progress, 2, 99, [r["id"] for r in rows]))
            return
        if scope == "range":
            # Every trip overlapping a time range (unix seconds, either end open).
            a, b = params.get("start"), params.get("end")
            rows = self.db.all(sql + " AND end_ts >= ? AND start_ts <= ? ORDER BY start_ts",
                               (uid, int(a or 0), int(b or 2**62)))
            self._finish(uid, progress, self._match_pending(uid, progress, 2, 99, [r["id"] for r in rows]))
            return
        if scope == "pending":
            # Trips never matched, or whose points changed since.
            self._finish(uid, progress, self._match_pending(uid, progress, 2, 99))
            return
        if scope == "outdated":
            from ..engine.api import ENGINE_VERSION
            rows = self.db.all(sql + " AND engine_version != ? ORDER BY start_ts", (uid, ENGINE_VERSION))
        else:
            rows = self.db.all(sql + " ORDER BY start_ts", (uid,))
        summary = self._match_pending(uid, progress, 2, 99, [r["id"] for r in rows])
        self._finish(uid, progress, summary)

    def _job_push_all(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        only = {int(x) for x in params["ids"]} if params.get("ids") else None
        self._finish(uid, progress, self._push_pending(uid, progress, 2, 99, only))

    def _job_revert_all(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        rows = self.db.all("SELECT id FROM trips WHERE user_id=? AND pushed_version IS NOT NULL ORDER BY start_ts", (uid,))
        ids = [r["id"] for r in rows]
        out = push.apply(self.db, self.cfg, uid, {t: False for t in ids}, self.transport, keep_going=True,
                         progress=lambda p, m: progress(2 + int(97 * p / 100), m))
        left = self.db.one("SELECT COUNT(*) n FROM trips WHERE user_id=? AND pushed_version IS NOT NULL", (uid,))["n"]
        self._finish(uid, progress, {"reverted": len(ids) - left, "revert_failed": left,
                                     "points_restored": out["original_points"]})

    def _job_repair(self, uid: int, params: dict[str, Any], progress: Any, jid: str) -> None:
        out = push.repair(self.db, self.cfg, uid, self.transport, progress)
        self._finish(uid, progress, {"spans": out["spans"], "points_rewritten": out["posted"],
                                     "archived_new": out["archived_new"]})
