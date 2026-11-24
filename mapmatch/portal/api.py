"""HTTP API for the portal page."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..engine import valhalla
from ..engine.api import ENGINE_VERSION, MODES
from . import archive, auth, push, trips
from .db import now as now_iso
from .dawarich import DawarichError

COOKIE = "mapmatch_session"
MAX_UPLOAD = 64 * 1024 * 1024
MAX_POINTS = 250_000


class Credentials(BaseModel):
    username: str
    password: str


class SettingsIn(BaseModel):
    dawarich_url: str | None = None
    api_key: str | None = None
    auto_enabled: bool | None = None
    interval_min: int | None = Field(None, ge=15, le=7 * 24 * 60)
    auto_push: bool | None = None
    start_date: str | None = None
    trip_gap_min: int | None = Field(None, ge=5, le=24 * 60)
    strength: str | None = None


class Overrides(BaseModel):
    overrides: list[dict[str, Any]]


class ModeIn(BaseModel):
    mode: str


class SplitIn(BaseModel):
    ts: int


class LockIn(BaseModel):
    locked: bool


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = {}


class SignupIn(BaseModel):
    enabled: bool



def build(app_state: Any) -> APIRouter:
    db, cfg, jobs = app_state.db, app_state.cfg, app_state.jobs
    r = APIRouter(prefix=cfg.prefix + "/api")

    # ------------------------------------------------------------ deps
    def user(request: Request) -> dict[str, Any]:
        u = auth.session_user(db, request.cookies.get(COOKIE))
        if not u:
            raise HTTPException(401, "login required")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-portal") != "1":
            # Every write comes from our page with this header; a cross-site
            # form post cannot add it.
            raise HTTPException(403, "missing X-Portal header")
        return u

    def admin(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        if not u["is_admin"]:
            raise HTTPException(403, "admin only")
        return u

    def trip_or_404(uid: int, tid: int) -> dict[str, Any]:
        t = trips.get(db, uid, tid)
        if not t:
            raise HTTPException(404, "trip not found")
        return t

    def set_cookie(resp: Response, token: str) -> None:
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=cfg.secure_cookie,
                        max_age=cfg.session_days * 86400, path=cfg.prefix or "/")

    # ------------------------------------------------------------ auth

    @r.post("/login")
    def login(body: Credentials, response: Response) -> dict[str, Any]:
        u = auth.authenticate(db, body.username, body.password)
        if not u:
            time.sleep(0.4)
            raise HTTPException(401, "wrong username or password")
        set_cookie(response, auth.new_session(db, u["id"], cfg.session_days))
        return {"ok": True}

    @r.post("/signup")
    def signup(body: Credentials, response: Response) -> dict[str, Any]:
        if not auth.signup_enabled(db):
            raise HTTPException(403, "sign-up is turned off")
        try:
            uid = auth.create_user(db, body.username.strip(), body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        set_cookie(response, auth.new_session(db, uid, cfg.session_days))
        return {"ok": True}

    @r.post("/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        auth.end_session(db, request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path=cfg.prefix or "/")
        return {"ok": True}

    @r.get("/public")
    def public_info() -> dict[str, Any]:
        return {"signup": auth.signup_enabled(db), "engine": ENGINE_VERSION}

    @r.get("/me")
    def me(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        s = trips.settings(db, u["id"])
        return {"username": u["username"], "is_admin": bool(u["is_admin"]),
                "settings": _public_settings(s, cfg), "archive": archive.stats(db, u["id"]),
                "dry_run": cfg.push_dry_run, "engine": ENGINE_VERSION, "modes": list(MODES)}

    # ------------------------------------------------------------ settings

    @r.put("/settings")
    def put_settings(body: SettingsIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        sets: dict[str, Any] = {}
        data = body.model_dump(exclude_none=True)
        if "start_date" in data and data["start_date"]:
            try:
                datetime.fromisoformat(data["start_date"])
            except ValueError as exc:
                raise HTTPException(400, "start date must be YYYY-MM-DD") from exc
        if "strength" in data and data["strength"] not in ("fill", "strict"):
            raise HTTPException(400, "strength must be fill or strict")
        if "dawarich_url" in data and data["dawarich_url"] and \
                not data["dawarich_url"].startswith(("http://", "https://")):
            raise HTTPException(400, "Dawarich URL must start with http:// or https://")
        for k, v in data.items():
            sets[k] = (1 if v else 0) if isinstance(v, bool) else (v.strip() if isinstance(v, str) else v)
        if sets:
            with db.tx() as c:
                c.execute(f"UPDATE settings SET {', '.join(f'{k}=?' for k in sets)} WHERE user_id=?",
                          (*sets.values(), u["id"]))
        return _public_settings(trips.settings(db, u["id"]), cfg)

    @r.post("/settings/test")
    def test_connection(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        try:
            with push.client_for(db, cfg, u["id"], jobs.transport) as dw:
                n = dw.count()
        except DawarichError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": f"Connected. The account reports {n} points."}

    # ------------------------------------------------------------ trips
    @r.get("/trips")
    def list_trips(start: int | None = None, end: int | None = None, state: str | None = None,
                   mode: str | None = None, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        sql = "SELECT * FROM trips WHERE user_id=?"
        args: list[Any] = [u["id"]]
        if start:
            sql += " AND end_ts >= ?"
            args.append(start)
        if end:
            sql += " AND start_ts <= ?"
            args.append(end)
        out = []
        totals: dict[str, float] = {}
        counts: dict[str, int] = {}
        for row in db.all(sql + " ORDER BY start_ts DESC", tuple(args)):
            t = dict(row)
            p = trips.public(t)
            p["preview"] = json.loads(t["preview"] or "[]")
            if state and p["state"] != state and p["push"] != state:
                continue
            if mode and not any(m["mode"] == mode for m in p["modes"]):
                continue
            for m in p["modes"]:
                totals[m["mode"]] = round(totals.get(m["mode"], 0.0) + m["km"], 2)
            counts[p["state"]] = counts.get(p["state"], 0) + 1
            counts["push_" + p["push"]] = counts.get("push_" + p["push"], 0) + 1
            out.append(p)
        return {"trips": out, "km_by_mode": totals, "counts": counts}

    @r.get("/trips/changes")
    def trip_changes(since: str = "", u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Trips changed at or after `since` (ISO time from the previous call),
        for drawing matches live while a job runs. Clients dedupe by version."""
        stamp = now_iso()
        rows = db.all("SELECT * FROM trips WHERE user_id=? AND updated_at >= ? ORDER BY updated_at",
                      (u["id"], since or stamp))
        out = []
        for row in rows:
            t = dict(row)
            p = trips.public(t)
            p["preview"] = json.loads(t["preview"] or "[]")
            out.append(p)
        return {"now": stamp, "trips": out}

    @r.get("/points")
    def raw_points(start: int | None = None, end: int | None = None,
                   u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Original GPS points in a range, as [lat, lon], thinned past MAX_POINTS."""
        pts = archive.window(db, u["id"], start, end)
        total = len(pts)
        if total > MAX_POINTS:
            step = total / MAX_POINTS
            pts = [pts[int(k * step)] for k in range(MAX_POINTS)]
        return {"total": total, "points": [[round(p["lat"], 6), round(p["lon"], 6)] for p in pts]}

    def _detail(uid: int, tid: int) -> dict[str, Any]:
        t = trip_or_404(uid, tid)
        res = trips.result(db, t)
        raw = archive.window(db, uid, t["start_ts"], t["end_ts"])
        ids = db.all("SELECT id FROM trips WHERE user_id=? ORDER BY start_ts", (uid,))
        order = [x["id"] for x in ids]
        k = order.index(tid)
        return {
            "trip": trips.public(t),
            "segments": res["segments"] if res else [],
            "dropped": res.get("dropped", []) if res else [],
            "raw": [{k2: p[k2] for k2 in ("lat", "lon", "timestamp", "accuracy", "velocity", "tracker_id")} for p in raw],
            "history": [{**h, "overrides": json.loads(h["overrides"]), "quality": json.loads(h["quality"])}
                        for h in trips.history(db, tid)],
            "prev": order[k - 1] if k > 0 else None,
            "next": order[k + 1] if k + 1 < len(order) else None,
        }

    @r.get("/trips/{tid}")
    def trip_detail(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        return _detail(u["id"], tid)

    def _rematch(uid: int, tid: int, reason: str, overrides: list[dict[str, Any]] | None) -> dict[str, Any]:
        try:
            trips.match(db, uid, tid, reason, overrides)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except valhalla.ValhallaError as exc:
            raise HTTPException(502, f"Valhalla: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"matching failed: {exc}") from exc
        return _detail(uid, tid)


    @r.put("/trips/{tid}/overrides")
    def put_overrides(tid: int, body: Overrides, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        return _rematch(u["id"], tid, "edit", body.overrides)


    @r.post("/trips/{tid}/segments/{sid}/mode")
    def set_mode(tid: int, sid: int, body: ModeIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        t = trip_or_404(u["id"], tid)
        if body.mode not in MODES:
            raise HTTPException(400, "unknown mode")
        res = trips.result(db, t)
        if not res:
            raise HTTPException(400, "trip has no match yet")
        try:
            ov = trips.segment_override(t, res, sid, body.mode)
        except KeyError as exc:
            raise HTTPException(404, "segment not found") from exc
        return _rematch(u["id"], tid, f"segment {sid} → {body.mode}", ov)


    @r.post("/trips/{tid}/split")
    def split(tid: int, body: SplitIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        t = trip_or_404(u["id"], tid)
        res = trips.result(db, t)
        if not res:
            raise HTTPException(400, "trip has no match yet")
        try:
            ov = trips.split_override(t, res, body.ts)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _rematch(u["id"], tid, "split", ov)

    @r.post("/trips/{tid}/recalculate")
    def recalculate(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        t = trip_or_404(u["id"], tid)
        return _rematch(u["id"], tid, "map match" if not t["result_version"] else "recalculate", [])

    @r.post("/trips/{tid}/undo")
    def undo(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        try:
            trips.undo(db, u["id"], tid)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _detail(u["id"], tid)


    @r.post("/trips/{tid}/lock")
    def lock(tid: int, body: LockIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        with db.tx() as c:
            c.execute("UPDATE trips SET locked=? WHERE id=?", (1 if body.locked else 0, tid))
        return _detail(u["id"], tid)

    @r.post("/trips/{tid}/push")
    def push_one(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        try:
            out = push.push_trip(db, cfg, u["id"], tid, jobs.transport)
        except (ValueError, DawarichError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": out, **_detail(u["id"], tid)}

    @r.post("/trips/{tid}/revert")
    def revert_one(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        try:
            out = push.revert_trip(db, cfg, u["id"], tid, jobs.transport)
        except (ValueError, DawarichError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": out, **_detail(u["id"], tid)}

    @r.get("/trips/{tid}/export.{fmt}")
    def export(tid: int, fmt: str, u: dict[str, Any] = Depends(user)) -> Response:
        t = trip_or_404(u["id"], tid)
        res = trips.result(db, t)
        if not res:
            raise HTTPException(400, "trip has no match yet")
        name = f"trip-{datetime.fromtimestamp(t['start_ts'], timezone.utc):%Y%m%d-%H%M}"
        if fmt == "geojson":
            feats = [{"type": "Feature", "properties": {"mode": s["mode"], "km": s["km"], "t0": s["t0"], "t1": s["t1"]},
                      "geometry": {"type": "LineString",
                                   "coordinates": [[c[1], c[0]] for p in s["pieces"] for c in p["coords"]]}}
                     for s in res["segments"]]
            return JSONResponse({"type": "FeatureCollection", "features": feats},
                                headers={"Content-Disposition": f'attachment; filename="{name}.geojson"'})
        if fmt == "gpx":
            pts = "".join(
                f'<trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}"><time>'
                f'{datetime.fromtimestamp(p["timestamp"], timezone.utc):%Y-%m-%dT%H:%M:%SZ}</time></trkpt>'
                for p in res["stream"])
            body = ('<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="mapmatch" '
                    f'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>{escape(name)}</name><trkseg>{pts}'
                    '</trkseg></trk></gpx>')
            return Response(body, media_type="application/gpx+xml",
                            headers={"Content-Disposition": f'attachment; filename="{name}.gpx"'})
        raise HTTPException(404, "unknown format")

    # ------------------------------------------------------------ jobs

    @r.post("/jobs")
    def start_job(body: JobIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        if body.kind not in ("sync", "rematch", "push_all", "revert_all"):
            raise HTTPException(400, "unknown job")
        if body.kind in ("sync", "push_all", "revert_all") and not trips.settings(db, u["id"]).get("api_key"):
            raise HTTPException(400, "add your Dawarich API key in Settings first")
        return {"job": jobs.enqueue(u["id"], body.kind, body.params)}

    @r.post("/import/csv")
    async def import_csv(file: UploadFile = File(...), u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(400, "file must be UTF-8 CSV") from exc
        head = text.splitlines()[0].lower() if text else ""
        if not all(k in head for k in ("timestamp", "lat", "lon")):
            raise HTTPException(400, "CSV needs timestamp, lat and lon columns")
        return {"job": jobs.enqueue(u["id"], "import_csv", {"name": file.filename}, upload=text)}

    @r.get("/jobs")
    def list_jobs(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        rows = db.all("SELECT id, kind, params, status, progress, message, error, created_at, started_at, finished_at"
                      " FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (u["id"],))
        return {"jobs": [{**dict(x), "params": json.loads(x["params"])} for x in rows]}

    @r.get("/jobs/{jid}")
    def get_job(jid: str, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        row = db.one("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, u["id"]))
        if not row:
            raise HTTPException(404, "job not found")
        return {**dict(row), "params": json.loads(row["params"])}

    @r.post("/jobs/{jid}/cancel")
    def cancel_job(jid: str, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        return {"ok": jobs.cancel(u["id"], jid)}

    @r.get("/pushes")
    def list_pushes(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        rows = db.all("SELECT * FROM pushes WHERE user_id=? ORDER BY id DESC LIMIT 100", (u["id"],))
        return {"pushes": [{**dict(x), "detail": json.loads(x["detail"] or "{}")} for x in rows]}

    # ------------------------------------------------------------ admin
    @r.get("/admin")
    def admin_info(_: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        users = db.all("SELECT u.id, u.username, u.is_admin, u.created_at, s.auto_enabled, s.last_run, s.last_error,"
                       " (SELECT COUNT(*) FROM points p WHERE p.user_id=u.id) AS points,"
                       " (SELECT COUNT(*) FROM trips t WHERE t.user_id=u.id) AS trips"
                       " FROM users u LEFT JOIN settings s ON s.user_id=u.id ORDER BY u.id")
        outdated = db.one("SELECT COUNT(*) n FROM trips WHERE result_version > 0 AND engine_version != ?",
                          (ENGINE_VERSION,))["n"]
        return {"users": [dict(x) for x in users], "signup": auth.signup_enabled(db),
                "valhalla": valhalla.status(), "engine": ENGINE_VERSION, "outdated_trips": outdated,
                "dry_run": cfg.push_dry_run}


    @r.post("/admin/signup")
    def admin_signup(body: SignupIn, _: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        with db.tx() as c:
            c.execute("UPDATE site SET value=? WHERE key='signup'", ("1" if body.enabled else "0",))
        return {"signup": body.enabled}

    return r


def _public_settings(s: dict[str, Any], cfg: Any) -> dict[str, Any]:
    key = s.get("api_key") or ""
    return {
        "dawarich_url": s.get("dawarich_url") or "", "default_dawarich_url": cfg.dawarich_url,
        "api_key_set": bool(key), "api_key_hint": ("…" + key[-4:]) if len(key) > 8 else ("set" if key else ""),
        "auto_enabled": bool(s.get("auto_enabled")), "interval_min": s.get("interval_min", 60),
        "auto_push": bool(s.get("auto_push")), "start_date": s.get("start_date") or "",
        "trip_gap_min": s.get("trip_gap_min", 30), "strength": s.get("strength") or "fill",
        "last_run": s.get("last_run") or "", "last_error": s.get("last_error") or "",
        "synced_to": s.get("synced_to") or 0,
    }
