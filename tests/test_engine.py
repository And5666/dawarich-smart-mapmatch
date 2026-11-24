"""Engine unit tests. No network: Valhalla is faked in conftest.py."""
from __future__ import annotations

from conftest import T0, encode_polyline, track

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
