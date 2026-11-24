"""Portal-wide settings: one value per key in the `site` table.

These apply to every account on the instance, unlike the per-user settings row:
the Valhalla address, whether anyone may register, and the region catalogue.
"""
from __future__ import annotations

from typing import Any

from .db import Database, now


def get(db: Database, key: str, default: str = "") -> str:
    row = db.one("SELECT value FROM site WHERE key=?", (key,))
    return row["value"] if row else default


def put(db: Database, key: str, value: Any) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO site(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, str(value)))


def valhalla_url(db: Database) -> str:
    return (get(db, "valhalla_url") or "").strip()


def signup_enabled(db: Database) -> bool:
    return get(db, "signup") == "1"


def set_signup(db: Database, enabled: bool) -> None:
    put(db, "signup", "1" if enabled else "0")


# ------------------------------------------------------------------ regions
def regions(db: Database) -> list[dict[str, Any]]:
    rows = db.all("SELECT * FROM regions ORDER BY name COLLATE NOCASE")
    return [dict(r) for r in rows]


def region(db: Database, rid: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM regions WHERE id=?", (rid,))
    return dict(row) if row else None


def add_region(db: Database, name: str, pbf_url: str = "", bounds: str = "", notes: str = "",
               built_at: str = "") -> int:
    stamp = now()
    with db.tx() as c:
        cur = c.execute("INSERT INTO regions(name, pbf_url, bounds, notes, built_at, created_at, updated_at)"
                        " VALUES (?,?,?,?,?,?,?)",
                        (name.strip()[:80], pbf_url.strip()[:500], bounds.strip()[:80], notes.strip()[:500],
                         built_at.strip(), stamp, stamp))
        return int(cur.lastrowid)


def update_region(db: Database, rid: int, **fields: Any) -> None:
    allowed = {"name", "pbf_url", "bounds", "notes", "built_at"}
    sets, args = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?")
            args.append(str(v).strip()[:500 if k in ("pbf_url", "notes") else 80])
    if not sets:
        return
    sets.append("updated_at=?")
    args.extend([now(), rid])
    with db.tx() as c:
        c.execute(f"UPDATE regions SET {', '.join(sets)} WHERE id=?", tuple(args))


def delete_region(db: Database, rid: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM regions WHERE id=?", (rid,))
