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
    cfg = Config(data_dir=tmp_path, base_path="/", dawarich_url="http://dawarich.test",
                 admin_user="admin", admin_password="adminpass1", scheduler=False)
    app = create_app(cfg, transport=dw.transport, start_workers=False)
    client = TestClient(app)
    assert client.post("/api/login", json={"username": "admin", "password": "adminpass1"}).status_code == 200
    client.put("/api/settings", json={"api_key": "secret"}, headers=H)
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
    assert t["modes"] == [] and len(t["preview"]) > 2 and t["km"] > 1   # raw line and distance
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
    trip = client.get("/api/trips").json()["trips"][-1]   # oldest first after reversing
    tid = trip["id"]
    outside = {k for k in originals if not trip["start"] <= k[0] <= trip["end"]}

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
    assert r["result"]["dry_run"] and r["result"]["post"] > 0
    assert dw.points == before
    assert r["trip"]["push"] == "original"


def test_failed_post_leaves_originals(env):
    client, dw, jobs, db = env
    sync(client, jobs)
    originals = dw.originals()
    dw.fail_post = True
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    assert client.post(f"/api/trips/{tid}/push", headers=H).status_code == 400
    assert dw.originals() == originals and not dw.ours()


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
    cfg = Config(data_dir=tmp_path, base_path="/mm", dawarich_url="http://x", admin_user="admin",
                 admin_password="adminpass1", scheduler=False)
    app = create_app(cfg, start_workers=False)
    c = TestClient(app)
    c.post("/mm/api/login", json={"username": "admin", "password": "adminpass1"})
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
    assert client.post("/api/signup", json={"username": "bob", "password": "longenough"}).status_code == 403
    client.post("/api/admin/signup", json={"enabled": True}, headers=H)
    sync(client, jobs)
    tid = client.get("/api/trips").json()["trips"][0]["id"]
    other = TestClient(client.app)
    assert other.post("/api/signup", json={"username": "bob", "password": "longenough"}).status_code == 200
    assert other.get(f"/api/trips/{tid}").status_code == 404     # not bob's trip
    assert other.get("/api/admin").status_code == 403
    assert TestClient(client.app).get("/api/me").status_code == 401


def test_scheduler_runs_auto_with_push(env):
    client, dw, jobs, db = env
    client.put("/api/settings", json={"auto_enabled": True, "auto_push": True, "interval_min": 60}, headers=H)
    assert jobs.tick()
    jobs.run_pending()
    trips = client.get("/api/trips").json()["trips"]
    assert trips and all(t["push"] == "pushed" for t in trips)
    assert not jobs.tick(), "not due again until the interval passes"
