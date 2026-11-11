"""Small self-hosted web app for the map matcher.

One FastAPI router. SQLite for accounts/settings, JSON files for route data.
No build step. The page is served from the same container as /review.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

sys.path.insert(0, "/data")
os.environ.setdefault("RAIL_DATA_DIR", "/data")
os.environ.setdefault("VALHALLA_URL", "http://dawarich-valhalla:8002")
from smartmap.assemble import build_track  # noqa: E402
from smartmap.connectors import add_connectors, repair_internal_gaps, rescue_raw_parts  # noqa: E402
from smartmap.infer import infer_track  # noqa: E402
from smartmap.metrics import track_metrics  # noqa: E402
from smartmap.review import build as build_review  # noqa: E402
from smartmap.stream import from_parts  # noqa: E402

ROOT = Path(os.environ.get("PORTAL_DATA", "/data/portal"))
DB = ROOT / "portal.db"
USERS = ROOT / "users"
DAWARICH = os.environ.get("DAWARICH_URL", "http://dawarich_app:3001").rstrip("/")
VALHALLA = os.environ.get("VALHALLA_URL", "http://dawarich-valhalla:8002").rstrip("/")
HOST_HEADER = os.environ.get("DAWARICH_HOST_HEADER", "dawarich.andw.net")
COOKIE = "portal_session"
ROOT.mkdir(parents=True, exist_ok=True)
USERS.mkdir(parents=True, exist_ok=True)
os.chmod(ROOT, 0o700)

router = APIRouter(prefix="/review/portal")
_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _init_db() -> None:
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
          password TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
          api_key TEXT DEFAULT '', auto INTEGER NOT NULL DEFAULT 0,
          interval_minutes INTEGER NOT NULL DEFAULT 360,
          last_run TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS site (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO site(key,value) VALUES('signup','1')")
        row = c.execute("SELECT id FROM users WHERE username='andre'").fetchone()
        admin_pw = os.environ.get("PORTAL_ADMIN_PASSWORD", "")
        if not row and admin_pw:
            c.execute("INSERT INTO users(username,password,is_admin,created_at) VALUES(?,?,?,?)",
                      ("andre", _hash(admin_pw), 1, _now()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"{salt}${h.hex()}"


def _check(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
        return secrets.compare_digest(h.hex(), digest)
    except Exception:
        return False


def _user(req: Request) -> sqlite3.Row:
    token = req.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "login required")
    with _db() as c:
        row = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
                        (token,)).fetchone()
    if not row:
        raise HTTPException(401, "login expired")
    return row


def _admin(user: sqlite3.Row) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(403, "admin only")
    return user


def _signup_enabled() -> bool:
    with _db() as c:
        r = c.execute("SELECT value FROM site WHERE key='signup'").fetchone()
    return bool(r and r["value"] == "1")


def _udir(uid: int) -> Path:
    p = USERS / str(uid)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")))
    tmp.replace(path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _job_start(uid: int, kind: str, fn) -> str:
    jid = uuid.uuid4().hex[:12]
    _jobs[jid] = {"id": jid, "user": uid, "kind": kind, "status": "queued",
                  "progress": 0, "message": "queued", "created": _now()}

    def work():
        _jobs[jid].update(status="running", message="starting")
        try:
            fn(jid)
            _jobs[jid].update(status="done", progress=100, message="done")
        except Exception as exc:  # noqa: BLE001
            _jobs[jid].update(status="failed", message=f"{type(exc).__name__}: {exc}")
            raise

    threading.Thread(target=work, daemon=True).start()
    return jid


def _job(jid: str, pct: int, msg: str) -> None:
    if jid in _jobs:
        _jobs[jid].update(progress=pct, message=msg)


def _dawarich(method: str, path: str, key: str, **kwargs):
    headers = {"Host": HOST_HEADER}
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), headers=headers) as c:
        r = c.request(method, f"{DAWARICH}{path}", params={"api_key": key, **kwargs.pop("params", {})}, **kwargs)
    if r.status_code >= 300:
        raise RuntimeError(f"Dawarich {method} {path}: {r.status_code} {r.text[:200]}")
    return r


def _fetch_points(key: str, start: int | None = None, end: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    while True:
        params = {"page": page, "per_page": 1000, "order": "asc", "slim": "true"}
        if start:
            params["start_at"] = datetime.fromtimestamp(start, timezone.utc).isoformat()
        if end:
            params["end_at"] = datetime.fromtimestamp(end, timezone.utc).isoformat()
        r = _dawarich("GET", "/api/v1/points", key, params=params)
        batch = r.json()
        out.extend(batch)
        pages = int(r.headers.get("x-total-pages") or 1)
        if page >= pages or not batch:
            break
        page += 1
    return out


def _delete_all_points(key: str) -> int:
    r = _dawarich("GET", "/api/v1/points", key, params={"per_page": 1000, "page": 1, "order": "asc"})
    ids: list[int] = []
    page, pages = 1, int(r.headers.get("x-total-pages") or 1)
    while page <= pages:
        rr = _dawarich("GET", "/api/v1/points", key, params={"per_page": 1000, "page": page, "order": "asc"})
        ids += [int(p["id"]) for p in rr.json() if p.get("id")]
        page += 1
    for i in range(0, len(ids), 500):
        _dawarich("DELETE", "/api/v1/points/bulk_destroy", key,
                  json={"point_ids": ids[i:i + 500]})
    return len(ids)


def _push_points(key: str, points: list[dict[str, Any]], device: str) -> int:
    features = []
    for p in points:
        lat = p.get("lat", p.get("latitude"))
        lon = p.get("lon", p.get("longitude"))
        props = {"timestamp": int(p["timestamp"]), "device_id": device}
        for src, dst in (("accuracy", "horizontal_accuracy"), ("velocity", "speed"),
                         ("altitude", "altitude"), ("course", "course")):
            if p.get(src) is not None:
                props[dst] = p[src]
        features.append({"type": "Feature",
                         "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                         "properties": props})
    features.sort(key=lambda f: f["properties"]["timestamp"])
    for i in range(0, len(features), 500):
        _dawarich("POST", "/api/v1/points", key, json={"locations": features[i:i + 500]})
    return len(features)


def _matches(points: list[dict[str, Any]], jid: str, label: str) -> tuple[list[dict], list[dict]]:
    groups: dict[str, list[dict]] = {}
    for p in points:
        # Slim API responses leave track_id out; tracker_id is still there and
        # is unique per pushed route.
        key = p.get("track_id") or p.get("tracker_id")
        if key is None:
            continue
        groups.setdefault(str(key), []).append({
            "lat": float(p["latitude"]), "lon": float(p["longitude"]),
            "timestamp": int(p["timestamp"]), "accuracy": p.get("accuracy"),
            "velocity": p.get("velocity"), "altitude": p.get("altitude"),
        })
    items = sorted(groups.items())
    tracks = []

    def one(item):
        key, pts = item
        suffix = str(key).split("-")[-1]
        tid = int(suffix) if suffix.isdigit() else abs(hash(key)) % 1_000_000_000
        pts = sorted(pts, key=lambda x: x["timestamp"])
        tr = infer_track(pts)
        built = build_track(tid, tr)
        built["_raw_points"] = tr["fixes"]
        # Initial sync is deliberately base matching only. Connector routing is
        # expensive; opening a route and pressing "Map match all" adds it.
        built["points"] = from_parts(built)
        built["ngen"] = len(built["points"])
        built["gen"] = [[p["lat"], p["lon"]] for p in built["points"]]
        return built

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, item): item[0] for item in items}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                tracks.append(fut.result())
            except Exception:
                pass
            _job(jid, min(95, int(done * 90 / max(1, len(items)))),
                 f"{label} {done}/{len(items)}")
    review = build_review(tracks)
    for r in review:
        r["start"] = min((p["start"] for p in r["parts"]), default=0)
        r["end"] = max((p["end"] for p in r["parts"]), default=0)
    return tracks, review


def _run_import(uid: int, jid: str, start: int | None, end: int | None) -> None:
    with _db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not user or not user["api_key"]:
        raise RuntimeError("add a Dawarich API key first")
    _job(jid, 3, "downloading Dawarich points")
    points = _fetch_points(user["api_key"], start, end)
    d = _udir(uid)
    _write_json(d / "source.json", points)
    _job(jid, 20, f"matching {len(points)} points")
    tracks, review = _matches(points, jid, "matching")
    _write_json(d / "tracks.json", tracks)
    _write_json(d / "review.json", review)
    with _db() as c:
        c.execute("UPDATE users SET last_run=? WHERE id=?", (_now(), uid))


def _run_push(uid: int, jid: str) -> None:
    with _db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    d = _udir(uid)
    review = _read_json(d / "review.json", [])
    tracks = _read_json(d / "tracks.json", [])
    if not review or not user["api_key"]:
        raise RuntimeError("nothing to push")
    backup = d / "backups"
    backup.mkdir(exist_ok=True)
    old = _fetch_points(user["api_key"])
    _write_json(backup / f"{int(time.time())}.json", old)
    _job(jid, 15, "backup saved, clearing account points")
    _delete_all_points(user["api_key"])
    _job(jid, 35, "pushing matched points")
    for n, r in enumerate(review, 1):
        stream = from_parts({"parts": r.get("parts") or []})
        _push_points(user["api_key"], stream, f"portal-{uid}-{r['id']}")
        _job(jid, 35 + int(n * 60 / max(1, len(review))), f"pushed {n}/{len(review)} routes")
    _job(jid, 100, "pushed")


def _run_revert(uid: int, jid: str) -> None:
    with _db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    d = _udir(uid)
    backups = sorted((d / "backups").glob("*.json"), reverse=True) if (d / "backups").exists() else []
    if not backups:
        raise RuntimeError("no backup to restore")
    data = _read_json(backups[0], [])
    _job(jid, 10, f"restoring {len(data)} original points")
    _delete_all_points(user["api_key"])
    for i in range(0, len(data), 500):
        _push_points(user["api_key"], data[i:i + 500], "restored")
        _job(jid, 10 + int((i + 500) * 80 / max(1, len(data))), "restoring")
    _job(jid, 100, "restored")


def _auto_loop() -> None:
    while True:
        try:
            with _db() as c:
                users = c.execute("SELECT * FROM users WHERE auto=1").fetchall()
            now = datetime.now(timezone.utc)
            for u in users:
                try:
                    last = datetime.fromisoformat(u["last_run"]) if u["last_run"] else None
                    if last and (now - last).total_seconds() < int(u["interval_minutes"]) * 60:
                        continue
                    jid = _job_start(u["id"], "auto", lambda j, uid=u["id"]: (_run_import(uid, j, None, None), _run_push(uid, j)))
                    _ = jid
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(60)


_init_db()
threading.Thread(target=_auto_loop, daemon=True).start()


# ---------- auth ----------
class LoginIn(BaseModel):
    username: str
    password: str


class SignupIn(LoginIn):
    pass


@router.post("/api/login")
def login(body: LoginIn, response: Response):
    with _db() as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (body.username.strip(),)).fetchone()
    if not u or not _check(body.password, u["password"]):
        raise HTTPException(401, "wrong username or password")
    token = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, u["id"], _now()))
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=30 * 86400, path="/review/portal")
    return {"ok": True}


@router.post("/api/signup")
def signup(body: SignupIn, response: Response):
    if not _signup_enabled():
        raise HTTPException(403, "signup is disabled")
    name = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", name):
        raise HTTPException(400, "username must be 3-40 chars")
    if len(body.password) < 8:
        raise HTTPException(400, "password too short")
    with _db() as c:
        try:
            cur = c.execute("INSERT INTO users(username,password,created_at) VALUES(?,?,?)",
                            (name, _hash(body.password), _now()))
        except sqlite3.IntegrityError:
            raise HTTPException(400, "username already exists")
        uid = cur.lastrowid
    token = secrets.token_urlsafe(32)
    with _db() as c:
        c.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, uid, _now()))
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=30 * 86400, path="/review/portal")
    return {"ok": True}


@router.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with _db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
    response.delete_cookie(COOKIE, path="/review/portal")
    return {"ok": True}


@router.get("/api/me")
def me(user=Depends(_user)):
    return {"username": user["username"], "is_admin": bool(user["is_admin"]),
            "key_set": bool(user["api_key"]), "auto": bool(user["auto"]),
            "interval": user["interval_minutes"], "last_run": user["last_run"],
            "signup_enabled": _signup_enabled()}


class SettingsIn(BaseModel):
    api_key: str | None = None
    auto: bool | None = None
    interval: int | None = None


@router.put("/api/settings")
def settings(body: SettingsIn, user=Depends(_user)):
    with _db() as c:
        if body.api_key is not None:
            c.execute("UPDATE users SET api_key=? WHERE id=?", (body.api_key.strip(), user["id"]))
        if body.auto is not None:
            c.execute("UPDATE users SET auto=? WHERE id=?", (1 if body.auto else 0, user["id"]))
        if body.interval is not None:
            c.execute("UPDATE users SET interval_minutes=? WHERE id=?",
                      (max(15, min(7 * 24 * 60, body.interval)), user["id"]))
    return {"ok": True}


# ---------- data ----------
@router.get("/api/map")
def map_data(start: int | None = None, end: int | None = None, user=Depends(_user)):
    review = _read_json(_udir(user["id"]) / "review.json", [])
    routes = []
    for r in review:
        a, b = int(r.get("start") or 0), int(r.get("end") or 0)
        if start and b < start:
            continue
        if end and a > end:
            continue
        gen = r.get("gen") or []
        if len(gen) > 220:
            step = (len(gen) - 1) / 219
            gen = [gen[round(i * step)] for i in range(220)]
        routes.append({k: r.get(k) for k in ("id", "km", "distance", "start", "end", "nraw", "ngen")} |
                      {"parts": len(r.get("parts") or []), "gen": gen})
    routes.sort(key=lambda x: x["start"])
    return {"routes": routes}


@router.get("/api/route/{rid}")
def route(rid: int, user=Depends(_user)):
    d = _udir(user["id"])
    review = _read_json(d / "review.json", [])
    r = next((x for x in review if int(x["id"]) == rid), None)
    if not r:
        raise HTTPException(404, "route not found")
    tracks = _read_json(d / "tracks.json", [])
    t = next((x for x in tracks if int(x["id"]) == rid), {})
    r = dict(r)
    r["raw"] = t.get("raw", r.get("raw", []))
    r["gen"] = t.get("gen", r.get("gen", []))
    r["original_points"] = t.get("_raw_points", [])
    return r


@router.post("/api/import")
def import_data(user=Depends(_user)):
    if not user["api_key"]:
        raise HTTPException(400, "set your Dawarich API key first")
    jid = _job_start(user["id"], "import", lambda j: _run_import(user["id"], j, None, None))
    return {"job": jid}


@router.post("/api/push")
def push(user=Depends(_user)):
    if not user["api_key"]:
        raise HTTPException(400, "set your Dawarich API key first")
    jid = _job_start(user["id"], "push", lambda j: _run_push(user["id"], j))
    return {"job": jid}


@router.post("/api/revert")
def revert(user=Depends(_user)):
    jid = _job_start(user["id"], "revert", lambda j: _run_revert(user["id"], j))
    return {"job": jid}


@router.get("/api/jobs/{jid}")
def job(jid: str, user=Depends(_user)):
    j = _jobs.get(jid)
    if not j or j["user"] != user["id"]:
        raise HTTPException(404, "job not found")
    return j


@router.get("/api/download/source")
def download_source(user=Depends(_user)):
    data = _read_json(_udir(user["id"]) / "source.json", [])
    return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="dawarich-source.json"'})


class SegmentIn(BaseModel):
    mode: str | None = None
    matched: bool | None = None


@router.patch("/api/route/{rid}/segment/{idx}")
def edit_segment(rid: int, idx: int, body: SegmentIn, user=Depends(_user)):
    d = _udir(user["id"])
    review = _read_json(d / "review.json", [])
    r = next((x for x in review if int(x["id"]) == rid), None)
    if not r or idx < 0 or idx >= len(r["parts"]):
        raise HTTPException(404, "segment not found")
    p = r["parts"][idx]
    if body.mode:
        p["mode"] = body.mode
        p["engine"] = "manual" if body.mode != "raw" else "raw"
    if body.matched is False:
        p["engine"] = "raw"
        p["mode"] = "raw"
        t = next((x for x in _read_json(d / "tracks.json", []) if int(x["id"]) == rid), None)
        if t:
            raw = t.get("_raw_points") or []
            p["points"] = [[x["lat"], x["lon"]] for x in raw
                           if int(p["start"]) <= int(x["timestamp"]) <= int(p["end"])]
    _write_json(d / "review.json", review)
    return {"ok": True, "part": p}


@router.post("/api/route/{rid}/remove")
def remove_route(rid: int, user=Depends(_user)):
    d = _udir(user["id"])
    review = [x for x in _read_json(d / "review.json", []) if int(x["id"]) != rid]
    tracks = [x for x in _read_json(d / "tracks.json", []) if int(x["id"]) != rid]
    _write_json(d / "review.json", review)
    _write_json(d / "tracks.json", tracks)
    return {"ok": True}


@router.post("/api/route/{rid}/rematch")
def rematch_route(rid: int, user=Depends(_user)):
    """Rerun the model for one route using its saved raw points."""
    d = _udir(user["id"])
    tracks = _read_json(d / "tracks.json", [])
    t = next((x for x in tracks if int(x["id"]) == rid), None)
    if not t:
        raise HTTPException(404, "route not found")
    raw = t.get("_raw_points") or []
    if len(raw) < 2:
        raise HTTPException(400, "not enough raw points")
    tr = infer_track(raw)
    built = build_track(rid, tr)
    built["_raw_points"] = tr["fixes"]
    repair_internal_gaps(built)
    rescue_raw_parts(built)
    add_connectors(built)
    built["points"] = from_parts(built)
    built["ngen"] = len(built["points"])
    built["gen"] = [[p["lat"], p["lon"]] for p in built["points"]]
    tracks = [built if int(x["id"]) == rid else x for x in tracks]
    review = _read_json(d / "review.json", [])
    old = next((x for x in review if int(x["id"]) == rid), {"id": rid})
    new = build_review([built])[0]
    new["start"] = min((p["start"] for p in new["parts"]), default=0)
    new["end"] = max((p["end"] for p in new["parts"]), default=0)
    review = [new if int(x["id"]) == rid else x for x in review]
    _write_json(d / "tracks.json", tracks)
    _write_json(d / "review.json", review)
    return {"ok": True, "route": new}


class PointsIn(BaseModel):
    points: list[dict[str, Any]]


@router.put("/api/route/{rid}/points")
def edit_points(rid: int, body: PointsIn, user=Depends(_user)):
    d = _udir(user["id"])
    tracks = _read_json(d / "tracks.json", [])
    t = next((x for x in tracks if int(x["id"]) == rid), None)
    if not t:
        raise HTTPException(404, "route not found")
    pts = sorted(body.points, key=lambda p: p["timestamp"])
    t["_raw_points"] = pts
    _write_json(d / "tracks.json", tracks)
    return rematch_route(rid, user)


# ---------- admin ----------
@router.get("/api/admin")
def admin_info(user=Depends(_user)):
    _admin(user)
    try:
        with httpx.Client(timeout=10) as c:
            v = c.get(f"{VALHALLA}/status").json()
    except Exception as exc:  # noqa: BLE001
        v = {"error": str(exc)}
    return {"signup_enabled": _signup_enabled(), "valhalla": v}


@router.post("/api/admin/signup")
def admin_signup(body: dict, user=Depends(_user)):
    _admin(user)
    val = "1" if body.get("enabled") else "0"
    with _db() as c:
        c.execute("UPDATE site SET value=? WHERE key='signup'", (val,))
    return {"ok": True}


@router.post("/api/admin/valhalla")
def admin_valhalla(body: dict, user=Depends(_user)):
    _admin(user)
    # Real tile builds need the Valhalla build tooling and a mounted data dir.
    # This endpoint only runs an admin-configured command, never a shell string
    # supplied by the browser.
    cmd = os.environ.get("VALHALLA_DOWNLOAD_CMD", "").strip()
    if not cmd:
        return {"ok": False, "message": "VALHALLA_DOWNLOAD_CMD is not configured on this host"}
    region = str(body.get("region") or "").strip()
    if not region or not re.fullmatch(r"[A-Za-z0-9_,. -]{2,80}", region):
        raise HTTPException(400, "invalid region")
    subprocess.Popen(cmd.split() + [region], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "message": f"started download for {region}"}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page():
    return HTMLResponse(PAGE)


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mapmatch</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;color:#1c2430;background:#f5f7fa}
a{color:#2563eb}.hidden{display:none!important}.muted{color:#667085}.row{display:flex;gap:10px;align-items:center}.grow{flex:1}
button,.btn{border:1px solid #d0d5dd;background:#fff;color:#1c2430;border-radius:8px;padding:8px 12px;font:inherit;cursor:pointer}
button.primary{background:#2563eb;color:#fff;border-color:#2563eb}button.danger{background:#b42318;color:#fff;border-color:#b42318}
button:hover{filter:brightness(.97)}input,select,textarea{border:1px solid #d0d5dd;border-radius:8px;padding:8px 10px;font:inherit;background:#fff;color:#1c2430;width:100%}
label{font-size:12px;color:#475467;display:block;margin-bottom:4px}
.login{min-height:100vh;display:grid;place-items:center;background:linear-gradient(135deg,#eef4ff,#f8fafc)}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:16px;padding:24px;box-shadow:0 12px 40px #10182812;width:min(420px,92vw)}
.card h1{margin:0 0 4px;font-size:24px}.card p{margin:0 0 18px;color:#667085}.field{margin-bottom:12px}
.app{display:grid;grid-template-columns:310px 1fr;height:100vh}.side{background:#fff;border-right:1px solid #e4e7ec;display:flex;flex-direction:column;min-height:0}
.brand{padding:16px;border-bottom:1px solid #e4e7ec;display:flex;justify-content:space-between;align-items:center}.brand b{font-size:17px}
.tools{padding:12px 16px;border-bottom:1px solid #e4e7ec;display:grid;gap:8px}.tools .row{gap:6px}
.list{overflow:auto;flex:1}.item{padding:10px 16px;border-bottom:1px solid #f0f2f5;cursor:pointer}.item:hover,.item.active{background:#eff6ff}
.item b{font-size:13px}.item .meta{font-size:11px;color:#667085}
main{display:flex;flex-direction:column;min-width:0;min-height:0}.top{height:54px;background:#fff;border-bottom:1px solid #e4e7ec;display:flex;align-items:center;gap:12px;padding:0 16px}
#map{flex:1;min-height:0}.panel{position:absolute;z-index:500;right:16px;top:70px;width:min(420px,calc(100% - 32px));background:#fff;border:1px solid #e4e7ec;border-radius:12px;padding:16px;box-shadow:0 12px 40px #10182820}
.editor{position:fixed;inset:0;background:#f5f7fa;z-index:1000;display:flex;flex-direction:column}.edhead{height:56px;background:#fff;border-bottom:1px solid #e4e7ec;display:flex;align-items:center;gap:10px;padding:0 16px}
.edmaps{display:grid;grid-template-columns:1fr 1fr;height:38vh;min-height:220px}.edmap{position:relative}.edmap .cap{position:absolute;z-index:500;top:8px;left:8px;background:#fff9;padding:2px 8px;border-radius:4px;font-size:11px}
.edbody{flex:1;display:grid;grid-template-columns:1.2fr .8fr;gap:12px;padding:12px;overflow:auto}
.box{background:#fff;border:1px solid #e4e7ec;border-radius:10px;padding:12px}.seg{display:grid;grid-template-columns:44px 1fr 110px 70px 44px;gap:8px;align-items:center;padding:8px 0;border-bottom:1px solid #f0f2f5;font-size:12px}
.seg:last-child{border:0}.chip{font-size:11px;padding:2px 7px;border-radius:20px;background:#eef2f6;color:#475467}.chip.c{background:#e0f2fe;color:#075985}.chip.r{background:#fff7ed;color:#9a3412}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#101828;color:#fff;padding:10px 16px;border-radius:8px;z-index:2000;font-size:13px}
.progress{height:6px;background:#e4e7ec;border-radius:99px;overflow:hidden}.progress i{display:block;height:100%;background:#2563eb;width:0}
@media(max-width:850px){.app{grid-template-columns:1fr}.side{display:none}.edmaps{grid-template-columns:1fr;grid-template-rows:1fr 1fr}.edbody{grid-template-columns:1fr}}
</style></head><body>
<div id="login" class="login"><div class="card">
 <h1>Mapmatch</h1><p>Clean routes for your Dawarich account.</p>
 <div class="field"><label>Username</label><input id="lu" autocomplete="username"></div>
 <div class="field"><label>Password</label><input id="lp" type="password" autocomplete="current-password"></div>
 <div class="row"><button class="primary" onclick="login()">Sign in</button><button onclick="signup()" id="signupBtn">Create account</button></div>
 <p id="lerr" class="muted" style="margin-top:12px"></p>
</div></div>

<div id="app" class="app hidden">
 <aside class="side"><div class="brand"><b>Mapmatch</b><span id="who" class="muted"></span></div>
  <div class="tools">
   <div class="row"><input id="from" type="date"><input id="to" type="date"></div>
   <div class="row"><button class="primary grow" onclick="loadMap()">Show</button><button onclick="openSettings()">Settings</button></div>
   <div class="row"><button class="grow" onclick="startJob('import')">Sync &amp; match</button><button onclick="startJob('push')">Push</button></div>
  </div>
  <div id="list" class="list"></div>
 </aside>
 <main><div class="top"><b id="title">Routes</b><span class="grow"></span><span id="status" class="muted"></span>
  <button id="adminBtn" class="hidden" onclick="openAdmin()">Admin</button><button onclick="logout()">Sign out</button></div>
  <div id="map"></div>
 </main>
</div>

<div id="panel" class="panel hidden"></div>

<div id="editor" class="editor hidden">
 <div class="edhead"><button onclick="closeEditor()">Back</button><b id="edtitle">Route</b><span class="grow"></span>
  <button onclick="rematch()">Map match all</button><button onclick="removeRoute()" class="danger">Remove</button></div>
 <div class="edmaps"><div class="edmap"><div class="cap">Original</div><div id="mapO" style="position:absolute;inset:0"></div></div>
 <div class="edmap"><div class="cap">Matched</div><div id="mapM" style="position:absolute;inset:0"></div></div></div>
 <div class="edbody"><div class="box"><div class="row"><b>Segments</b><span class="grow"></span><span class="muted">change mode or leave raw</span></div><div id="segs"></div></div>
 <div class="box"><div class="row"><b>Original points</b><span class="grow"></span><button onclick="savePoints()">Save &amp; rerun</button></div>
  <p class="muted" style="font-size:12px">JSON list. Keep timestamps; latitude/longitude in WGS84.</p><textarea id="pts" style="height:300px;font-family:ui-monospace,monospace;font-size:11px"></textarea></div></div>
</div>
<div id="jobs" class="panel hidden"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const API='/review/portal/api';let ME=null,DATA=[],CUR=null,MAP=null,mapO=null,mapM=null,layers=[],sel=null;
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m){let t=document.createElement('div');t.className='toast';t.textContent=m;document.body.appendChild(t);setTimeout(()=>t.remove(),3000)}
async function api(path,opt={}){let r=await fetch(API+path,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...opt});if(r.status===401){showLogin();throw new Error('login')}let j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||r.statusText);return j}
function login(){api('/login',{method:'POST',body:JSON.stringify({username:$('lu').value,password:$('lp').value})}).then(init).catch(e=>$('lerr').textContent=e.message)}
function signup(){api('/signup',{method:'POST',body:JSON.stringify({username:$('lu').value,password:$('lp').value})}).then(init).catch(e=>$('lerr').textContent=e.message)}
function logout(){api('/logout',{method:'POST'}).then(showLogin)}
function showLogin(){$('login').classList.remove('hidden');$('app').classList.add('hidden');$('editor').classList.add('hidden')}
function init(){api('/me').then(m=>{ME=m;$('login').classList.add('hidden');$('app').classList.remove('hidden');$('who').textContent=m.username;if(m.is_admin)$('adminBtn').classList.remove('hidden');$('signupBtn').classList.toggle('hidden',!m.signup_enabled);let y=new Date();y.setFullYear(y.getFullYear()-1);$('from').value=y.toISOString().slice(0,10);$('to').value=new Date().toISOString().slice(0,10);if(!MAP)MAP=initMap('map');loadMap();if(!m.key_set)openSettings(true)}).catch(showLogin)}
function initMap(id){let m=L.map(id,{preferCanvas:true}).setView([51.24,-0.59],11);L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OSM'}).addTo(m);return m}
async function loadMap(){let a=new Date($('from').value).getTime()/1000,b=new Date($('to').value).getTime()/1000+86399;let d=await api('/map?start='+Math.floor(a)+'&end='+Math.floor(b));DATA=d.routes;renderList();draw();}
function renderList(){let el=$('list');el.innerHTML='';DATA.forEach((r,i)=>{let d=document.createElement('div');d.className='item';d.innerHTML=`<b>#${r.id} · ${r.distance} km</b><div class="meta">${new Date(r.start*1000).toLocaleDateString()} · ${r.parts} parts</div>`;d.onclick=()=>openEditor(r.id);el.appendChild(d)})}
function draw(){layers.forEach(l=>MAP.removeLayer(l));layers=[];DATA.forEach(r=>{if(!r.gen||r.gen.length<2)return;let l=L.polyline(r.gen,{color:'#2563eb',weight:3,opacity:.65}).addTo(MAP);l.on('click',()=>openEditor(r.id));layers.push(l)});}
function openSettings(force){let key=force?'':'Ask for your Dawarich API key.';$('panel').classList.remove('hidden');$('panel').innerHTML=`<div class="row"><b>Settings</b><span class="grow"></span><button onclick="$('panel').classList.add('hidden')">Close</button></div>
 <div class="field"><label>Dawarich API key</label><input id="key" placeholder="paste key"> </div>
 <div class="row"><label class="grow"><input id="auto" type="checkbox" style="width:auto"> Automatic matching</label><input id="interval" type="number" min="15" style="width:110px" placeholder="minutes"></div>
 <p class="muted" style="font-size:12px">The key is stored only on this server. Automatic runs check for new points, back up, match and push.</p>
 <div class="row" style="margin-top:10px"><button class="primary" onclick="saveSettings()">Save</button><a class="btn" href="/review/portal/api/download/source">Download source</a><button onclick="startJob('revert')">Revert push</button></div>`;api('/me').then(m=>{$('auto').checked=m.auto;$('interval').value=m.interval})}
async function saveSettings(){let b={auto:$('auto').checked,interval:+$('interval').value};if($('key').value.trim())b.api_key=$('key').value.trim();await api('/settings',{method:'PUT',body:JSON.stringify(b)});toast('Settings saved');$('panel').classList.add('hidden')}
async function startJob(kind){if(kind==='push'&&!confirm('Replace the Dawarich account points with the matched set? A backup is saved first.'))return;if(kind==='revert'&&!confirm('Restore the newest backup to Dawarich?'))return;let d=await api('/'+kind,{method:'POST'});poll(d.job)}
async function poll(id){$('jobs').classList.remove('hidden');$('jobs').innerHTML='<b>Working…</b><div class="progress" style="margin-top:10px"><i id="bar"></i></div><p id="jmsg" class="muted"></p>';let t=setInterval(async()=>{let j=await api('/jobs/'+id);$('bar').style.width=j.progress+'%';$('jmsg').textContent=j.message;if(j.status==='done'||j.status==='failed'){clearInterval(t);setTimeout(()=>$('jobs').classList.add('hidden'),1500);if(j.status==='done')loadMap()}},900)}
async function openEditor(id){let r=await api('/route/'+id);CUR=r;$('editor').classList.remove('hidden');$('edtitle').textContent='Route #'+id;$('pts').value=JSON.stringify(r.original_points||[],null,0);if(!mapO){mapO=initMap('mapO');mapM=initMap('mapM')}drawEditor();renderSegs()}
function drawEditor(){mapO.eachLayer(l=>{if(l instanceof L.TileLayer)return;mapO.removeLayer(l)});mapM.eachLayer(l=>{if(l instanceof L.TileLayer)return;mapM.removeLayer(l)});
L.polyline(CUR.raw,{color:'#98a2b3',weight:3}).addTo(mapO);L.polyline(CUR.gen,{color:'#2563eb',weight:3}).addTo(mapM);let b=L.latLngBounds(CUR.raw.length?CUR.raw:CUR.gen);mapO.fitBounds(b,{padding:[20,20]});mapM.fitBounds(b,{padding:[20,20]})}
function renderSegs(){let el=$('segs');el.innerHTML='';CUR.parts.forEach((p,i)=>{let d=document.createElement('div');d.className='seg';d.innerHTML=`<span class="chip ${p.engine==='raw'?'r':'c'}">${i+1}</span><span>${esc(p.mode)} · ${esc(p.engine)}<br><span class="muted">${(p.distance||0).toFixed? p.distance : p.distance} km</span></span><select data-i="${i}">${['driving','cycling','walking','train','raw'].map(m=>`<option ${m===p.mode?'selected':''}>${m}</option>`).join('')}</select><label><input type="checkbox" data-m="${i}" ${p.engine!=='raw'?'checked':''}> matched</label><button data-x="${i}">Raw</button>`;el.appendChild(d)});el.querySelectorAll('select').forEach(s=>s.onchange=()=>segEdit(+s.dataset.i,{mode:s.value}));el.querySelectorAll('input[data-m]').forEach(c=>c.onchange=()=>segEdit(+c.dataset.m,{matched:c.checked}));el.querySelectorAll('button[data-x]').forEach(b=>b.onclick=()=>segEdit(+b.dataset.x,{matched:false}))}
async function segEdit(i,b){await api('/route/'+CUR.id+'/segment/'+i,{method:'PATCH',body:JSON.stringify(b)});CUR=await api('/route/'+CUR.id);renderSegs()}
async function rematch(){let r=await api('/route/'+CUR.id+'/rematch',{method:'POST'});CUR=r.route;drawEditor();renderSegs();toast('Rematched')}
async function removeRoute(){if(!confirm('Remove this route from the matched set?'))return;await api('/route/'+CUR.id+'/remove',{method:'POST'});closeEditor();loadMap()}
function closeEditor(){$('editor').classList.add('hidden');CUR=null}
async function savePoints(){try{let pts=JSON.parse($('pts').value).map(p=>({lat:+p.lat,lon:+p.lon,timestamp:+p.timestamp}));let r=await api('/route/'+CUR.id+'/points',{method:'PUT',body:JSON.stringify({points:pts})});CUR=r.route;drawEditor();renderSegs();toast('Reran route')}catch(e){toast(e.message)}}
async function openAdmin(){let a=await api('/admin');$('panel').classList.remove('hidden');$('panel').innerHTML=`<div class="row"><b>Admin</b><span class="grow"></span><button onclick="$('panel').classList.add('hidden')">Close</button></div><div class="box" style="margin-top:10px"><label>Signups</label><button onclick="toggleSignup(true)">Enabled</button> <button onclick="toggleSignup(false)">Disabled</button><p class="muted" style="font-size:12px">Valhalla tileset: ${esc(JSON.stringify(a.valhalla))}</p><div class="field"><label>Download more Valhalla data</label><div class="row"><input id="region" placeholder="region name"><button onclick="dlValhalla()">Download</button></div><p class="muted" style="font-size:12px">Requires a configured VALHALLA_DOWNLOAD_CMD on the host.</p></div></div>`}
async function toggleSignup(v){await api('/admin/signup',{method:'POST',body:JSON.stringify({enabled:v})});toast('Signups '+(v?'enabled':'disabled'))}
async function dlValhalla(){let d=await api('/admin/valhalla',{method:'POST',body:JSON.stringify({region:$('region').value})});toast(d.message)}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeEditor();$('panel').classList.add('hidden')}});
init();
</script></body></html>"""
