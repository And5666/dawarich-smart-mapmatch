"""SQLite storage with versioned migrations.

The `points` table is the archive of original GPS fixes. Nothing in the
portal deletes from it; every other table holds derived data.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      username TEXT UNIQUE NOT NULL,
      password TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL
    );
    CREATE TABLE sessions (
      token_hash TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      created_at TEXT NOT NULL,
      expires_at TEXT NOT NULL
    );
    CREATE TABLE site (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE settings (
      user_id INTEGER PRIMARY KEY REFERENCES users(id),
      dawarich_url TEXT NOT NULL DEFAULT '',
      api_key TEXT NOT NULL DEFAULT '',
      auto_enabled INTEGER NOT NULL DEFAULT 0,
      interval_min INTEGER NOT NULL DEFAULT 60,
      auto_push INTEGER NOT NULL DEFAULT 0,
      start_date TEXT NOT NULL DEFAULT '',
      trip_gap_min INTEGER NOT NULL DEFAULT 30,
      strength TEXT NOT NULL DEFAULT 'fill',
      synced_to INTEGER NOT NULL DEFAULT 0,
      last_run TEXT NOT NULL DEFAULT '',
      last_error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE points (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      ts INTEGER NOT NULL,
      lat REAL NOT NULL,
      lon REAL NOT NULL,
      lat_e6 INTEGER NOT NULL,
      lon_e6 INTEGER NOT NULL,
      accuracy REAL,
      velocity REAL,
      altitude REAL,
      tracker_id TEXT NOT NULL DEFAULT '',
      dawarich_id INTEGER,
      source TEXT NOT NULL DEFAULT 'api',
      archived_at TEXT NOT NULL,
      UNIQUE(user_id, ts, lat_e6, lon_e6)
    );
    CREATE INDEX points_user_ts ON points(user_id, ts);
    CREATE TABLE trips (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL REFERENCES users(id),
      start_ts INTEGER NOT NULL,
      end_ts INTEGER NOT NULL,
      n_points INTEGER NOT NULL DEFAULT 0,
      closed INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'new',
      dirty INTEGER NOT NULL DEFAULT 1,
      locked INTEGER NOT NULL DEFAULT 0,
      overrides TEXT NOT NULL DEFAULT '[]',
      error TEXT NOT NULL DEFAULT '',
      engine_version TEXT NOT NULL DEFAULT '',
      result_version INTEGER NOT NULL DEFAULT 0,
      pushed_version INTEGER,
      pushed_at TEXT NOT NULL DEFAULT '',
      km REAL NOT NULL DEFAULT 0,
      modes TEXT NOT NULL DEFAULT '[]',
      quality TEXT NOT NULL DEFAULT '{}',
      preview TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX trips_user_start ON trips(user_id, start_ts);
    CREATE TABLE trip_results (
      id INTEGER PRIMARY KEY,
      trip_id INTEGER NOT NULL REFERENCES trips(id),
      version INTEGER NOT NULL,
      reason TEXT NOT NULL DEFAULT '',
      engine_version TEXT NOT NULL DEFAULT '',
      overrides TEXT NOT NULL DEFAULT '[]',
      quality TEXT NOT NULL DEFAULT '{}',
      result_gz BLOB NOT NULL,
      created_at TEXT NOT NULL,
      UNIQUE(trip_id, version)
    );
    CREATE TABLE pushes (
      id INTEGER PRIMARY KEY,
      user_id INTEGER NOT NULL,
      trip_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      version INTEGER,
      deleted INTEGER NOT NULL DEFAULT 0,
      posted INTEGER NOT NULL DEFAULT 0,
      dry_run INTEGER NOT NULL DEFAULT 0,
      detail TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
      id TEXT PRIMARY KEY,
      user_id INTEGER NOT NULL,
      kind TEXT NOT NULL,
      params TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'queued',
      progress INTEGER NOT NULL DEFAULT 0,
      message TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',
      cancel INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      started_at TEXT NOT NULL DEFAULT '',
      finished_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX jobs_user ON jobs(user_id, created_at);
    INSERT INTO site(key, value) VALUES ('signup', '0');
    """,
    # 2: sign-in with a Dawarich API key; per-trip point corrections
    """
    ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE trips ADD COLUMN point_edits TEXT NOT NULL DEFAULT '{}';
    """,
    # 3: password accounts; one Valhalla and one region catalogue for the site
    """
    ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE regions (
      id INTEGER PRIMARY KEY,
      name TEXT NOT NULL,
      pbf_url TEXT NOT NULL DEFAULT '',
      bounds TEXT NOT NULL DEFAULT '',
      notes TEXT NOT NULL DEFAULT '',
      built_at TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO site(key, value) VALUES ('valhalla_url', '');
    """,
    # 4: per-trip choice of what happens where the GPS drops out
    """
    ALTER TABLE trips ADD COLUMN gap_mode TEXT NOT NULL DEFAULT '';
    """,
    # 5: individual gaps that must stay as recorded
    """
    ALTER TABLE trips ADD COLUMN keep_gaps TEXT NOT NULL DEFAULT '[]';
    """,
    # 6: "route it anyway" — the explicit override of the plausibility gate
    """
    ALTER TABLE trips ADD COLUMN force_route INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE trips ADD COLUMN force_gaps TEXT NOT NULL DEFAULT '[]';
    """,
    # 7: every point the portal wrote to Dawarich, so none is ever taken for GPS
    """
    CREATE TABLE written (
      user_id INTEGER NOT NULL,
      ts INTEGER NOT NULL,
      lat_e6 INTEGER NOT NULL,
      lon_e6 INTEGER NOT NULL,
      PRIMARY KEY(user_id, ts, lat_e6, lon_e6)
    ) WITHOUT ROWID;
    """,
    # 8: what the trip's own choices were when each version was made
    """
    ALTER TABLE trip_results ADD COLUMN choices TEXT NOT NULL DEFAULT '{}';
    """,
    # 9: snap a detailed (watch, bike computer) track to the map instead of keeping it
    """
    ALTER TABLE trips ADD COLUMN snap_recorded INTEGER NOT NULL DEFAULT 0;
    """,
    # 10: what gets matched: the modes snapped to the map, detailed GPS kept or
    # snapped, and whether automatic pushes include trips that need a look.
    # A trip's snap_recorded is now 1 snap, -1 keep, 0 follow the account, and
    # route_gaps holds gaps to route in a trip that otherwise keeps its gaps.
    """
    ALTER TABLE settings ADD COLUMN match_modes TEXT NOT NULL DEFAULT 'walk,cycle,drive,train';
    ALTER TABLE settings ADD COLUMN detailed TEXT NOT NULL DEFAULT 'keep';
    ALTER TABLE settings ADD COLUMN push_review INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE trips ADD COLUMN route_gaps TEXT NOT NULL DEFAULT '[]';
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pack(value: Any) -> bytes:
    return gzip.compress(json.dumps(value, separators=(",", ":")).encode(), 6)


def unpack(blob: bytes) -> Any:
    return json.loads(gzip.decompress(blob))


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._write = threading.Lock()
        with self.connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction. Writes are serialized in-process."""
        with self._write, self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def migrate(self) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)")
            row = c.execute("SELECT v FROM schema_version").fetchone()
            v = row["v"] if row else 0
            if not row:
                c.execute("INSERT INTO schema_version(v) VALUES (0)")
            for i, sql in enumerate(MIGRATIONS[v:], start=v + 1):
                # executescript() would commit our transaction; run statements one by one.
                for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                    c.execute(stmt)
                c.execute("UPDATE schema_version SET v=?", (i,))

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self.connect() as c:
            return c.execute(sql, args).fetchone()

    def all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(sql, args).fetchall()
