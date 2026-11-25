"""HTTP API for the portal page."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StrictBool

from ..engine import valhalla
from ..engine.api import ENGINE_VERSION, MODES
from . import archive, auth, push, railways, site, trips
from .db import now as now_iso
from .dawarich import Client, DawarichError

COOKIE = "mapmatch_session"
MAX_UPLOAD = 64 * 1024 * 1024
MAX_POINTS = 250_000


class SettingsIn(BaseModel):
    auto_enabled: bool | None = None
    interval_min: int | None = Field(None, ge=15, le=7 * 24 * 60)
    auto_push: bool | None = None
    start_date: str | None = None
    trip_gap_min: int | None = Field(None, ge=5, le=24 * 60)
    strength: str | None = None
    match_modes: list[str] | None = None
    detailed: str | None = None
    push_review: bool | None = None


class Overrides(BaseModel):
    overrides: list[dict[str, Any]]


class ModeIn(BaseModel):
    mode: str


class SplitIn(BaseModel):
    ts: int


class GapsIn(BaseModel):
    # True: route across. False: keep the recorded GPS. None: follow the
    # account's own setting again (trip-wide only).
    route: StrictBool | None = None
    # With t0 and t1, the choice is for that one gap instead of the whole trip.
    t0: int | None = None
    t1: int | None = None
    # Route it even though the plausibility gate refuses it (the timestamps make
    # it look faster than the cap). Explicit "match it anyway" from the editor.
    force: StrictBool | None = None


class RecordedIn(BaseModel):
    # True: snap the detailed stretches to the map. False: keep them as recorded.
    # None: do what the account's Match settings say.
    snap: StrictBool | None = None


class RestoreIn(BaseModel):
    version: int


class LockIn(BaseModel):
    locked: bool


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = {}


class ConnectIn(BaseModel):
    api_key: str


class LoginIn(BaseModel):
    username: str
    password: str


class RegisterIn(BaseModel):
    username: str
    password: str
    display_name: str = ""


class PasswordIn(BaseModel):
    password: str


class AdminUserIn(BaseModel):
    username: str
    password: str
    display_name: str = ""
    is_admin: bool = False


class AdminUserPatch(BaseModel):
    password: str | None = None
    is_admin: bool | None = None
    disabled: bool | None = None
    display_name: str | None = None


class SiteIn(BaseModel):
    signup: bool | None = None
    valhalla_url: str | None = None


class ValhallaTestIn(BaseModel):
    url: str = ""


class RegionIn(BaseModel):
    name: str
    pbf_url: str = ""
    bounds: str = ""
    notes: str = ""
    built_at: str = ""


class RegionPatch(BaseModel):
    name: str | None = None
    pbf_url: str | None = None
    bounds: str | None = None
    notes: str | None = None
    built_at: str | None = None


class RangeIn(BaseModel):
    t0: int
    t1: int
    mode: str


class PointEditsIn(BaseModel):
    edits: dict[str, Any]



def build(app_state: Any) -> APIRouter:
    db, cfg, jobs = app_state.db, app_state.cfg, app_state.jobs
    r = APIRouter(prefix=cfg.prefix + "/api")

    # ------------------------------------------------------------ deps
    def user(request: Request) -> dict[str, Any]:
        u = auth.session_user(db, request.cookies.get(COOKIE))
        if not u or u.get("disabled"):
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
    def login(body: LoginIn, response: Response) -> dict[str, Any]:
        who = auth.authenticate(db, body.username, body.password)
        if not who:
            time.sleep(0.4)   # no free guesses
            raise HTTPException(401, "Wrong username or password")
        set_cookie(response, auth.new_session(db, int(who["id"]), cfg.session_days))
        return {"ok": True, "is_admin": bool(who["is_admin"])}

    @r.post("/register")
    def register(body: RegisterIn, response: Response) -> dict[str, Any]:
        """Create an account: freely only while nobody exists or sign-up is on."""
        first = db.one("SELECT COUNT(*) n FROM users")["n"] == 0
        if not first and not site.signup_enabled(db):
            raise HTTPException(403, "Sign-up is off. Ask an admin to make you an account.")
        try:
            uid = auth.create_user(db, body.username.strip(), body.password,
                                   is_admin=first, display_name=body.display_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        set_cookie(response, auth.new_session(db, uid, cfg.session_days))
        return {"ok": True, "is_admin": first}

    @r.post("/connect")
    def connect(body: ConnectIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Link the signed-in account to a Dawarich user by pasting its API key."""
        key = body.api_key.strip()
        if not key or len(key) > 200:
            raise HTTPException(400, "Paste the API key from your Dawarich account page")
        s = trips.settings(db, u["id"])
        base = (s.get("dawarich_url") or cfg.dawarich_url).rstrip("/")
        try:
            with Client(base, key, cfg.dawarich_host_header, transport=jobs.transport) as dw:
                who = dw.me()
                dw.count()
        except DawarichError as exc:
            time.sleep(0.4)
            msg = str(exc)
            if "rejected" in msg:
                raise HTTPException(401, "Dawarich didn't accept that key. Check it on your Dawarich account page.") from exc
            raise HTTPException(502, f"Couldn't reach Dawarich: {msg}") from exc
        auth.link_key(db, u["id"], base, key)
        if not u.get("display_name") and who.get("email"):
            # A new link names the account after the Dawarich user.
            auth.update_user(db, u["id"], display_name=str(who["email"]))
        first = archive.stats(db, u["id"])["points"] == 0
        if first:
            jobs.enqueue(u["id"], "sync")
        return {"ok": True, "first_sync": first,
                "dawarich": {"id": who.get("id"), "email": who.get("email") or ""}}

    @r.post("/connect/base")
    def connect_base(body: dict[str, str], u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Remember which Dawarich this account points at, before a key is added."""
        url = (body.get("dawarich_url") or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")) or len(url) > 200:
            raise HTTPException(400, "Dawarich URL must start with http:// or https://")
        with db.tx() as c:
            c.execute("UPDATE settings SET dawarich_url=? WHERE user_id=?", (url, u["id"]))
        return {"ok": True}

    @r.post("/unlink")
    def unlink(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        auth.unlink_key(db, u["id"])
        return {"ok": True}

    @r.post("/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        auth.end_session(db, request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path=cfg.prefix or "/")
        return {"ok": True}

    @r.get("/public")
    def public_info() -> dict[str, Any]:
        # Seen before signing in: only what the sign-in page needs, nothing
        # about where Dawarich or the map server live, or which version runs.
        return {"signup": site.signup_enabled(db),
                "first_run": db.one("SELECT COUNT(*) n FROM users")["n"] == 0}

    @r.get("/me")
    def me(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        s = trips.settings(db, u["id"])
        return {"name": u.get("display_name") or u["username"], "username": u["username"],
                "is_admin": bool(u["is_admin"]), "dawarich_linked": bool(s.get("api_key")),
                "settings": _public_settings(s, cfg), "archive": archive.stats(db, u["id"]),
                "dry_run": cfg.push_dry_run, "dawarich_needs_repair": push.needs_repair(db, u["id"]), "engine": ENGINE_VERSION, "modes": list(MODES)}

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
        if "detailed" in data and data["detailed"] not in trips.DETAILED:
            raise HTTPException(400, "detailed must be keep or snap")
        if "match_modes" in data:
            if set(data["match_modes"]) - set(trips.MOVING):
                raise HTTPException(400, "modes to match must be walk, cycle, drive or train")
            data["match_modes"] = ",".join(trips.match_modes(data["match_modes"]))
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
        edits = json.loads(t.get("point_edits") or "{}")
        ids = db.all("SELECT id FROM trips WHERE user_id=? ORDER BY start_ts", (uid,))
        order = [x["id"] for x in ids]
        k = order.index(tid)
        return {
            "trip": trips.public(t),
            "segments": res["segments"] if res else [],
            "dropped": res.get("dropped", []) if res else [],
            "raw": [{**{k2: p[k2] for k2 in ("id", "lat", "lon", "timestamp", "accuracy", "velocity")},
                     "edit": edits.get(str(p["id"]))} for p in raw],
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

    @r.post("/trips/{tid}/range")
    def set_range(tid: int, body: RangeIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Pin any stretch of a trip (not just a whole segment) to a mode."""
        t = trip_or_404(u["id"], tid)
        if body.mode not in MODES:
            raise HTTPException(400, "unknown mode")
        if body.t1 - body.t0 < 1 and body.t0 - body.t1 < 1:
            raise HTTPException(400, "select a longer stretch")
        ov = trips.range_override(t, max(body.t0, t["start_ts"]), min(body.t1, t["end_ts"]), body.mode)
        return _rematch(u["id"], tid, f"part set to {body.mode}", ov)

    @r.put("/trips/{tid}/points")
    def put_points(tid: int, body: PointEditsIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Move or leave out GPS fixes for this trip, then rematch. The archived
        originals are untouched; send {} to undo every correction."""
        trip_or_404(u["id"], tid)
        try:
            trips.set_point_edits(db, u["id"], tid, body.edits)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        n = len(body.edits)
        return _rematch(u["id"], tid, f"{n} point{'s' if n != 1 else ''} corrected" if n else "corrections cleared", None)

    @r.get("/trips/{tid}/versions/{v}")
    def trip_version(tid: int, v: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """An earlier version, to look at before going back to it."""
        trip_or_404(u["id"], tid)
        old = trips.version(db, tid, v)
        if not old:
            raise HTTPException(404, "that version is no longer kept")
        res = old.pop("result")
        return {**old, "segments": res["segments"], "quality": res.get("quality", {})}

    @r.post("/trips/{tid}/restore")
    def restore_version(tid: int, body: RestoreIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        trip_or_404(u["id"], tid)
        try:
            trips.restore(db, u["id"], tid, body.version)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return _detail(u["id"], tid)

    @r.post("/trips/{tid}/clear")
    def clear_trip(tid: int, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Back to the original GPS in mapmatch. Dawarich is not touched."""
        trip_or_404(u["id"], tid)
        trips.clear(db, u["id"], tid)
        return _detail(u["id"], tid)

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

    @r.post("/trips/{tid}/gaps")
    def put_gaps(tid: int, body: GapsIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Route or keep the gaps: one hole by its times, or the whole trip."""
        trip = trip_or_404(u["id"], tid)
        if body.t0 is not None and body.t1 is not None:
            if body.route is None:
                raise HTTPException(400, "say whether that gap should be routed or kept")
            span = [min(body.t0, body.t1), max(body.t0, body.t1)]
            hits = lambda span_list: [k for k in span_list if not (span[0] <= k[1] and k[0] <= span[1])]
            keep = hits(json.loads(trip.get("keep_gaps") or "[]"))
            force = hits(json.loads(trip.get("force_gaps") or "[]"))
            route = hits(json.loads(trip.get("route_gaps") or "[]"))
            if body.route is False:
                keep.append(span)
                reason = "gap kept as recorded"
            elif body.force:
                force.append(span)
                reason = "gap routed anyway, over the speed cap"
            else:
                # Routed even if the trip (or the account) keeps its gaps.
                route.append(span)
                reason = "gap routed"
            trips.set_keep_gaps(db, u["id"], tid, keep)
            trips.set_force_gaps(db, u["id"], tid, force)
            trips.set_route_gaps(db, u["id"], tid, route)
            return _rematch(u["id"], tid, reason, None)
        mode = "" if body.route is None else ("fill" if body.route else "strict")
        try:
            trips.set_gap_mode(db, u["id"], tid, mode)
            # A choice for every gap replaces the choices made gap by gap.
            trips.set_keep_gaps(db, u["id"], tid, [])
            trips.set_force_gaps(db, u["id"], tid, [])
            trips.set_route_gaps(db, u["id"], tid, [])
            # "Route across" also says whether the speed and length checks may be overridden.
            trips.set_force_route(db, u["id"], tid, bool(body.force) and body.route is True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        reason = {"fill": "gaps routed" + (" anyway, over the speed cap" if body.force else ""),
                  "strict": "gaps kept as recorded",
                  "": "gaps follow settings"}[mode]
        return _rematch(u["id"], tid, reason, None)

    @r.put("/trips/{tid}/recorded")
    def put_recorded(tid: int, body: RecordedIn, u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        """Keep a detailed track as recorded, or snap it to the map anyway."""
        trip_or_404(u["id"], tid)
        trips.set_snap_recorded(db, u["id"], tid, body.snap)
        return _rematch(u["id"], tid, {True: "snapped to the map", False: "kept as recorded",
                                       None: "detailed GPS follows settings"}[body.snap], None)

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
        if body.kind not in ("sync", "rematch", "push_all", "revert_all", "repair", "reset"):
            raise HTTPException(400, "unknown job")
        needs_key = ("sync", "push_all", "revert_all", "repair") + (("reset",) if body.params.get("dawarich") else ())
        if body.kind in needs_key and not trips.settings(db, u["id"]).get("api_key"):
            raise HTTPException(400, "add your Dawarich API key in Settings first")
        if body.kind == "rematch":
            once = body.params.get("once")
            if once is not None:
                if not isinstance(once, dict):
                    raise HTTPException(400, "once must be an object")
                if once.get("match_modes") is not None and (not isinstance(once["match_modes"], list)
                                                            or set(once["match_modes"]) - set(trips.MOVING)):
                    raise HTTPException(400, "modes to match must be walk, cycle, drive or train")
                if once.get("detailed") not in (None, *trips.DETAILED):
                    raise HTTPException(400, "detailed must be keep or snap")
                if once.get("strength") not in (None, "fill", "strict"):
                    raise HTTPException(400, "strength must be fill or strict")
            if body.params.get("push") and not trips.settings(db, u["id"]).get("api_key"):
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
    # Instance-wide settings: the Valhalla every account matches against, the
    # region catalogue that goes with it, whether people may register, and the
    # accounts themselves.

    def _site_state() -> dict[str, Any]:
        url = site.valhalla_url(db)
        return {
            "signup": site.signup_enabled(db),
            "valhalla_url": url,
            "valhalla": valhalla.status(timeout=4.0) if url else {},
            "configured": bool(url),
            "users": auth.users(db),
            "regions": site.regions(db),
            "data_dir": bool(cfg.valhalla_data_dir),
            "data_dir_path": cfg.valhalla_data_dir,
        }

    @r.get("/admin/site")
    def admin_site(u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        return _site_state()

    @r.put("/admin/site")
    def put_admin_site(body: SiteIn, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        if body.signup is not None:
            site.set_signup(db, body.signup)
        if body.valhalla_url is not None:
            url = body.valhalla_url.strip().rstrip("/")
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(400, "Valhalla address must start with http:// or https://")
            if len(url) > 200:
                raise HTTPException(400, "That address is too long")
            site.put(db, "valhalla_url", url)
            valhalla.configure(url)   # applies to every account, at once
        return _site_state()

    @r.post("/admin/valhalla/test")
    def test_valhalla(body: ValhallaTestIn, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        url = (body.url or "").strip().rstrip("/") or site.valhalla_url(db)
        if not url:
            raise HTTPException(400, "Give a Valhalla address first")
        out = valhalla.status(timeout=6.0, url=url)
        out["url"] = url
        return out

    @r.get("/admin/valhalla/data")
    def valhalla_data(u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        """What is in the mounted Valhalla data directory, when an admin mounted one."""
        root = cfg.valhalla_data_dir
        if not root or not Path(root).is_dir():
            return {"mounted": False, "files": []}
        files = []
        for p in sorted(Path(root).iterdir(), key=lambda x: x.name.lower()):
            try:
                st = p.stat()
                files.append({"name": p.name, "dir": p.is_dir(),
                              "size": None if p.is_dir() else st.st_size,
                              "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds")})
            except OSError:
                continue
        return {"mounted": True, "root": str(root), "files": files}

    @r.get("/admin/railways")
    def admin_railways(u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        """Railway networks trains are matched on, and extracts to build more from."""
        return railways.state(cfg)

    @r.post("/admin/railways/build")
    def admin_build_railways(body: dict[str, str], u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        try:
            railways.build(cfg, str(body.get("source") or ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return railways.state(cfg)

    @r.delete("/admin/railways/{file}")
    def admin_remove_railways(file: str, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        try:
            railways.remove(cfg, file)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return railways.state(cfg)

    @r.get("/admin/valhalla/data/{name}")
    def valhalla_data_file(name: str, u: dict[str, Any] = Depends(admin)) -> FileResponse:
        root = cfg.valhalla_data_dir
        if not root:
            raise HTTPException(400, "No Valhalla data directory is mounted in this container")
        base = Path(root).resolve()
        target = (base / name).resolve()
        if target.parent != base or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target, filename=target.name)

    @r.post("/admin/users")
    def admin_create_user(body: AdminUserIn, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        try:
            uid = auth.create_user(db, body.username.strip(), body.password, body.is_admin, body.display_name)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"id": uid, "users": auth.users(db)}

    @r.patch("/admin/users/{uid}")
    def admin_patch_user(uid: int, body: AdminUserPatch, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        target = db.one("SELECT * FROM users WHERE id=?", (uid,))
        if not target:
            raise HTTPException(404, "no such account")
        loses_admin = (body.is_admin is False or body.disabled is True) and target["is_admin"]
        if uid == u["id"] and (body.is_admin is False or body.disabled is True):
            raise HTTPException(400, "You can't remove your own admin access or disable yourself")
        if loses_admin and auth.admin_count(db) <= 1:
            raise HTTPException(400, "At least one admin account has to stay")
        if body.password is not None:
            try:
                auth.set_password(db, uid, body.password)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        auth.update_user(db, uid, is_admin=body.is_admin, disabled=body.disabled, display_name=body.display_name)
        return {"users": auth.users(db)}

    # ------------------------------------------------------------ regions

    @r.get("/admin/regions")
    def list_regions(u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        return {"regions": site.regions(db)}

    @r.post("/admin/regions")
    def add_region(body: RegionIn, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        if not body.name.strip():
            raise HTTPException(400, "Give the region a name")
        rid = site.add_region(db, body.name, body.pbf_url, body.bounds, body.notes, body.built_at)
        return {"id": rid, "regions": site.regions(db)}

    @r.patch("/admin/regions/{rid}")
    def patch_region(rid: int, body: RegionPatch, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        if not site.region(db, rid):
            raise HTTPException(404, "no such region")
        site.update_region(db, rid, **body.model_dump(exclude_none=True))
        return {"regions": site.regions(db)}

    @r.delete("/admin/regions/{rid}")
    def delete_region(rid: int, u: dict[str, Any] = Depends(admin)) -> dict[str, Any]:
        site.delete_region(db, rid)
        return {"regions": site.regions(db)}

    # ------------------------------------------------------------ system
    @r.get("/system")
    def system(u: dict[str, Any] = Depends(user)) -> dict[str, Any]:
        outdated = db.one("SELECT COUNT(*) n FROM trips WHERE user_id=? AND result_version > 0 AND engine_version != ?",
                          (u["id"], ENGINE_VERSION))["n"]
        # The admin's address, or the VALHALLA_URL the container was started with.
        url = site.valhalla_url(db) or os.environ.get("VALHALLA_URL", "")
        # The address itself is internal: only admins see it.
        return {"valhalla": valhalla.status(timeout=4.0) if url else {},
                "valhalla_url": (url if u["is_admin"] else "set") if url else "", "regions": site.regions(db),
                "engine": ENGINE_VERSION, "outdated_trips": outdated,
                "dry_run": cfg.push_dry_run, "dawarich": cfg.dawarich_url,
                "is_admin": bool(u["is_admin"]), "signup": site.signup_enabled(db)}

    return r


def _public_settings(s: dict[str, Any], cfg: Any) -> dict[str, Any]:
    key = s.get("api_key") or ""
    return {
        "dawarich_url": s.get("dawarich_url") or "", "default_dawarich_url": cfg.dawarich_url,
        "api_key_set": bool(key), "api_key_hint": ("…" + key[-4:]) if len(key) > 8 else ("set" if key else ""),
        "auto_enabled": bool(s.get("auto_enabled")), "interval_min": s.get("interval_min", 60),
        "auto_push": bool(s.get("auto_push")), "start_date": s.get("start_date") or "",
        "trip_gap_min": s.get("trip_gap_min", 30), "strength": s.get("strength") or "fill",
        "match_modes": trips.match_modes(s.get("match_modes", ",".join(trips.MOVING))),
        "detailed": s.get("detailed") or "keep", "push_review": bool(s.get("push_review", 1)),
        "last_run": s.get("last_run") or "", "last_error": s.get("last_error") or "",
        "synced_to": s.get("synced_to") or 0,
    }
