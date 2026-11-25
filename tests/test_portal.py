"""Portal tests against a fake Dawarich and a fake Valhalla (no network)."""
from __future__ import annotations

import pytest
from conftest import track
from fake_dawarich import FakeDawarich

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from mapmatch.portal.app import create_app  # noqa: E402
from mapmatch.portal.config import Config  # noqa: E402

H = {"x-portal": "1"}
DRIVE = [(500, 0)] * 20
WALK = [(40, 0)] * 12
HOME = [(2, -2), (-2, 2)] * 40   # 40 min of jitter at home


def day(t0: int) -> list[dict]:
    """home -> walk + drive -> office -> drive home -> home."""
    home = track(HOME, t0=t0)
    out = track(WALK + DRIVE, start=(home[-1]["lat"], home[-1]["lon"]), t0=home[-1]["timestamp"] + 30)
    office = track(HOME, start=(out[-1]["lat"], out[-1]["lon"]), t0=out[-1]["timestamp"] + 30)
    back = track([(-500, 0)] * 20 + [(-40, 0)] * 12, start=(office[-1]["lat"], office[-1]["lon"]),
                 t0=office[-1]["timestamp"] + 30)
    home2 = track(HOME, start=(back[-1]["lat"], back[-1]["lon"]), t0=back[-1]["timestamp"] + 30)
    return home + out + office + back + home2


@pytest.fixture
def env(tmp_path, fake_valhalla):
    dw = FakeDawarich()
    dw.seed(day(1_780_000_000))
    cfg = Config(data_dir=tmp_path, base_path="/", dawarich_url="http://dawarich.test", scheduler=False,
                 admin_user="admin", admin_password="adminpass")
    app = create_app(cfg, transport=dw.transport, start_workers=False)
    client = TestClient(app)
    assert client.post("/api/login", json={"username": "admin", "password": "adminpass"}).status_code == 200
    assert client.post("/api/connect", json={"api_key": "secret"}, headers=H).status_code == 200
    jobs = app.state.portal.jobs
    return client, dw, jobs, app.state.portal.db


def run_job(client, jobs, kind, params=None):
    jid = client.post("/api/jobs", json={"kind": kind, "params": params or {}}, headers=H).json()["job"]
    jobs.run_pending()
    job = client.get(f"/api/jobs/{jid}").json()
    assert job["status"] == "done", job
    return job


def sync(client, jobs):
    """Sync (raw only), then map match everything pending."""
    run_job(client, jobs, "sync")
    return run_job(client, jobs, "rematch", {"scope": "pending"})


def archive_count(db) -> int:
    return db.one("SELECT COUNT(*) n FROM points")["n"]


def test_sync_only_stores_raw_points(env, fake_valhalla):
    client, dw, jobs, db = env
    run_job(client, jobs, "sync")
    assert archive_count(db) == len(dw.originals())
    assert not fake_valhalla.calls, "sync must not map match"
    trips = client.get("/api/trips").json()
    assert len(trips["trips"]) == 2 and trips["counts"] == {"new": 2, "push_original": 2}
    t = trips["trips"][0]
    assert t["modes"] == [] and t["km"] > 1
    assert [p["m"] for p in t["preview"]] == ["none"] and len(t["preview"][0]["c"]) >= 2   # one grey raw line
    d = client.get(f"/api/trips/{t['id']}").json()
    assert d["segments"] == [] and len(d["raw"]) == t["n_points"]
    assert client.post(f"/api/trips/{t['id']}/push", headers=H).status_code == 400   # nothing to push yet
    # Matching one trip on request.
    d = client.post(f"/api/trips/{t['id']}/recalculate", headers=H).json()
    assert d["segments"] and d["history"][0]["reason"] == "map match"


def test_sync_archives_and_finds_trips(env):
    client, dw, jobs, db = env
    before = dw.originals()
    sync(client, jobs)
    assert archive_count(db) == len(before)
    trips = client.get("/api/trips").json()["trips"]
    assert len(trips) == 2                       # the two drives; home/office are stays
    assert all(t["state"] == "matched" and t["closed"] for t in trips)
    assert all(t["push"] == "original" for t in trips)
    # Sync is idempotent.
    sync(client, jobs)
    assert archive_count(db) == len(before)
    assert len(client.get("/api/trips").json()["trips"]) == 2


def test_push_edit_push_revert_round_trip(env):
    client, dw, jobs, db = env
    originals = dw.originals()
    sync(client, jobs)
    n_archive = archive_count(db)
    listing = client.get("/api/trips").json()["trips"]
    trip = listing[-1]   # oldest first after reversing
    tid = trip["id"]
    # A push rewrites the trip and tidies the stops up to the next trip.
    nxt = min((t["start"] for t in listing if t["start"] > trip["end"]), default=trip["end"] + 6 * 3600)
    outside = {k for k in originals if not trip["start"] - 6 * 3600 <= k[0] < nxt}

    r = client.post(f"/api/trips/{tid}/push", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["trip"]["push"] == "pushed"
    assert dw.ours(), "matched points written"
    assert dw.originals() == outside, "only the trip window's originals are replaced"
    assert archive_count(db) == n_archive

    # Edit a segment's mode, then push again: the earlier matched points go.
    detail = client.get(f"/api/trips/{tid}").json()
    seg = next(s for s in detail["segments"] if s["mode"] == "drive")
    d2 = client.post(f"/api/trips/{tid}/segments/{seg['id']}/mode", json={"mode": "cycle"}, headers=H).json()
    assert d2["trip"]["push"] == "outdated"
    assert any(s["mode"] == "cycle" and s["forced"] for s in d2["segments"])
    first_push_ids = {p["id"] for p in dw.ours()}
    assert client.post(f"/api/trips/{tid}/push", headers=H).status_code == 200
    assert not first_push_ids & {p["id"] for p in dw.ours()}
    assert dw.originals() == outside

    # Revert restores exactly the original set.
    r = client.post(f"/api/trips/{tid}/revert", headers=H)
    assert r.status_code == 200, r.text
    assert not dw.ours()
    assert dw.originals() == originals
    assert archive_count(db) == n_archive
    # A sync after the revert does not double-archive the re-posted originals.
    sync(client, jobs)
    assert archive_count(db) == n_archive


def test_undo_and_recalculate(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    seg = client.get(f"/api/trips/{tid}").json()["segments"][0]
    d = client.post(f"/api/trips/{tid}/segments/{seg['id']}/mode", json={"mode": "raw"}, headers=H).json()
    assert d["segments"][0]["mode"] == "raw" and d["trip"]["overrides"]
    d = client.post(f"/api/trips/{tid}/undo", headers=H).json()
    assert not d["trip"]["overrides"] and d["trip"]["version"] == 3
    d = client.post(f"/api/trips/{tid}/recalculate", headers=H).json()
    assert d["history"][0]["reason"] == "recalculate"


def test_split_keeps_two_segments(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    seg = max(client.get(f"/api/trips/{tid}").json()["segments"], key=lambda s: s["t1"] - s["t0"])
    mid = (seg["t0"] + seg["t1"]) // 2
    d = client.post(f"/api/trips/{tid}/split", json={"ts": mid}, headers=H).json()
    halves = [s for s in d["segments"] if s["forced"]]
    assert len(halves) == 2 and halves[0]["mode"] == halves[1]["mode"]


def test_dry_run_changes_nothing(env, tmp_path):
    client, dw, jobs, db = env
    sync(client, jobs)
    jobs.cfg.push_dry_run = True
    before = {k: dict(v) for k, v in dw.points.items()}
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    r = client.post(f"/api/trips/{tid}/push", headers=H).json()
    assert r["result"]["dry_run"] and r["result"]["posted"] > 0
    assert dw.points == before
    assert r["trip"]["push"] == "original"


def test_failed_post_is_repaired_from_the_archive(env, monkeypatch):
    client, dw, jobs, db = env
    monkeypatch.setattr("mapmatch.portal.push.time.sleep", lambda s: None)
    sync(client, jobs)
    originals = dw.originals()
    dw.fail_post = True
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    assert client.post(f"/api/trips/{tid}/push", headers=H).status_code == 400
    assert client.get("/api/me").json()["dawarich_needs_repair"]
    dw.fail_post = False
    run_job(client, jobs, "repair")
    assert dw.originals() == originals and not dw.ours()
    assert not client.get("/api/me").json()["dawarich_needs_repair"]


def test_push_keeps_devices_so_tracks_do_not_bridge(env):
    """Matched points go on the device of the GPS they replace, and a revert puts
    every original back on its own device, so Dawarich's per-device tracks never
    join the fixes either side of a trip with a straight line."""
    client, dw, jobs, db = env
    devices = dw.devices()
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][-1]["id"]
    assert client.post(f"/api/trips/{tid}/push", headers=H).status_code == 200
    assert dw.ours() and {p["tracker_id"] for p in dw.points.values()} == {"phone"}
    assert client.post(f"/api/trips/{tid}/revert", headers=H).status_code == 200
    assert dw.devices() == devices


def test_hidden_anomalies_are_archived_and_cleared(env):
    """Dawarich hides points its anomaly filter flagged; they must still be
    archived and replaced, or they sit under the matched line as spikes."""
    client, dw, jobs, db = env
    sync(client, jobs)
    trip = client.get("/api/trips").json()["trips"][-1]
    mid = (trip["start"] + trip["end"]) // 2 + 1
    dw.seed([{"lat": 52.0, "lon": 1.0, "timestamp": mid}], anomaly=True)
    assert client.post(f"/api/trips/{trip['id']}/push", headers=H).status_code == 200
    assert not [p for p in dw.points.values() if p["timestamp"] == mid and p.get("ssid") != "mapmatch"]
    assert db.one("SELECT COUNT(*) n FROM points WHERE ts=?", (mid,))["n"] == 1


def test_late_point_marks_trip_dirty_and_rematches(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    t = client.get("/api/trips").json()["trips"][-1]
    pts = client.get(f"/api/trips/{t['id']}").json()["raw"]
    a, b = pts[5], pts[6]
    dw.seed([{"lat": (a["lat"] + b["lat"]) / 2, "lon": (a["lon"] + b["lon"]) / 2,
              "timestamp": (a["timestamp"] + b["timestamp"]) // 2}])
    sync(client, jobs)
    t2 = next(x for x in client.get("/api/trips").json()["trips"] if x["id"] == t["id"])
    assert t2["version"] == t["version"] + 1 and not t2["dirty"]


def test_csv_import(tmp_path, fake_valhalla):
    dw = FakeDawarich()
    cfg = Config(data_dir=tmp_path, base_path="/mm", dawarich_url="http://x", scheduler=False,
                 admin_user="admin", admin_password="adminpass")
    app = create_app(cfg, transport=dw.transport, start_workers=False)
    c = TestClient(app)
    assert c.post("/mm/api/login", json={"username": "admin", "password": "adminpass"}).status_code == 200
    assert c.post("/mm/api/connect", json={"api_key": "secret"}, headers=H).status_code == 200
    rows = ["tracker_id,timestamp,lat,lon,accuracy,velocity,altitude"]
    rows += [f"phone,{p['timestamp']},{p['lat']},{p['lon']},8,," for p in day(1_780_000_000)]
    r = c.post("/mm/api/import/csv", files={"file": ("pts.csv", "\n".join(rows), "text/csv")}, headers=H)
    assert r.status_code == 200, r.text
    app.state.portal.jobs.run_pending()
    assert c.get(f"/mm/api/jobs/{r.json()['job']}").json()["status"] == "done"
    assert len(c.get("/mm/api/trips").json()["trips"]) == 2
    assert c.get("/mm/").status_code == 200


def test_auth_rules(env):
    client, dw, jobs, db = env
    assert client.put("/api/settings", json={"auto_push": True}).status_code == 403   # no X-Portal header
    assert client.get("/api/me").json()["name"] == "rider@example.com"   # named after the Dawarich user
    stranger = TestClient(client.app)
    assert stranger.get("/api/me").status_code == 401
    # A key alone is not a login any more.
    assert stranger.post("/api/connect", json={"api_key": "secret"}, headers=H).status_code == 401
    assert stranger.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    again = TestClient(client.app)
    assert again.post("/api/login", json={"username": "admin", "password": "adminpass"}).status_code == 200
    assert again.get(f"/api/trips/{tid}").status_code == 200   # the same account, same trips


def test_gap_choice_is_per_trip(env, fake_valhalla):
    client, dw, jobs, db = env
    sync(client, jobs)
    trips = client.get("/api/trips").json()["trips"]
    a, b = trips[0], trips[1]
    assert a["gap_mode"] == "" and b["gap_mode"] == ""
    assert client.post(f"/api/trips/{a['id']}/push", headers=H).status_code == 200
    a = next(x for x in client.get("/api/trips").json()["trips"] if x["id"] == a["id"])
    assert a["push"] == "pushed"
    r = client.post(f"/api/trips/{a['id']}/gaps", json={"route": False}, headers=H)
    assert r.status_code == 200, r.text
    t = r.json()["trip"]
    assert t["gap_mode"] == "strict" and t["version"] == a["version"] + 1
    assert t["push"] == "outdated", "the route changed, so Dawarich is behind"
    other = next(x for x in client.get("/api/trips").json()["trips"] if x["id"] == b["id"])
    assert other["gap_mode"] == "" and other["version"] == b["version"], "only this trip changed"
    # Choosing again is one more change; null hands it back to the account setting.
    t = client.post(f"/api/trips/{a['id']}/gaps", json={"route": None}, headers=H).json()["trip"]
    assert t["gap_mode"] == "" and t["version"] == a["version"] + 2
    assert client.post(f"/api/trips/{a['id']}/gaps", json={"route": "yes"}, headers=H).status_code == 422


def test_keeping_gaps_changes_the_route(env, fake_valhalla):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    # A fix in the middle of the trace that Valhalla cannot match breaks the
    # match into two pieces with recorded fixes between them: a real gap.
    fake_valhalla.unmatched = {5}
    filled = client.post(f"/api/trips/{tid}/gaps", json={"route": True}, headers=H).json()["trip"]["quality"]
    kept = client.post(f"/api/trips/{tid}/gaps", json={"route": False}, headers=H).json()["trip"]["quality"]
    assert kept["raw_frac"] > filled["raw_frac"], (filled, kept)
    assert filled["filled_frac"] > kept["filled_frac"], (filled, kept)


def test_each_gap_can_be_chosen_separately(env, fake_valhalla):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    fake_valhalla.unmatched = {5, 12}          # two holes in the trace
    d = client.post(f"/api/trips/{tid}/recalculate", headers=H).json()
    holes = [p for s in d["segments"] for p in s["pieces"] if p.get("gap")]
    assert len(holes) >= 2, holes
    first = holes[0]["gap"]
    d = client.post(f"/api/trips/{tid}/gaps", json={"t0": first[0], "t1": first[1], "route": False},
                    headers=H).json()
    assert d["trip"]["keep_gaps"] == [first]
    kinds = {tuple(p["gap"]): p["kind"] for s in d["segments"] for p in s["pieces"] if p.get("gap")}
    assert kinds[tuple(first)] in ("raw", "straight"), "that hole keeps the recorded GPS"
    others = [k for g, k in kinds.items() if g != tuple(first)]
    assert "filled" in others, "the other holes still route"
    # Switching it back forgets the exception.
    d = client.post(f"/api/trips/{tid}/gaps", json={"t0": first[0], "t1": first[1], "route": True},
                    headers=H).json()
    assert d["trip"]["keep_gaps"] == []
    # "Route them all" clears any per-gap choices too.
    client.post(f"/api/trips/{tid}/gaps", json={"t0": first[0], "t1": first[1], "route": False}, headers=H)
    d = client.post(f"/api/trips/{tid}/gaps", json={"route": True}, headers=H).json()
    assert d["trip"]["keep_gaps"] == [] and d["trip"]["gap_mode"] == "fill"


def test_route_anyway_reaches_the_engine(env, fake_valhalla, monkeypatch):
    from mapmatch.portal import trips as T
    seen: dict = {}
    real = T.match_trip
    def spy(points, overrides=None, options=None):
        seen.clear()
        seen.update(options or {})
        return real(points, overrides=overrides, options=options)
    monkeypatch.setattr(T, "match_trip", spy)
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    d = client.get(f"/api/trips/{tid}").json()
    hole = next(p for s in d["segments"] for p in s["pieces"] if p.get("gap"))["gap"]
    # A gap on its own, past the gate.
    d = client.post(f"/api/trips/{tid}/gaps", json={"t0": hole[0], "t1": hole[1], "route": True, "force": True},
                    headers=H).json()
    assert d["trip"]["force_gaps"] == [hole]
    assert seen["force_gaps"] == [hole] and seen["force_route"] is False
    # The whole trip, past the gate.
    d = client.post(f"/api/trips/{tid}/gaps", json={"route": True, "force": True}, headers=H).json()
    assert d["trip"]["force_route"] is True
    assert seen["force_route"] is True and seen["force_gaps"] == []
    # Keeping them clears both overrides.
    d = client.post(f"/api/trips/{tid}/gaps", json={"route": False}, headers=H).json()
    assert d["trip"]["force_route"] is False and seen["force_route"] is False
    # And an automatic rematch without an override passes nothing on.
    client.post(f"/api/trips/{tid}/gaps", json={"route": True}, headers=H)
    assert seen["force_route"] is False


def test_accounts_are_made_by_admins(env):
    client, dw, jobs, db = env
    # Sign-up is off, so a stranger cannot register.
    assert client.get("/api/public").json()["signup"] is False
    stranger = TestClient(client.app)
    r = stranger.post("/api/register", json={"username": "someone", "password": "longenough"})
    assert r.status_code == 403
    # An admin makes the account, then it signs in and links its own key.
    r = client.post("/api/admin/users", json={"username": "sam", "password": "sams long password"}, headers=H)
    assert r.status_code == 200, r.text
    sam = TestClient(client.app)
    assert sam.post("/api/login", json={"username": "sam", "password": "sams long password"}).status_code == 200
    me = sam.get("/api/me").json()
    assert me["is_admin"] is False and me["dawarich_linked"] is False
    assert len(sam.get("/api/trips").json()["trips"]) == 0
    assert sam.post("/api/connect", json={"api_key": "secret"}, headers=H).status_code == 200
    assert sam.get("/api/me").json()["dawarich_linked"] is True
    # Admin keeps control: disable stops the sign-in dead.
    uid = next(u["id"] for u in client.get("/api/admin/site").json()["users"] if u["username"] == "sam")
    assert client.patch(f"/api/admin/users/{uid}", json={"disabled": True}, headers=H).status_code == 200
    assert TestClient(client.app).post("/api/login", json={"username": "sam", "password": "sams long password"}).status_code == 401


def test_admin_sets_one_valhalla_and_region_catalogue(env, fake_valhalla):
    client, dw, jobs, db = env
    assert client.get("/api/system").json()["valhalla_url"] == ""
    # Saved by an admin; applies to the whole instance, so a member sees it too.
    r = client.put("/api/admin/site", json={"valhalla_url": "http://valhalla.test:8002"}, headers=H)
    assert r.status_code == 200, r.text
    assert client.get("/api/system").json()["valhalla_url"] == "http://valhalla.test:8002"
    assert client.put("/api/admin/site", json={"valhalla_url": "ftp://nope"}, headers=H).status_code == 400
    # Region catalogue.
    r = client.post("/api/admin/regions", json={"name": "England and Wales", "bounds": "50,-5.8,55.8,1.8"}, headers=H)
    assert r.status_code == 200 and r.json()["regions"][0]["name"] == "England and Wales"
    rid = r.json()["id"]
    r = client.patch(f"/api/admin/regions/{rid}", json={"built_at": "2026-11-01"}, headers=H)
    assert r.json()["regions"][0]["built_at"] == "2026-11-01"
    assert [x["name"] for x in client.get("/api/system").json()["regions"]] == ["England and Wales"]
    assert client.delete(f"/api/admin/regions/{rid}", headers=H).json()["regions"] == []
    # Only admins may touch any of it.
    stranger = TestClient(client.app)
    assert stranger.post("/api/admin/users", json={"username": "x", "password": "12345678"}).status_code == 401
    r = client.post("/api/admin/users", json={"username": "mem", "password": "mem password"}, headers=H)
    mem = TestClient(client.app)
    assert mem.post("/api/login", json={"username": "mem", "password": "mem password"}).status_code == 200
    assert mem.put("/api/admin/site", json={"signup": True}, headers=H).status_code == 403


def test_signup_opens_with_admin_permission(env):
    client, dw, jobs, db = env
    assert client.put("/api/admin/site", json={"signup": True}, headers=H).json()["signup"] is True
    fresh = TestClient(client.app)
    r = fresh.post("/api/register", json={"username": "newbie", "password": "newbie password"})
    assert r.status_code == 200 and r.json()["is_admin"] is False
    assert fresh.get("/api/me").json()["username"] == "newbie"


def test_admin_cli_resets_a_password(tmp_path):
    from mapmatch.portal import admin as cli
    from mapmatch.portal.db import Database

    db = Database(tmp_path / "portal.db")
    uid = __import__("mapmatch.portal.auth", fromlist=["x"]).create_user(db, "someone", "old password")
    monkeypatch_env = {"PORTAL_DATA": str(tmp_path)}
    import os
    old = {k: os.environ.get(k) for k in monkeypatch_env}
    os.environ.update(monkeypatch_env)
    try:
        assert cli.main(["reset", "someone", "--password", "new password"]) == 0
        assert cli.main(["reset", "nobody", "--password", "new password"]) == 1
        assert cli.main(["list"]) == 0
        from mapmatch.portal import auth
        assert auth.authenticate(db, "someone", "new password") is not None
        assert uid == 1
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_upgrade_rescues_the_old_key_only_admin(tmp_path, fake_valhalla):
    """An install made by the key-only sign-in has an admin with no password."""
    from mapmatch.portal import auth
    from mapmatch.portal.config import Config
    from mapmatch.portal.db import Database

    db = Database(tmp_path / "old.db")
    with db.tx() as c:
        c.execute("INSERT INTO users(username, password, is_admin, created_at)"
                  " VALUES ('dawarich:dawarich.example:5','!',1,'2026-01-01T00:00:00+00:00')")
        c.execute("INSERT INTO settings(user_id) VALUES (1)")
    auth.bootstrap_admin(db, "andre", "a long password")
    row = db.one("SELECT username, password, is_admin FROM users")
    assert row["username"] == "andre" and row["is_admin"] == 1
    assert auth.authenticate(db, "andre", "a long password") is not None
    assert auth.authenticate(db, "andre", "wrong password") is None


def test_first_account_on_a_fresh_install_is_the_admin(tmp_path, fake_valhalla):
    dw = FakeDawarich()
    cfg = Config(data_dir=tmp_path, base_path="/", dawarich_url="http://x", scheduler=False)
    app = create_app(cfg, transport=dw.transport, start_workers=False)
    c = TestClient(app)
    assert c.get("/api/public").json()["first_run"] is True
    r = c.post("/api/register", json={"username": "founder", "password": "founder password"})
    assert r.status_code == 200 and r.json()["is_admin"] is True
    assert c.get("/api/me").json()["is_admin"] is True


def test_scheduler_runs_auto_with_push(env):
    client, dw, jobs, db = env
    client.put("/api/settings", json={"auto_enabled": True, "auto_push": True, "interval_min": 60}, headers=H)
    assert jobs.tick()
    jobs.run_pending()
    trips = client.get("/api/trips").json()["trips"]
    assert trips and all(t["push"] == "pushed" for t in trips)
    assert not jobs.tick(), "not due again until the interval passes"


def test_trip_changes_feed_for_live_drawing(env):
    client, dw, jobs, db = env
    run_job(client, jobs, "sync")
    # Times are whole seconds and the feed repeats the boundary second, so an
    # unmatched trip may show up here; the page only draws newer versions.
    first = client.get("/api/trips/changes").json()
    assert all(t["version"] == 0 for t in first["trips"])
    job = run_job(client, jobs, "rematch", {"scope": "pending"})
    got = client.get(f"/api/trips/changes?since={job['created_at']}").json()
    assert len(got["trips"]) == 2
    assert all(t["version"] == 1 and t["modes"] and len(t["preview"]) > 1 for t in got["trips"])
    assert client.get(f"/api/trips/changes?since={got['now']}").status_code == 200


def test_preview_has_one_coloured_part_per_segment(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    for t in client.get("/api/trips").json()["trips"]:
        assert [p["m"] for p in t["preview"]] == [m["mode"] for m in t["modes"]]


def test_range_mode_pins_part_of_a_segment(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    d = client.get(f"/api/trips/{tid}").json()
    seg = max(d["segments"], key=lambda s: s["t1"] - s["t0"])
    a = seg["t0"] + (seg["t1"] - seg["t0"]) // 3
    b = seg["t0"] + 2 * (seg["t1"] - seg["t0"]) // 3
    d = client.post(f"/api/trips/{tid}/range", json={"t0": a, "t1": b, "mode": "train"}, headers=H).json()
    assert d["trip"]["overrides"] == [{"t0": a, "t1": b, "mode": "train"}]
    assert any(s["mode"] == "train" and s["forced"] for s in d["segments"])
    # a second range inside the first keeps the outer parts of the first
    d = client.post(f"/api/trips/{tid}/range", json={"t0": a + 60, "t1": a + 120, "mode": "walk"}, headers=H).json()
    assert [o["mode"] for o in d["trip"]["overrides"]] == ["train", "walk", "train"]


def test_point_edits_change_the_match_not_the_archive(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    n = archive_count(db)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    d = client.get(f"/api/trips/{tid}").json()
    p, q = d["raw"][5], d["raw"][6]
    edits = {str(p["id"]): [p["lat"] + 0.0003, p["lon"]], str(q["id"]): "x"}
    d2 = client.put(f"/api/trips/{tid}/points", json={"edits": edits}, headers=H).json()
    assert d2["trip"]["point_edits"] == 2 and d2["raw"][5]["edit"] == [round(p["lat"] + 0.0003, 7), round(p["lon"], 7)]
    assert d2["raw"][5]["lat"] == p["lat"], "the archived original is unchanged"
    assert archive_count(db) == n
    assert client.put(f"/api/trips/{tid}/points", json={"edits": {"999999": "x"}}, headers=H).status_code == 400
    d3 = client.put(f"/api/trips/{tid}/points", json={"edits": {}}, headers=H).json()
    assert d3["trip"]["point_edits"] == 0


def test_match_selected_and_range(env):
    client, dw, jobs, db = env
    run_job(client, jobs, "sync")
    ts = sorted(client.get("/api/trips").json()["trips"], key=lambda t: t["start"])
    run_job(client, jobs, "rematch", {"scope": "ids", "ids": [ts[0]["id"]]})
    got = {t["id"]: t["version"] for t in client.get("/api/trips").json()["trips"]}
    assert got[ts[0]["id"]] == 1 and got[ts[1]["id"]] == 0
    run_job(client, jobs, "rematch", {"scope": "range", "start": ts[1]["start"], "end": ts[1]["end"]})
    got = {t["id"]: t["version"] for t in client.get("/api/trips").json()["trips"]}
    assert got[ts[0]["id"]] == 1 and got[ts[1]["id"]] == 1


def test_history_opens_an_old_version_and_goes_back_to_it(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    v1 = client.get(f"/api/trips/{tid}").json()["trip"]["version"]
    t = client.post(f"/api/trips/{tid}/gaps", json={"route": False}, headers=H).json()["trip"]
    assert t["gap_mode"] == "strict"
    old = client.get(f"/api/trips/{tid}/versions/{v1}").json()
    assert old["version"] == v1 and old["segments"] and old["choices"]["gap_mode"] == ""
    d = client.post(f"/api/trips/{tid}/restore", json={"version": v1}, headers=H).json()
    assert d["trip"]["gap_mode"] == "", "the trip's choices come back with the version"
    assert d["trip"]["version"] == t["version"] + 1 and d["history"][0]["reason"] == f"back to v{v1}"
    assert client.get(f"/api/trips/{tid}/versions/999").status_code == 404


def test_push_tidies_the_stops_beside_a_trip_and_revert_restores_them(env):
    """Jitter while stopped becomes one clean point per stop (every half hour);
    a revert of the trip puts every recorded fix back."""
    client, dw, jobs, db = env
    originals = dw.originals()
    sync(client, jobs)
    listing = client.get("/api/trips").json()["trips"]
    trip = listing[-1]
    nxt = min(t["start"] for t in listing if t["start"] > trip["end"])
    stop = [k for k in originals if trip["end"] < k[0] < nxt]
    assert len(stop) > 20, "the fake day has 40 minutes of jitter at the office"
    assert client.post(f"/api/trips/{trip['id']}/push", headers=H).status_code == 200
    tidied = [p for p in dw.ours() if trip["end"] < p["timestamp"] < nxt]
    where = __import__("collections").Counter((p["latitude"], p["longitude"]) for p in tidied)
    (_, n), = [x for x in where.items() if x[1] > 1]
    assert 2 <= n <= 4, "one place: arrival, departure and every half hour"
    assert not [k for k in dw.originals() if trip["end"] < k[0] < nxt]
    assert client.post(f"/api/trips/{trip['id']}/revert", headers=H).status_code == 200
    assert dw.originals() == originals and not dw.ours()


def test_a_long_old_ride_is_one_trip_across_download_pages(env, monkeypatch):
    from mapmatch.portal import dawarich, trips
    client, dw, jobs, db = env
    monkeypatch.setattr(dawarich, "PAGE", 100)
    t0 = 1_700_000_000                                   # years old: the clock says every trip is over
    dw.points.clear()
    dw.seed(track([(5, 0)] * 450, t0=t0, dt=1))         # a 7.5 min ride, logged every second
    run_job(client, jobs, "sync")
    rows = db.all("SELECT * FROM trips ORDER BY start_ts")
    assert len(rows) == 1 and rows[0]["n_points"] == 451 and rows[0]["closed"]

    # Trips already cut by the old sync are joined again on the next one.
    with db.tx() as c:
        mid = t0 + 200
        c.execute("UPDATE trips SET end_ts=?, n_points=201 WHERE id=?", (mid, rows[0]["id"]))
        c.execute("INSERT INTO trips(user_id, start_ts, end_ts, n_points, closed, created_at, updated_at)"
                  " VALUES (?,?,?,?,1,'x','x')", (rows[0]["user_id"], mid + 1, t0 + 450, 250))
    trips.segment(db, rows[0]["user_id"], 30)
    again = db.all("SELECT * FROM trips ORDER BY start_ts")
    assert [(r["id"], r["n_points"]) for r in again] == [(rows[0]["id"], 451)]


def test_detailed_trip_can_be_snapped_and_kept_again(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    d = client.put(f"/api/trips/{tid}/recorded", json={"snap": True}, headers=H).json()
    assert d["trip"]["snap_recorded"] is True and d["history"][0]["reason"] == "snapped to the map"
    d = client.put(f"/api/trips/{tid}/recorded", json={"snap": False}, headers=H).json()
    assert d["trip"]["snap_recorded"] is False
    # Going back to the snapped version brings the choice back too.
    v = d["history"][1]["version"]
    d = client.post(f"/api/trips/{tid}/restore", json={"version": v}, headers=H).json()
    assert d["trip"]["snap_recorded"] is True
