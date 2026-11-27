"""The last check before Dawarich is changed (mapmatch.portal.guard)."""
from __future__ import annotations

from conftest import LAT0, LON0, T0

from mapmatch.portal.guard import check

HOUR = 3600


def fix(ts, lat=LAT0, lon=LON0):
    return {"lat": lat, "lon": lon, "timestamp": ts, "accuracy": 10.0}


def ours(src, ts, lat=LAT0, lon=LON0):
    return {"lat": lat, "lon": lon, "timestamp": ts, "ours": True, "src": src}


FIXES = [fix(T0), fix(T0 + 4 * HOUR), fix(T0 + 5 * HOUR, lon=LON0 + 0.01), fix(T0 + 60 * HOUR)]
LINE = {1: (T0 + 4 * HOUR, T0 + 5 * HOUR, [[LAT0, LON0, T0 + 4 * HOUR, 1], [LAT0, LON0 + 0.01, T0 + 5 * HOUR, 1]])}
SPAN = (T0, T0 + 60 * HOUR)


def test_what_the_portal_shows_and_the_gps_backs_is_written():
    posts = [{**FIXES[0], "ours": False}, ours("stop", T0 + 2 * HOUR),
             ours("match", T0 + 4 * HOUR), ours("match", T0 + 4 * HOUR + 1800, lon=LON0 + 0.005),
             ours("link", T0 + 4 * HOUR - 300, lon=LON0 + 0.0005), ours("stop", T0 + 4 * HOUR - 600)]
    assert check(posts, FIXES, LINE, SPAN) == []


def test_stop_points_where_nothing_was_recorded_are_refused():
    assert "nothing recorded" in check([ours("stop", T0 + 30 * HOUR, lon=LON0 + 0.01)], FIXES, LINE, SPAN)[0]
    assert "away from where" in check([ours("stop", T0 + HOUR, lat=LAT0 + 0.02)], FIXES, LINE, SPAN)[0]


def test_matched_points_off_the_drawn_line_are_refused():
    assert "off the line" in check([ours("match", T0 + 4 * HOUR + 1800, lat=LAT0 + 0.002, lon=LON0 + 0.005)],
                                   FIXES, LINE, SPAN)[0]
    assert "outside every pushed trip" in check([ours("match", T0 + HOUR)], FIXES, LINE, SPAN)[0]


def test_routes_guessed_across_unlogged_time_are_refused():
    posts = [ours("stop", T0), ours("link", T0 + 1800, lon=LON0 + 0.002), ours("stop", T0 + HOUR, lon=LON0 + 0.004)]
    assert "with nothing recorded" in check(posts, FIXES, LINE, SPAN)[0]
    far = [ours("stop", T0), ours("link", T0 + 300, lon=LON0 + 0.02), {**fix(T0 + 600, lon=LON0 + 0.04), "ours": False}]
    assert any("too far apart" in w for w in check(far, FIXES + [fix(T0 + 600, lon=LON0 + 0.04)], LINE, SPAN))


def test_anything_else_is_refused():
    assert "never recorded" in check([{**fix(T0 + 10), "ours": False}], FIXES, LINE, SPAN)[0]
    assert "outside the span" in check([ours("stop", T0 + 61 * HOUR)], FIXES, LINE, SPAN)[0]
    assert "no source" in check([{**fix(T0), "ours": True}], FIXES, LINE, SPAN)[0]
