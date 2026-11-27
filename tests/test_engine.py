"""Engine unit tests. No network: Valhalla is faked in conftest.py."""
from __future__ import annotations

import math

from conftest import LAT0, LON0, T0, encode_polyline, offset, track

from mapmatch.engine import valhalla
from mapmatch.engine.api import match_trip
from mapmatch.engine.clean import clean
from mapmatch.engine.fill import fill
from mapmatch.engine.io import read_csv_text
from mapmatch.engine.modes import label, runs, speeds
from mapmatch.engine.stays import find_stays
from mapmatch.engine.trips import split_trips

WALK = [(40, 0)] * 12          # 40 m / 30 s ≈ 4.8 km/h
DRIVE = [(500, 0)] * 12        # 500 m / 30 s = 60 km/h
STILL = [(0, 0)] * 10


# ---------------------------------------------------------------- io / clean
def test_csv_reader_tolerates_empty_fields():
    text = ("tracker_id,timestamp,lat,lon,accuracy,velocity,altitude\n"
            "a,1780000000,54.0,2.5,10,,\n"
            "a,1780000030,54.001,2.5,,3.2,12\n"
            "a,,54.0,2.5,10,0,0\n")
    pts = read_csv_text(text)
    assert len(pts) == 2
    assert pts[0]["velocity"] is None and pts[1]["accuracy"] is None and pts[1]["velocity"] == 3.2


def test_clean_drops_duplicates_bad_clock_accuracy_and_spikes():
    pts = track(WALK)
    dup = dict(pts[3], accuracy=50.0)               # same second, worse accuracy
    bogus = dict(pts[0], timestamp=1_704_067_200)   # 2024, a year before the rest
    noisy = dict(pts[5], timestamp=pts[5]["timestamp"] + 1, accuracy=400.0)
    spike = dict(pts[8], timestamp=pts[8]["timestamp"] + 2, lat=pts[8]["lat"] + 0.05)
    kept, dropped = clean(pts + [dup, bogus, noisy, spike])
    reasons = sorted(p["_drop"] for p in dropped)
    assert reasons == ["bad_time", "duplicate", "low_accuracy", "spike"]
    assert len(kept) == len(pts)
    assert all(a["timestamp"] < b["timestamp"] for a, b in zip(kept, kept[1:]))


def test_clean_drops_short_excursion():
    pts = track(DRIVE)
    far = [dict(pts[6], timestamp=pts[6]["timestamp"] + k, lat=pts[6]["lat"] + 0.08) for k in (5, 10)]
    kept, dropped = clean(pts + far)
    assert sorted(p["_drop"] for p in dropped) == ["teleport", "teleport"]
    assert len(kept) == len(pts)


# ---------------------------------------------------------------- stays / trips
def test_stays_found_and_sparse_stay_counts():
    pts = track(WALK + STILL + WALK)
    runs_ = find_stays(pts)
    assert len(runs_) == 1
    a, b = runs_[0]
    assert pts[b - 1]["timestamp"] - pts[a]["timestamp"] >= 180
    # Two fixes an hour apart at the same place are a stay too.
    sparse = track([(0, 0)], dt=3600)
    assert find_stays(sparse) == [(0, 2)]


def test_split_trips_by_long_stay_and_gap_and_drops_jitter():
    morning = track(DRIVE)
    stay = track([(1, 1)] * 70, start=(morning[-1]["lat"], morning[-1]["lon"]),
                 t0=morning[-1]["timestamp"] + 30)                 # 35 min at the office
    evening = track(DRIVE, start=(stay[-1]["lat"], stay[-1]["lon"]), t0=stay[-1]["timestamp"] + 30)
    jitter = track([(5, -5), (-5, 5)] * 5, start=(evening[-1]["lat"], evening[-1]["lon"]),
                   t0=evening[-1]["timestamp"] + 4000)            # pottering at home
    late = track(WALK, start=(evening[-1]["lat"], evening[-1]["lon"]),
                 t0=jitter[-1]["timestamp"] + 4000)
    trips = split_trips(morning + stay + evening + jitter + late)
    assert len(trips) == 3
    assert [len(t) for t in trips][0] >= len(morning) - 1


# ---------------------------------------------------------------- modes
def test_speeds_prefer_device_velocity():
    pts = track(WALK, velocity=15.0)               # positions say 5 km/h, phone says 54 km/h
    assert all(abs(v - 54.0) < 1e-6 for v in speeds(pts))
    pts = track(WALK, velocity=0.0)                # zero means unknown: use positions
    assert all(3.0 < v < 7.0 for v in speeds(pts)[1:-1])


def test_labels_walk_then_drive_without_flicker():
    pts = track(WALK + DRIVE)
    labels, forced, keys = label(pts)
    rs = [m for m, _, _ in runs(labels)]
    assert rs == ["walk", "drive"]
    assert not any(forced)


def test_short_run_merges_into_neighbour():
    pts = track(DRIVE[:8] + [(40, 0)] * 2 + DRIVE[:8])   # a 60 s crawl in traffic
    labels, _, _ = label(pts)
    assert set(labels) == {"drive"}


def test_override_forces_mode():
    pts = track(DRIVE)
    ov = [{"t0": pts[0]["timestamp"], "t1": pts[-1]["timestamp"], "mode": "cycle"}]
    labels, forced, _ = label(pts, ov)
    assert set(labels) == {"cycle"} and all(forced)


def test_adjacent_stays_keep_separate_keys():
    a = track(STILL)
    b = track(STILL, start=(a[0]["lat"] + 0.05, a[0]["lon"]), t0=a[-1]["timestamp"] + 60)
    labels, _, keys = label(a + b)
    assert set(labels) == {"stay"}
    assert len({k for k in keys}) == 2


# ---------------------------------------------------------------- valhalla parsing
def test_match_splits_pieces_at_discontinuities(monkeypatch):
    shape = [(54.0, 2.5), (54.0, 2.501), (54.0, 2.502), (54.01, 2.6), (54.01, 2.601)]
    resp = {
        "shape": encode_polyline(shape),
        "edges": [{"begin_shape_index": 0, "end_shape_index": 2},
                  {"begin_shape_index": 3, "end_shape_index": 4}],
        "matched_points": [
            {"type": "matched", "edge_index": 0, "distance_along_edge": 0.0, "lat": 54.0, "lon": 2.5},
            {"type": "interpolated", "edge_index": 0, "distance_along_edge": 0.5, "lat": 54.0, "lon": 2.501},
            {"type": "unmatched", "lat": 54.005, "lon": 2.55},
            {"type": "matched", "edge_index": 1, "distance_along_edge": 1.0, "lat": 54.01, "lon": 2.601},
        ],
    }
    monkeypatch.setattr(valhalla, "post", lambda *a, **k: resp)
    m = valhalla.match([{"lat": 0, "lon": 0, "timestamp": T0 + i} for i in range(4)], "auto")
    assert len(m["pieces"]) == 2
    a = m["anchors"]
    assert a[0]["piece"] == 0 and a[1]["piece"] == 0 and a[2] is None and a[3]["piece"] == 1
    assert a[1]["arc"] > a[0]["arc"]   # interpolated points are kept, with a real position


# ---------------------------------------------------------------- fill
def test_fill_accepts_plausible_route_and_rejects_detour(fake_valhalla):
    pts = track([(300, 0)])
    a, b = pts
    assert fill(a, [], b, "drive")["kind"] == "filled"
    fake_valhalla.route_detour = 3.0
    out = fill(a, [], b, "drive")
    assert out["kind"] == "straight" and "too_long" in out["why"]
    mid = track([(150, 40)])[1] | {"timestamp": a["timestamp"] + 15}
    assert fill(a, [mid], b, "drive")["kind"] == "raw"   # keeps the fix, never a straight jump


def test_strict_never_routes_even_with_an_empty_hole(fake_valhalla):
    # The common case in real data: the GPS simply stopped recording, so the
    # hole has no fixes inside it. "Keep the recorded GPS" must still not route.
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0}
    b = {"lat": LAT0 + 0.01, "lon": LON0, "timestamp": T0 + 300}
    assert fill(a, [], b, "drive", strength="fill")["kind"] == "filled"
    assert fill(a, [], b, "drive", strength="strict")["kind"] == "straight"


def test_route_anyway_waves_a_fast_route_through(fake_valhalla):
    # 5.5 km in 30 s is 660 km/h: the gate refuses it, and the editor's explicit
    # "route it anyway" is the only thing that accepts it.
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0}
    b = {"lat": LAT0 + 0.05, "lon": LON0, "timestamp": T0 + 30}
    refused = fill(a, [], b, "drive")
    assert refused["kind"] == "straight" and "too_fast" in refused["why"]
    assert fill(a, [], b, "drive", force=True)["kind"] == "filled"
    # It is an override of the speed judgement, not of a broken route: a hole
    # with no route at all still falls back to the recorded points.
    fake_valhalla.fail_route = True
    assert fill(a, [], b, "drive", force=True)["kind"] == "straight"


def test_one_hole_can_be_kept_while_others_route(fake_valhalla):
    from mapmatch.engine.api import match_trip
    fake_valhalla.unmatched = {10, 25}
    pts = track([(500, 0)] * 40)
    everything = match_trip(pts, options={"strength": "fill"})
    holes = [p for s in everything["segments"] for p in s["pieces"] if p.get("gap")]
    assert len(holes) == 2 and {p["kind"] for p in holes} == {"filled"}, holes
    first = holes[0]["gap"]
    chosen = match_trip(pts, options={"strength": "fill", "keep_gaps": [first]})
    kinds = {tuple(p["gap"]): p["kind"] for s in chosen["segments"] for p in s["pieces"] if p.get("gap")}
    assert kinds[tuple(first)] in ("raw", "straight"), "this one stays as recorded"
    assert any(k == "filled" for g, k in kinds.items() if g != tuple(first)), "the other still routes"


def test_fill_strict_does_not_route_through_unmatched_fixes(fake_valhalla):
    a, mid, b = track([(150, 0), (150, 0)])
    assert fill(a, [mid], b, "drive", strength="strict")["kind"] == "raw"
    assert "/route" not in fake_valhalla.calls


# ---------------------------------------------------------------- whole trip
def _check_stream(out):
    ts = [p["timestamp"] for p in out["stream"]]
    assert ts == sorted(ts) and len(ts) == len(set(ts)), "stream timestamps must strictly increase"


def test_match_trip_gap_does_not_create_single_fix_segments(fake_valhalla):
    first = track(DRIVE)
    second = track(DRIVE, start=(first[-1]["lat"], first[-1]["lon"] + 0.01),
                   t0=first[-1]["timestamp"] + 1200)     # 20 min dropout
    out = match_trip(first + second)
    _check_stream(out)
    for s in out["segments"]:
        assert s["n_fixes"] > 1
        for p in s["pieces"]:
            assert not (p["kind"] == "raw" and p["n_in"] <= 1)
    assert out["quality"]["raw_frac"] == 0.0


def test_match_trip_walk_stay_drive(fake_valhalla):
    walk = track(WALK)
    stay = track(STILL, start=(walk[-1]["lat"], walk[-1]["lon"]), t0=walk[-1]["timestamp"] + 30)
    drive = track(DRIVE, start=(stay[-1]["lat"], stay[-1]["lon"]), t0=stay[-1]["timestamp"] + 30)
    out = match_trip(walk + stay + drive)
    _check_stream(out)
    assert [s["mode"] for s in out["segments"]] == ["walk", "stay", "drive"]
    assert out["segments"][1]["km"] == 0.0
    assert out["quality"]["matched_frac"] > 0.9


def test_unmatched_fixes_are_routed_then_kept_raw(fake_valhalla):
    pts = track(DRIVE)
    fake_valhalla.unmatched = {4, 5, 6}
    out = match_trip(pts)
    kinds = {p["kind"] for s in out["segments"] for p in s["pieces"]}
    assert "filled" in kinds and "raw" not in kinds
    fake_valhalla.fail_route = True
    out = match_trip(pts)
    kinds = [p["kind"] for s in out["segments"] for p in s["pieces"]]
    assert "raw" in kinds and "straight" not in kinds
    _check_stream(out)


def test_override_raw_keeps_original_fixes(fake_valhalla):
    pts = track(DRIVE)
    ov = [{"t0": pts[0]["timestamp"], "t1": pts[-1]["timestamp"], "mode": "raw"}]
    out = match_trip(pts, overrides=ov)
    assert [s["mode"] for s in out["segments"]] == ["raw"]
    coords = out["segments"][0]["pieces"][0]["coords"]
    assert [(c[0], c[1]) for c in coords] == [(p["lat"], p["lon"]) for p in pts]
    assert "/trace_attributes" not in fake_valhalla.calls


# ---------------------------------------------------------------- v2.1 rules
from mapmatch.engine.assemble import remove_spurs  # noqa: E402
from conftest import offset, LAT0, LON0  # noqa: E402


def _line(steps, t0=T0):
    lat, lon, t = LAT0, LON0, t0
    out = [[lat, lon, t, 1]]
    for e, n in steps:
        lat, lon = offset(lat, lon, e, n)
        t += 10
        out.append([lat, lon, t, 1])
    return out


def test_spur_with_one_poor_fix_is_cut():
    # along a road, 60 m down a side road and straight back, then on
    line = _line([(50, 0), (50, 0), (0, 60), (0, -60), (50, 0), (50, 0)])
    acc = {int(c[2]): 25.0 for c in line}
    out = remove_spurs(line, acc)
    assert max(abs(c[0] - LAT0) for c in out) * 111_320 < 5     # the side road is gone
    assert [c[2] for c in out] == sorted(c[2] for c in out)


def test_spur_with_two_accurate_fixes_is_kept():
    line = _line([(50, 0), (50, 0), (0, 40), (0, 40), (0, -40), (0, -40), (50, 0)])
    acc = {int(c[2]): 6.0 for c in line}
    assert len(remove_spurs(line, acc)) == len(line)


def test_stale_phone_speed_loses_only_at_train_speeds():
    train = track([(3000, 0)] * 6, velocity=5.0, accuracy=5.0)    # 360 km/h by position
    assert min(speeds(train)) > 300
    jump = track([(40, 0), (40, 0), (900, 0), (40, 0), (40, 0)], velocity=4.5, accuracy=10.0)
    assert max(speeds(jump)) < 20                               # one GPS jump is not a drive


def test_fast_burst_is_not_merged_into_a_walk():
    pts = track([(350, 0)] * 2 + WALK)        # two fixes at 42 km/h, then walking
    labels, _, _ = label(pts)
    assert labels[0] != "walk" and labels[-1] == "walk"


def test_train_beats_drive_at_impossible_car_speed():
    from mapmatch.engine.modes import _cost
    assert _cost("drive", 350, False) > 30 and _cost("drive", 120, False) == 0.0


def test_trail_fill_keeps_gps_when_route_strays(fake_valhalla):
    a, m1, m2, b = track([(100, 0), (100, 0), (100, 0)])
    fake_valhalla.route_detour = 1.6          # a plausible length, but 60 m off the fixes
    out = fill(a, [m1, m2], b, "walk")
    assert out["kind"] == "raw" and "off_trail" in out["why"]
    fake_valhalla.route_detour = 1.0
    assert fill(a, [m1, m2], b, "walk")["kind"] == "filled"


def test_slow_scramble_is_walking_not_a_string_of_stays():
    """Crib Goch: a fix every two minutes, 15-25 m apart, with a long pause."""
    from mapmatch.engine.modes import label
    climb = track([(18, 6)] * 40, dt=120)                       # ~0.6 km/h for 80 min
    pause = track([(1, -1), (-1, 1)] * 6, start=(climb[-1]["lat"], climb[-1]["lon"]),
                  t0=climb[-1]["timestamp"] + 120, dt=90)       # 18 min on the ridge
    on = track([(18, 6)] * 20, start=(pause[-1]["lat"], pause[-1]["lon"]),
               t0=pause[-1]["timestamp"] + 120, dt=120)
    labels, _, _ = label(climb + pause + on)
    assert "stay" not in labels and set(labels) == {"walk"}


def test_stop_to_trip_link_follows_the_road_between_their_times(fake_valhalla):
    from mapmatch.portal.push import link
    fake_valhalla.route_detour = 1.2          # a road with a bend in it
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0, "tracker": "phone"}
    b = {"lat": LAT0, "lon": LON0 + 0.015, "timestamp": T0 + 300}
    pts = link(a, b, "drive")
    assert pts, "a routed link"
    assert all(T0 < p["timestamp"] < T0 + 300 for p in pts)
    assert [p["timestamp"] for p in pts] == sorted({p["timestamp"] for p in pts})
    assert all(p["ours"] and p["tracker"] == "phone" for p in pts)
    assert link(a, {**a, "timestamp": T0 + 60}) == [], "nothing to draw for a few metres"


def test_no_link_is_guessed_across_a_journey_nobody_logged(fake_valhalla):
    """Two hours at a place, then nothing for an hour, then a drive starting
    4.6 km away: the hour between is unknown, not a walk to invent."""
    from mapmatch.portal.push import link
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0, "tracker": "phone"}
    assert link(a, {"lat": LAT0, "lon": LON0 + 0.067, "timestamp": T0 + 3600}) == [], "4.6 km, an hour unrecorded"
    assert link(a, {"lat": LAT0, "lon": LON0 + 0.004, "timestamp": T0 + 3600}) == [], "short, but an hour unrecorded"
    assert link(a, {"lat": LAT0, "lon": LON0 + 0.004, "timestamp": T0 + 240}, "walk"), "the step to a trip is still drawn"


def test_stop_points_never_fill_days_with_no_data():
    """The phone off for six weeks at home: one fix before, one after. The stop
    is not stamped every half hour through the weeks in between."""
    from mapmatch.portal.push import tidy_stops
    day = 86400
    run = [{"lat": LAT0, "lon": LON0, "timestamp": T0, "accuracy": 15, "tracker_id": "phone"},
           {"lat": LAT0 + 0.0003, "lon": LON0, "timestamp": T0 + 44 * day, "accuracy": 15, "tracker_id": "phone"}]
    pts = [p for c in tidy_stops(run) for p in c]
    assert sorted(p["timestamp"] for p in pts) == [T0, T0 + 44 * day]
    # Overnight at home with a fix now and then is still one stop, stamped through the night.
    night = [{**run[0], "timestamp": T0 + k * 4 * 3600} for k in range(4)]
    stamps = [p["timestamp"] for c in tidy_stops(night) for p in c]
    assert len(stamps) == 12 * 2 + 1 and stamps[-1] == T0 + 12 * 3600


def test_pushed_lines_have_no_long_straight_steps_but_jumps_stay_jumps():
    from mapmatch.engine.geo import haversine
    from mapmatch.portal.push import densify
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0, "kind": "matched"}
    b = {"lat": LAT0, "lon": LON0 + 0.02, "timestamp": T0 + 60, "kind": "matched"}   # ~1.3 km
    pts = densify([a, b])
    assert all(haversine(p["lat"], p["lon"], q["lat"], q["lon"]) <= 150 for p, q in zip(pts, pts[1:]))
    assert [p["timestamp"] for p in pts] == sorted({p["timestamp"] for p in pts})
    assert len(densify([a, {**b, "kind": "straight"}])) == 2, "an unrouted jump is not drawn in"


def test_one_second_logging_does_not_split_a_ride_at_every_light():
    # A bike computer logs every second: 4 min riding at 18 km/h, 2.5 min
    # waiting in a queue at 3 km/h, 4 min riding again.
    ride = [(5, 0)] * 240
    pts = track(ride + [(0.8, 0)] * 150 + ride, dt=1)
    labels, _, _ = label(pts)
    assert {m for m, _, _ in runs(labels)} == {"cycle"}


def test_slow_ride_beside_a_railway_is_not_a_train(monkeypatch):
    from mapmatch.engine import modes
    monkeypatch.setattr(modes, "near_rail", lambda fixes: [True] * len(fixes))
    pts = track([(5, 0)] * 300, dt=1)                # 18 km/h along the line
    labels, _, _ = label(pts)
    assert set(labels) == {"cycle"}
    fast = track([(1000, 0)] * 12)                     # 120 km/h along the line
    assert set(label(fast)[0]) == {"train"}


def test_closely_logged_ride_at_town_driving_speed_is_cycling():
    # A road bike logged every second: 20 min at 30-36 km/h, never past 40.
    ride = track([(8.5, 0), (9.5, 0), (10, 0), (8.5, 0)] * 300, dt=1)
    assert set(label(ride)[0]) == {"cycle"}
    # A car logged as closely gets past 45 km/h, so it stays a drive.
    car = track([(8.5, 0)] * 300 + [(16, 0)] * 300, dt=1)
    assert set(label(car)[0]) == {"drive"}
    # A phone logging every 30 s can miss every fast moment of a drive
    # through town, so sparse slow driving is left to the speed model.
    town = track([(280, 0)] * 40)                      # 34 km/h, every 30 s
    assert set(label(town)[0]) == {"drive"}


# ---------------------------------------------------------------- GPX tracks (no accuracy)
def _bodies(monkeypatch, fake):
    seen = []

    def post(path, body, timeout=120.0):
        seen.append(body)
        return fake(path, body, timeout)
    monkeypatch.setattr(valhalla, "post", post)
    return seen


def test_gpx_track_is_searched_narrowly_but_phone_track_is_not(monkeypatch):
    # Valhalla prefers a road among its candidates even when a cycle track is
    # a metre from the fix and the road ten: only a narrow search keeps a
    # precise GPX ride on the track.
    from conftest import FakeValhalla
    seen = _bodies(monkeypatch, FakeValhalla())
    valhalla.match(track([(5, 0)] * 20, dt=1, accuracy=None), "bicycle")
    body = seen[-1]
    assert {p.get("radius") for p in body["shape"]} == {valhalla.NO_ACCURACY_RADIUS}
    assert body["trace_options"]["gps_accuracy"] == valhalla.NARROW_GPS_ACCURACY
    valhalla.match(track([(5, 0)] * 20, dt=1, accuracy=None), "bicycle", wide=True)
    assert "radius" not in seen[-1]["shape"][0]
    valhalla.match(track([(50, 0)] * 20, accuracy=8.0), "bicycle")
    body = seen[-1]
    assert {p["radius"] for p in body["shape"]} == {30}
    assert body["trace_options"]["gps_accuracy"] == valhalla.PROFILES["bicycle"]["trace_options"]["gps_accuracy"]


def test_narrow_search_falls_back_to_wide_when_it_loses_the_trace(monkeypatch):
    from conftest import FakeValhalla
    from mapmatch.engine import match as M

    class Picky(FakeValhalla):
        def _trace(self, body):
            # The narrow search finds nothing for most of the ride.
            narrow = body["shape"][0].get("radius") == valhalla.NO_ACCURACY_RADIUS
            self.unmatched = set(range(2, 15)) if narrow else set()
            return super()._trace(body)
    seen = _bodies(monkeypatch, Picky())
    res = M._match_chunk(track([(5, 0)] * 20, dt=1, accuracy=None), "bicycle")
    assert len(seen) == 2 and all(res["anchors"])
    seen = _bodies(monkeypatch, FakeValhalla())
    M._match_chunk(track([(5, 0)] * 20, dt=1, accuracy=None), "bicycle")
    assert len(seen) == 1                                    # nothing lost: no second call


def test_gpx_out_and_back_along_a_cycle_track_is_kept(fake_valhalla):
    # Out 80 m along a spur and back: fixes with no accuracy on it are a real
    # detour, not a stray fix, so the line keeps it.
    steps = [(50, 0)] * 4 + [(0, 20)] * 4 + [(0, -20)] * 4 + [(50, 0)] * 4
    res = match_trip(track(steps, dt=10, accuracy=None))
    north = max(c[0] for s in res["segments"] for p in s["pieces"] for c in p["coords"])
    assert (north - LAT0) * 111_320 > 60


def test_detailed_track_is_kept_as_recorded(fake_valhalla):
    # A watch logging every second, weaving along a cycle track.
    steps = [(5, 0.6 if k % 20 < 10 else -0.6) for k in range(400)]
    pts = track(steps, dt=1, accuracy=None)
    out = match_trip(pts)
    kinds = {p["kind"] for s in out["segments"] for p in s["pieces"]}
    assert kinds == {"recorded"} and out["quality"]["as_recorded"] and out["quality"]["kept_because"] == "detailed"
    line = [c for s in out["segments"] for p in s["pieces"] for c in p["coords"]]
    assert len(line) == len(pts) and all(abs(c[0] - p["lat"]) < 1e-9 for c, p in zip(line, pts))
    assert out["quality"]["recorded_frac"] == 1.0 and out["quality"]["raw_frac"] == 0.0
    # The trip can still ask to be snapped.
    snapped = match_trip(pts, options={"snap_recorded": True})
    assert "recorded" not in {p["kind"] for s in snapped["segments"] for p in s["pieces"]}


def test_phone_track_and_holes_in_a_detailed_one():
    from mapmatch.engine.assemble import precise, recorded_pieces
    assert not precise(track([(150, 0)] * 40))                          # every 30 s
    assert not precise(track([(5, 12 if k % 2 else -12) for k in range(60)], dt=1))   # dense but jittery
    a = track([(5, 0)] * 60, dt=1)
    b = track([(5, 0)] * 60, start=(a[-1]["lat"], a[-1]["lon"] + 0.004), t0=a[-1]["timestamp"] + 40, dt=1)
    pcs = recorded_pieces(a + b, "cycle", "strict")
    assert [p["kind"] for p in pcs][0] == "recorded" and [p["kind"] for p in pcs][-1] == "recorded"
    assert any(p.get("gap") for p in pcs)                               # the tunnel is a gap to choose


def test_a_detailed_trip_is_tidied_where_it_stops_and_where_it_has_a_hole(fake_valhalla):
    """Kept point for point where it moves. The five minutes at a cafe are one dot, not a
    tangle, and the 200 m with no signal are routed or left open, not a straight line."""
    ride = [(5, 0.5 if k % 20 < 10 else -0.5) for k in range(300)]
    a = track(ride, dt=1, accuracy=None)
    stop = track([(0.3, -0.3), (-0.3, 0.3)] * 150, start=(a[-1]["lat"], a[-1]["lon"]),
                 t0=a[-1]["timestamp"] + 1, dt=1, accuracy=None)           # 5 min at a cafe
    b = track(ride, start=(stop[-1]["lat"], stop[-1]["lon"] + 0.003), t0=stop[-1]["timestamp"] + 45,
              dt=1, accuracy=None)                                       # signal lost under a bridge
    pts = a + stop + b
    out = match_trip(pts)
    assert not out["quality"].get("as_recorded"), "something was tidied, so there is something to push"
    stays = [s for s in out["segments"] if s["mode"] == "stay"]
    assert stays and all(len({(c[0], c[1]) for p in s["pieces"] for c in p["coords"]}) == 1 for s in stays), "a stop is one dot"
    rec = [p for s in out["segments"] for p in s["pieces"] if p["kind"] == "recorded"]
    assert sum(len(p["coords"]) for p in rec) >= 580, "both rides are kept point for point"
    from mapmatch.engine.geo import haversine
    assert all(haversine(a[0], a[1], b[0], b[1]) < 100 for p in rec for a, b in zip(p["coords"], p["coords"][1:])), \
        "the hole is not part of the recorded line"
    assert any(p.get("gap") for s in out["segments"] for p in s["pieces"]), "it is a gap, routed or left open"


def test_modes_not_matched_keep_their_gps(fake_valhalla):
    drive = track(DRIVE * 3)
    out = match_trip(drive, options={"match_modes": ["cycle"]})
    assert out["quality"]["as_recorded"] and out["quality"]["kept_because"] == "modes"
    assert not fake_valhalla.calls
    assert [(p["lat"], p["lon"]) for p in out["stream"]] == [(p["lat"], p["lon"]) for p in drive]
    # Set by hand, the same drive is matched after all.
    forced = match_trip(drive, overrides=[{"t0": drive[0]["timestamp"], "t1": drive[-1]["timestamp"], "mode": "drive"}],
                        options={"match_modes": ["cycle"]})
    assert not forced["quality"].get("as_recorded") and "matched" in {p["kind"] for s in forced["segments"] for p in s["pieces"]}
    # Everything matched by default.
    assert not match_trip(drive)["quality"].get("as_recorded")


def test_only_the_unmatched_mode_keeps_its_gps_in_a_mixed_trip(fake_valhalla):
    walk = track(WALK * 2)
    drive = track(DRIVE * 3, start=(walk[-1]["lat"], walk[-1]["lon"]), t0=walk[-1]["timestamp"] + 30)
    out = match_trip(walk + drive, options={"match_modes": ["drive"]})
    by_mode = {s["mode"]: {p["kind"] for p in s["pieces"]} for s in out["segments"]}
    assert "recorded" in by_mode.get("walk", set()) and "matched" in by_mode.get("drive", set()), by_mode


def test_one_gap_can_be_routed_in_a_trip_that_keeps_its_gaps(fake_valhalla):
    fake_valhalla.unmatched = {10, 25}
    pts = track([(500, 0)] * 40)
    kept = match_trip(pts, options={"strength": "strict"})
    holes = [p for s in kept["segments"] for p in s["pieces"] if p.get("gap")]
    assert len(holes) == 2 and {p["kind"] for p in holes} <= {"raw", "straight"}, holes
    first = holes[0]["gap"]
    chosen = match_trip(pts, options={"strength": "strict", "route_gaps": [first]})
    kinds = {tuple(p["gap"]): p["kind"] for s in chosen["segments"] for p in s["pieces"] if p.get("gap")}
    assert kinds[tuple(first)] == "filled" and len(kinds) == 2
    assert all(k in ("raw", "straight") for g, k in kinds.items() if g != tuple(first))


def test_each_gap_stays_its_own_piece(fake_valhalla):
    from mapmatch.engine.assemble import merge_pieces
    a = {"kind": "raw", "coords": [[0, 0, 0, 1], [0, 1, 1, 1]], "n_in": 2, "km": 0.1}
    g = {"kind": "raw", "coords": [[0, 1, 1, 1], [0, 2, 9, 1]], "n_in": 0, "km": 0.1, "gap": [1, 9]}
    out = merge_pieces([a, g, dict(a)])
    assert len(out) == 3 and out[1]["gap"] == [1, 9]


def test_watch_stretch_inside_a_phone_trip_is_kept_and_the_rest_matched(fake_valhalla):
    from mapmatch.engine.assemble import dense_blocks, precise
    phone = track([(250, 0)] * 30, dt=30)                                   # 30 km/h, every 30 s
    watch = track([(5, 0.5 if k % 20 < 10 else -0.5) for k in range(400)], start=(phone[-1]["lat"], phone[-1]["lon"]),
                  t0=phone[-1]["timestamp"] + 1, dt=1, accuracy=None)
    pts = phone + watch
    assert not precise(pts), "mostly logged by the phone, so not a detailed trip"
    out = match_trip(pts)
    assert not out["quality"].get("as_recorded")
    kinds = {p["kind"] for s in out["segments"] for p in s["pieces"]}
    assert "matched" in kinds and "recorded" in kinds
    line = {(c[0], c[1]) for s in out["segments"] for p in s["pieces"] for c in p["coords"]}
    assert all((p["lat"], p["lon"]) in line for p, d in zip(clean(pts)[0], dense_blocks(clean(pts)[0])) if d)


def test_closely_logged_walk_at_riding_speed_is_a_ride():
    # 2.5 m/s (9 km/h) for half the time, stopped for the rest: a slow ride with stops.
    steps = [(2.5, 0)] * 60 + [(0.05, 0)] * 60
    pts = track(steps * 4, dt=1, accuracy=None)
    labels, _, _ = label(pts)
    assert "walk" not in labels and "cycle" in labels
    brisk = track([(1.6, 0)] * 400, dt=1, accuracy=None)                    # 5.8 km/h
    assert set(label(brisk)[0]) == {"walk"}


def test_trip_where_the_server_has_no_map_is_kept_as_recorded(fake_valhalla):
    fake_valhalla.no_map = True
    pts = track(DRIVE * 3)
    out = match_trip(pts)
    assert out["quality"]["as_recorded"] and out["quality"]["kept_because"] == "no_map"
    assert out["quality"]["no_map_frac"] == 1.0
    assert "/trace_attributes" not in fake_valhalla.calls


def test_train_on_an_openstreetmap_railway_outside_gb(fake_valhalla, tmp_path, monkeypatch):
    import gzip
    import json
    from mapmatch.engine import railnet
    # A railway running east with a junction halfway, far from any GB line.
    line = [[LAT0, LON0 + k * 0.005] for k in range(60)]
    branch = [[LAT0 + k * 0.004, LON0 + 0.15] for k in range(1, 20)]
    with gzip.open(tmp_path / "railways-test.json.gz", "wt") as f:
        json.dump({"name": "test", "nodes": line + branch,
                   "ways": [list(range(30)), list(range(29, 60)), [30] + list(range(60, 79))], "stations": []}, f)
    monkeypatch.setattr(railnet, "_dirs", [str(tmp_path)])
    monkeypatch.setattr(railnet, "_nets", None)
    pts = track([(830, 0)] * 24)                    # 100 km/h along the line, a fix every 30 s
    out = match_trip(pts)
    trains = [s for s in out["segments"] if s["mode"] == "train"]
    assert trains and {p["kind"] for s in trains for p in s["pieces"]} >= {"matched"}
    assert out["quality"]["p95"] < 15
    monkeypatch.setattr(railnet, "_nets", None)


def test_detail_is_judged_per_watch_block_not_per_mode():
    from mapmatch.engine.assemble import detailed_fixes, precise
    ride = track([(5, 0.5 if k % 20 < 10 else -0.5) for k in range(400)], dt=1, accuracy=None)
    phone = track([(250, 0)] * 20, start=(ride[-1]["lat"], ride[-1]["lon"]), t0=ride[-1]["timestamp"] + 30, dt=30)
    d = detailed_fixes(ride + phone[1:])
    assert all(d[:400]) and not any(d[401:])
    assert not precise(ride[100:121]), "too short to judge on its own"
    assert all(d[100:121]), "but it sits inside a detailed block"


def test_area_between_gps_and_drawn_line():
    """A drawn line that leaves the straight hop between two fixes encloses
    area; one that runs along the fixes encloses none."""
    from mapmatch.engine.quality import between
    m = 1 / 110540.0                                   # one metre of latitude
    k = 1 / (111320.0 * 0.6225)                        # one metre of longitude at 51.5 N
    a = {"lat": 51.5, "lon": -1.0, "timestamp": T0}
    b = {"lat": 51.5, "lon": -1.0 + 1000 * k, "timestamp": T0 + 100}
    along = [[51.5, -1.0, T0, 1], [51.5, -1.0 + 1000 * k, T0 + 100, 1]]
    box = [[51.5, -1.0, T0, 1], [51.5 + 200 * m, -1.0, T0 + 20, 1],
           [51.5 + 200 * m, -1.0 + 1000 * k, T0 + 80, 1], [51.5, -1.0 + 1000 * k, T0 + 100, 1]]
    seg = lambda coords, kind="matched": [{"mode": "drive", "pieces": [{"kind": kind, "coords": coords}]}]
    assert between([a, b], seg(along)).get("area_m2", 0) < 50
    q = between([a, b], seg(box))
    assert 190_000 < q["area_m2"] < 210_000            # a 1000 m x 200 m rectangle
    assert 130 < q["gap_m"] < 150 and q["bulge_max_m"] in range(195, 206)
    assert (q["bulge_t0"], q["bulge_t1"]) == (T0, T0 + 100)
    assert q["detour_line_m"] > q["detour_hop_m"] + 350
    assert between([a, b], seg(box, "recorded")) == {}, "the GPS itself encloses nothing"


def _walk(n, lat0=51.45, lon0=-2.6, dt=25, step=35.0):
    """n fixes walking east at about 5 km/h."""
    k = 1 / (111320.0 * 0.623)
    return [{"lat": lat0, "lon": lon0 + i * step * k, "timestamp": T0 + i * dt, "accuracy": 15.0} for i in range(n)]


def test_one_fix_that_jumps_off_a_walk_and_back_is_dropped():
    pts = _walk(16)
    pts[8] = {**pts[8], "lat": pts[8]["lat"] + 95 / 110540.0}          # 95 m sideways, back at the next fix
    kept, dropped = clean(pts, jitter=True)
    assert [p["timestamp"] for p in dropped if p["_drop"] == "spike"] == [pts[8]["timestamp"]]
    kept, dropped = clean(_walk(16), jitter=True)
    assert not dropped, "an ordinary walk loses nothing"
    kept, dropped = clean(pts)                                         # trip splitting sees every fix
    assert not dropped


def test_phone_hopping_to_and_fro_while_parked_is_dropped_but_a_turn_round_is_not():
    m = 1 / 110540.0
    arrive = _walk(12)                                                  # arrives on foot, phone then sits still
    home = arrive[-1]
    t = home["timestamp"]
    hops = []
    for i, off in enumerate([350, 0, 420, 0, -380, 0, 300, 0, 450, 0]):  # every 22 s: away, back, away, back
        hops.append({"lat": home["lat"] + off * m, "lon": home["lon"], "timestamp": t + 22 * (i + 1), "accuracy": 20.0})
    leave = [{**p, "lon": home["lon"] + (p["lon"] - arrive[0]["lon"]), "timestamp": hops[-1]["timestamp"] + 25 * (i + 1)}
             for i, p in enumerate(_walk(8))]
    kept, dropped = clean(arrive + hops + leave, jitter=True)
    assert sum(1 for p in dropped if p["_drop"] == "jitter") == 5, "the five hops away go, the fixes at the true spot stay"
    # A drive up a road, a turn, and back down it: three fast reversals at most, and it never comes back to one spot.
    drive = [{"lat": 51.45 + y * m, "lon": -2.6, "timestamp": T0 + 30 * i, "accuracy": 10.0}
             for i, y in enumerate([0, 400, 800, 1200, 1050, 1180, 800, 400, 0, -400, -800])]
    kept, dropped = clean(drive, jitter=True)
    assert not [p for p in dropped if p["_drop"] == "jitter"]


def test_sparse_fixes_cannot_be_slower_than_the_ground_they_cover():
    """A fix a minute, a kilometre apart: the phone's own reading taken at a
    junction (2 m/s) must not make a drive look like a bike ride."""
    drive = track([(1000, 0)] * 8, dt=60, velocity=2.0)
    assert min(speeds(drive)) > 50
    labels, _, _ = label(drive)
    assert set(labels) == {"drive"}
    # Closely logged, the reading is trusted: positions a few metres apart are noise.
    ride = track([(5, 0)] * 30, dt=1, velocity=5.0)
    assert 17 < speeds(ride)[10] < 19


def test_a_ride_does_not_turn_into_a_drive_without_a_stop():
    """The car slowing into town is still the car; a ride after parking is a ride."""
    fast = track([(900, 0)] * 10, dt=40)                                    # 81 km/h
    slow = track([(160, 0)] * 14, start=(fast[-1]["lat"], fast[-1]["lon"]), t0=fast[-1]["timestamp"] + 40, dt=40)   # 14 km/h
    labels, _, _ = label(fast + slow)
    assert set(labels) == {"drive"}, "no stop between them: one vehicle"
    parked = track([(2, 0), (-2, 0)] * 4, start=(fast[-1]["lat"], fast[-1]["lon"]), t0=fast[-1]["timestamp"] + 40, dt=40)
    ride = track([(160, 0)] * 40, start=(parked[-1]["lat"], parked[-1]["lon"]), t0=parked[-1]["timestamp"] + 40, dt=40)   # 27 min at 14 km/h
    labels, _, _ = label(fast + parked + ride)
    assert labels[-1] == "cycle" and "stay" in labels and labels[0] == "drive"
    # A short quick hop on from the car park is the car again.
    hop = track([(300, 0)] * 8, start=(parked[-1]["lat"], parked[-1]["lon"]), t0=parked[-1]["timestamp"] + 40, dt=40)   # 27 km/h, 5 min
    labels, _, _ = label(fast + parked + hop)
    assert labels[-1] == "drive"


def test_a_routed_gap_that_goes_round_the_block_is_drawn_straight(fake_valhalla):
    """Two fixes 120 m apart with nothing between them: a route four times
    that length is an anchor on the wrong side of the road, not a journey.
    (A route too long for the time it took is a different thing, and stays a
    straight line that is shown as a doubt.)"""
    a = {"lat": LAT0, "lon": LON0, "timestamp": T0}
    lat, lon = offset(LAT0, LON0, 120, 0)
    b = {"lat": lat, "lon": lon, "timestamp": T0 + 40}
    fake_valhalla.route_detour = 3.0          # about 500 m for the 120 m hop, at a speed a car can do
    out = fill(a, [], b, "drive")
    assert out["kind"] == "join" and "way_round" in out["why"] and len(out["coords"]) == 2
    fake_valhalla.route_detour = 1.2
    assert fill(a, [], b, "drive")["kind"] == "filled", "an ordinary route is kept"


def test_gps_kept_point_for_point_is_not_a_doubt():
    from mapmatch.engine.api import _unsure_km
    dense = [[LAT0, LON0 + k * 0.0005, T0 + 30 * k, 1] for k in range(20)]          # 33 m steps
    sparse = [[LAT0, LON0 + k * 0.01, T0 + 300 * k, 1] for k in range(3)]           # 650 m steps
    seg = lambda kind, coords, km: [{"mode": "walk", "pieces": [{"kind": kind, "coords": coords, "km": km}]}]
    assert _unsure_km(seg("raw", dense, 0.62)) == 0
    assert _unsure_km(seg("raw", sparse, 1.3)) > 1.2
    assert _unsure_km(seg("straight", sparse[:2], 0.65)) == 0.65


def test_a_routed_gap_keeps_the_times_of_the_fixes_along_it():
    """About 900 m in the first minute, 100 m in the second: the line passes
    the middle fix at its own time, not where an even speed would put it."""
    from mapmatch.engine.assemble import timed
    from mapmatch.engine.geo import haversine
    # a road with bends in it, so that no vertex is simplified away
    line = [offset(LAT0, LON0, m, 40 if (m // 100) % 2 else 0) for m in range(0, 1001, 100)]
    mid = {"lat": line[9][0], "lon": line[9][1], "timestamp": T0 + 60}
    even = timed(line, T0, T0 + 120)
    pinned = timed(line, T0, T0 + 120, [mid])
    at = lambda out: min(out, key=lambda c: haversine(c[0], c[1], mid["lat"], mid["lon"]))[2]
    assert T0 + 100 < at(even) < T0 + 115, "an even speed reaches it late"
    assert abs(at(pinned) - (T0 + 60)) < 1.5
    assert pinned[0][2] == T0 and pinned[-1][2] == T0 + 120
    assert [c[2] for c in pinned] == sorted(c[2] for c in pinned)
    far = {"lat": LAT0 + 0.01, "lon": line[5][1], "timestamp": T0 + 10}                # over a kilometre off the line
    assert [c[2] for c in timed(line, T0, T0 + 120, [far])] == [c[2] for c in even]


def _two_track_network(tmp_path):
    """A double-track main line running east for 6 km, the tracks 4 m apart and joined only by a
    crossover at each end: the shape of a real railway in OpenStreetMap."""
    import gzip
    import json
    nodes, ways = [], []
    for row, north in enumerate((0.0, 4.0)):
        ids = []
        for k in range(61):                                   # a node every 100 m
            lat, lon = offset(LAT0, LON0, 100.0 * k, north)
            ids.append(len(nodes))
            nodes.append([lat, lon])
        ways.append(ids)
    ways.append([0, 61])                                      # crossovers at the two ends
    ways.append([60, 121])
    d = tmp_path / "rail"
    d.mkdir()
    with gzip.open(d / "railways-test.json.gz", "wt") as f:
        json.dump({"name": "test", "nodes": nodes, "ways": ways, "stations": [nodes[5], nodes[55]]}, f)
    return d


def test_a_train_keeps_to_one_track(tmp_path, monkeypatch):
    """Fixes fall on either track at random. Snapped one by one the line runs to a crossover and
    back each time they change; followed as a journey it is one track, as long as the ground covered."""
    from mapmatch.engine import railnet
    monkeypatch.setattr(railnet, "_dirs", [str(_two_track_network(tmp_path))])
    monkeypatch.setattr(railnet, "_nets", None)
    fixes = []
    for k in range(11):                                       # every 500 m, alternately nearer each track
        lat, lon = offset(LAT0, LON0, 500.0 * k + 250.0, 3.5 if k % 2 else 0.5)
        fixes.append({"lat": lat, "lon": lon, "timestamp": T0 + 30 * k, "accuracy": 10.0})
    res = railnet.match(fixes)
    assert all(res["anchors"]) and len(res["pieces"]) == 1
    assert 4900 < res["pieces"][0]["cum"][-1] < 5200, "5 km of ground, 5 km of rails: no trips to the crossovers"
    assert max(a["dist"] for a in res["anchors"]) < 5
    path = railnet.between(fixes[0]["lat"], fixes[0]["lon"], fixes[-1]["lat"], fixes[-1]["lon"])
    assert path and len(path) > 40


def test_a_poor_fix_a_walker_would_have_to_run_to_is_dropped():
    pts = [{**p, "accuracy": 12.0} for p in _walk(16)]
    pts[8] = {**pts[8], "lat": pts[8]["lat"] + 110 / 110540.0, "accuracy": 38.0}     # 110 m out, and the phone says it is unsure
    kept, dropped = clean(pts, jitter=True)
    assert [p["timestamp"] for p in dropped] == [pts[8]["timestamp"]]
    sure = [{**p, "accuracy": 8.0} for p in pts]                                     # the same shape from a fix it trusts
    sure[8]["lat"] = pts[8]["lat"] - 110 / 110540.0 + 45 / 110540.0                  # 45 m: too small for the side-step rule
    kept, dropped = clean(sure, jitter=True)
    assert not dropped, "a good fix off the line is where the walker was"


def test_a_hole_nothing_explains_is_left_unconnected(fake_valhalla):
    from mapmatch.engine import assemble
    lat, lon = offset(LAT0, LON0, 3000, 0)
    hop = [[LAT0, LON0, T0, 1], [lat, lon, T0 + 600, 1]]                             # 3 km, ten minutes, nothing between
    near = [[LAT0, LON0, T0, 1], [offset(LAT0, LON0, 150, 0)[0], offset(LAT0, LON0, 150, 0)[1], T0 + 60, 1]]
    segs = [{"mode": "walk", "pieces": [{"kind": "straight", "coords": hop, "n_in": 0, "km": 3.0},
                                        {"kind": "straight", "coords": near, "n_in": 0, "km": 0.15}]}]
    assemble.unlink(segs)
    assert segs[0]["pieces"][0].get("unlinked") is True and not segs[0]["pieces"][1].get("unlinked")
    # Kept GPS is cut at such a hole; with no route to be had the two halves are not joined.
    fake_valhalla.fail_route = True
    track_ = [[LAT0, LON0 + k * 0.0003, T0 + 20 * k, 1] for k in range(5)]
    far = [[LAT0 + 0.03, LON0 + k * 0.0003, T0 + 1000 + 20 * k, 1] for k in range(5)]
    segs = [{"mode": "walk", "pieces": [{"kind": "recorded", "coords": track_ + far, "n_in": 10, "km": 3.5}]}]
    assemble.unlink(segs)
    kinds = [(p["kind"], bool(p.get("unlinked"))) for p in segs[0]["pieces"]]
    assert kinds == [("recorded", False), ("straight", True), ("recorded", False)]
    # With a route, the hole is the route: here a ride, since nobody walks 3.3 km in 15 minutes.
    fake_valhalla.fail_route = False
    segs = [{"mode": "walk", "pieces": [{"kind": "recorded", "coords": track_ + far, "n_in": 10, "km": 3.5}]}]
    assemble.unlink(segs)
    mid = segs[0]["pieces"][1]
    assert mid["kind"] == "filled" and mid.get("via") == "cycle" and not mid.get("unlinked")


def test_kept_gps_is_not_drawn_through_fixes_the_phone_was_unsure_of(fake_valhalla):
    from mapmatch.engine.assemble import _filled
    a, m1, m2, b = track([(40, 0), (40, 0), (40, 0)])
    m1 = {**m1, "lat": m1["lat"] + 90 / 111_320.0, "accuracy": 39.0}                 # scatter: 90 m off, rated poor
    fake_valhalla.fail_route = True
    pc = _filled(a, [m1, m2], b, "walk", "fill", 2)
    assert pc["kind"] == "raw" and len(pc["coords"]) == 3, "the poor fix is left out, the good one kept"


def test_kept_gps_loses_the_fixes_nobody_could_have_walked_to():
    from mapmatch.engine.assemble import calm
    def at(east, north, dt):
        lat, lon = offset(LAT0, LON0, east, north)
        return {"lat": lat, "lon": lon, "timestamp": T0 + dt, "accuracy": 12.0}
    a, b = at(0, 0, 0), at(200, 0, 150)                       # 200 m in 150 s: a walk
    zig = [at(66, 80, 50), at(133, -80, 100)]                 # ...that the fixes make 380 m of
    assert len(calm(a, zig, b, 5.0)) < 2
    straight = [at(66, 3, 50), at(133, -3, 100)]
    assert calm(a, straight, b, 5.0) == straight, "a walk the right length keeps every fix"


def test_the_tangle_where_a_trip_ends_indoors_is_one_position():
    def at(east, north, k):
        lat, lon = offset(LAT0, LON0, east, north)
        return {"lat": lat, "lon": lon, "timestamp": T0 + 30 * k, "accuracy": 14.0}
    walk = [at(40 * k, 0, k) for k in range(20)]              # 800 m to the shop
    star = [at(760 + e, n, 20 + k) for k, (e, n) in enumerate([(45, 40), (-30, -35), (50, -20), (-40, 30), (20, 55), (-15, -50), (35, 10), (0, 0)])]
    kept, dropped = clean(walk + star, jitter=True)
    assert len(dropped) >= 6 and all(p["timestamp"] >= walk[17]["timestamp"] for p in dropped), "only the last 90 m"
    assert len([p for p in kept if p["timestamp"] <= walk[16]["timestamp"]]) == 17, "the walk there is untouched"
    assert len(kept) <= 20


def test_a_ferry_line_is_not_a_path_for_a_walk():
    """The map-matcher's answer runs road, ferry, road. The ferry edge is left out: the
    fix on it has no anchor and the line is two pieces, one each side of the water."""
    from mapmatch.engine import valhalla
    pts = [offset(LAT0, LON0, 100.0 * k, 0) for k in range(4)]
    raw = {"shape": "x",
           "edges": [{"begin_shape_index": 0, "end_shape_index": 1, "use": "footway"},
                     {"begin_shape_index": 1, "end_shape_index": 2, "use": "ferry"},
                     {"begin_shape_index": 2, "end_shape_index": 3, "use": "footway"}],
           "matched_points": [{"type": "matched", "edge_index": k, "lat": pts[k][0], "lon": pts[k][1],
                               "distance_along_edge": 0.5, "distance_from_trace_point": 3.0} for k in range(3)]}
    real, real_decode = valhalla.post, valhalla.decode_polyline
    valhalla.post = lambda path, body, timeout=120.0: raw
    valhalla.decode_polyline = lambda s, precision=6: list(pts)
    try:
        fixes = [{"lat": p[0], "lon": p[1], "timestamp": T0 + 30 * k, "accuracy": 10.0} for k, p in enumerate(pts[:3])]
        m = valhalla.match(fixes, "pedestrian")
        assert len(m["pieces"]) == 2 and m["anchors"][1] is None and m["anchors"][0] and m["anchors"][2]
        assert len(valhalla.match(fixes, "auto")["pieces"]) == 1, "a car ferry is a road for a car"
    finally:
        valhalla.post, valhalla.decode_polyline = real, real_decode
