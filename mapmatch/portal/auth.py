"""Accounts and sessions. Password hashing kept from the old portal (pbkdf2)."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import Database, now

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,40}")
ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
    return f"{salt}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS)
        return secrets.compare_digest(h.hex(), digest)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(db: Database, username: str, password: str, is_admin: bool = False,
                display_name: str = "") -> int:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("username must be 3-40 letters, digits, dot, dash or underscore")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("username already exists")
        cur = c.execute("INSERT INTO users(username, password, is_admin, display_name, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (username, hash_password(password), 1 if is_admin else 0,
                         (display_name or "").strip()[:80], now()))
        uid = int(cur.lastrowid)
        c.execute("INSERT INTO settings(user_id) VALUES (?)", (uid,))
    return uid


def link_key(db: Database, uid: int, base_url: str, api_key: str) -> None:
    """Attach a Dawarich API key to an existing account.

    The key is only ever a link: an admin (or the account itself) signs in with a
    password, then pastes the key once. The key stays per account, so several
    Dawarich users can share one portal.
    """
    with db.tx() as c:
        row = c.execute("SELECT 1 FROM settings WHERE user_id=?", (uid,)).fetchone()
        if row:
            c.execute("UPDATE settings SET api_key=?, dawarich_url=? WHERE user_id=?", (api_key, base_url, uid))
        else:
            c.execute("INSERT INTO settings(user_id, api_key, dawarich_url) VALUES (?,?,?)",
                      (uid, api_key, base_url))


def unlink_key(db: Database, uid: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE settings SET api_key='' WHERE user_id=?", (uid,))


def set_password(db: Database, uid: int, password: str) -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with db.tx() as c:
        c.execute("UPDATE users SET password=? WHERE id=?", (hash_password(password), uid))


def update_user(db: Database, uid: int, *, is_admin: bool | None = None, disabled: bool | None = None,
                display_name: str | None = None) -> None:
    sets, args = [], []
    if is_admin is not None:
        sets.append("is_admin=?")
        args.append(1 if is_admin else 0)
    if disabled is not None:
        sets.append("disabled=?")
        args.append(1 if disabled else 0)
    if display_name is not None:
        sets.append("display_name=?")
        args.append(display_name.strip()[:80])
    if not sets:
        return
    args.append(uid)
    with db.tx() as c:
        c.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", tuple(args))


def users(db: Database) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT u.id, u.username, u.display_name, u.is_admin, u.disabled, u.created_at,"
        "       s.api_key, s.auto_enabled, s.last_run,"
        "       (SELECT COUNT(*) FROM points p WHERE p.user_id=u.id) AS points,"
        "       (SELECT COUNT(*) FROM trips t WHERE t.user_id=u.id) AS trips"
        "  FROM users u LEFT JOIN settings s ON s.user_id=u.id ORDER BY u.id")
    out = []
    for r in rows:
        d = dict(r)
        key = d.pop("api_key") or ""
        d["dawarich_linked"] = bool(key)
        d["api_key_hint"] = ("…" + key[-4:]) if len(key) > 8 else ("set" if key else "")
        d["points"] = d["points"] or 0
        d["trips"] = d["trips"] or 0
        out.append(d)
    return out


def admin_count(db: Database) -> int:
    return db.one("SELECT COUNT(*) n FROM users WHERE is_admin=1 AND disabled=0")["n"]


def bootstrap_admin(db: Database, username: str, password: str) -> None:
    """Create the admin named by the environment, unless an admin already exists.

    This is also the recovery path: a legacy account created by the old
    key-only sign-in has no password, so it cannot be signed into; the
    environment admin can set one from the admin page.
    """
    if not password:
        return
    # An account made by the old key-only sign-in has no password; give the
    # environment admin's credentials to it so an upgraded install can be
    # signed into again, and rename it if the environment name is free.
    broken = db.one("SELECT id, username FROM users WHERE is_admin=1 AND (password='' OR password='!')"
                    " ORDER BY id LIMIT 1")
    if broken:
        uid = int(broken["id"])
        if broken["username"] != username and USERNAME_RE.fullmatch(username) and not db.one(
                "SELECT 1 FROM users WHERE username=?", (username,)):
            with db.tx() as c:
                c.execute("UPDATE users SET username=? WHERE id=?", (username, uid))
        set_password(db, uid, password)
        return
    if db.one("SELECT 1 FROM users WHERE is_admin=1"):
        return
    row = db.one("SELECT id, password FROM users WHERE username=?", (username,))
    if row:
        set_password(db, int(row["id"]), password)
        update_user(db, int(row["id"]), is_admin=True, disabled=False)
        return
    create_user(db, username, password, is_admin=True)


def authenticate(db: Database, username: str, password: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row or row["disabled"] or not check_password(password, row["password"]):
        return None
    return dict(row)


def new_session(db: Database, user_id: int, days: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")
    with db.tx() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
        c.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                  (_token_hash(token), user_id, now(), expires))
    return token


def session_user(db: Database, token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    row = db.one("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                 "WHERE s.token_hash=? AND s.expires_at > ?", (_token_hash(token), now()))
    return dict(row) if row else None


def end_session(db: Database, token: str | None) -> None:
    if token:
        with db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))


def signup_enabled(db: Database) -> bool:
    row = db.one("SELECT value FROM site WHERE key='signup'")
    return bool(row and row["value"] == "1")
