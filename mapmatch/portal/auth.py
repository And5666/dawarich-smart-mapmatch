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


def create_user(db: Database, username: str, password: str, is_admin: bool = False) -> int:
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("username must be 3-40 letters, digits, dot, dash or underscore")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("username already exists")
        cur = c.execute("INSERT INTO users(username, password, is_admin, created_at) VALUES (?,?,?,?)",
                        (username, hash_password(password), 1 if is_admin else 0, now()))
        uid = int(cur.lastrowid)
        c.execute("INSERT INTO settings(user_id) VALUES (?)", (uid,))
    return uid


def bootstrap_admin(db: Database, username: str, password: str) -> None:
    """Create the first admin from the environment. Ignored once any admin exists."""
    if not password or db.one("SELECT 1 FROM users WHERE is_admin=1"):
        return
    create_user(db, username, password, is_admin=True)


def authenticate(db: Database, username: str, password: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row or not check_password(password, row["password"]):
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
