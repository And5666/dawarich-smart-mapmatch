"""Assemble one line per track from the inferred state sequence."""
from __future__ import annotations

import math
from typing import Any

from .geo import cumulative, dist_point_segment, haversine, point_at, subline

SPACING = {"auto": 25.0, "bicycle": 20.0, "pedestrian": 15.0, "rail": 40.0}
LABEL = {"auto": "driving", "bicycle": "cycling", "pedestrian": "walking", "rail": "train"}


def _raw_point(p: dict[str, Any]) -> dict[str, Any]:
    return {"lat": p["lat"], "lon": p["lon"], "timestamp": int(p["timestamp"]),
            "accuracy": p.get("accuracy"), "velocity": p.get("velocity"),
            "altitude": p.get("altitude")}


def _candidate_point(c: dict[str, Any], shape: Any | None) -> tuple[float, float, float]:
    if shape:
        poly, cum = shape
        if c.get("arc") is not None:
            return point_at(poly, cum, c["arc"])
    return c["lat"], c["lon"], 0.0


def _append_curve(out: list[dict[str, Any]], poly: list[tuple[float, float]],
                  t0: int, t1: int, spacing: float, accuracy: Any) -> None:
    if not out or len(poly) < 2 or t1 <= t0:
        if poly:
            lat, lon, h = point_at(poly, cumulative(poly), cumulative(poly)[-1])
            out.append({"lat": lat, "lon": lon, "timestamp": int(t1),
                        "accuracy": accuracy, "inserted": True, "course": h})
        return
    cum = cumulative(poly)
    total = cum[-1]
    if total <= 1.0:
        lat, lon, h = point_at(poly, cum, total)
        if int(t1) > out[-1]["timestamp"]:
            out.append({"lat": lat, "lon": lon, "timestamp": int(t1),
                        "accuracy": accuracy, "inserted": True, "course": h})
        return
    # Add geometry vertices and spacing inserts, but never more than one point
    # per second. That keeps the output line detailed without duplicate times.
    dt = int(t1) - int(t0)
    targets: list[tuple[float, int]] = []
    if dt >= 2:
        n = min(4000, max(0, int(total // spacing)))
        for k in range(1, n):
            targets.append((total * k / n, int(round(t0 + dt * (total * k / n) / total))))
    # Include shape vertices that fall between the two ends, at interpolated times.
    for i in range(1, len(poly) - 1):
        arc = cum[i]
        if arc <= 1.0 or arc >= total - 1.0:
            continue
        ts = int(round(t0 + dt * (arc / total))) if dt > 0 else int(t1)
        targets.append((arc, ts))
    targets.sort()
    last_ts = int(out[-1]["timestamp"])
    for arc, ts in targets:
        if ts <= last_ts:
            continue
        if ts >= int(t1):
            break
        lat, lon, h = point_at(poly, cum, arc)
        out.append({"lat": lat, "lon": lon, "timestamp": int(ts),
                    "accuracy": accuracy, "inserted": True, "course": h})
        last_ts = ts
    lat, lon, h = point_at(poly, cum, total)
    out.append({"lat": lat, "lon": lon, "timestamp": int(t1),
                "accuracy": accuracy, "inserted": False, "course": h})


def _append_raw(out: list[dict[str, Any]], p: dict[str, Any]) -> None:
    q = _raw_point(p)
    if out and out[-1]["timestamp"] == q["timestamp"] and \
            abs(out[-1]["lat"] - q["lat"]) < 1e-9 and abs(out[-1]["lon"] - q["lon"]) < 1e-9:
        return
    out.append(q)


def _matched_run(fixes: list[dict[str, Any]], mode: str,
                 cands: list[dict[str, Any] | None],
                 shapes: dict[tuple[str, int], Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    prev = None
    for p, c in zip(fixes, cands):
        if c is None:
            _append_raw(out, p)
            prev = None
            continue
        shape = shapes.get((mode, c.get("chunk"))) if mode != "rail" else None
        if mode == "rail" and c.get("shape") is not None:
            # rail uses its own shape id; imported lazily to keep this module small
            from . import rail as rail_mod
            poly = rail_mod.shape_line(c["shape"])
            shape = (poly, rail_mod.shape_cum(c["shape"]))
        if not out:
            lat, lon, h = _candidate_point(c, shape)
            out.append({"lat": lat, "lon": lon, "timestamp": int(p["timestamp"]),
                        "accuracy": p.get("accuracy"), "inserted": False, "course": h})
            prev = (p, c, shape)
            continue
        prev_key = (prev[1].get("chunk"), prev[1].get("shape")) if prev else None
        cur_key = (c.get("chunk"), c.get("shape"))
        if prev and prev[2] and shape and prev_key == cur_key:
            poly, cum = shape
            a = min(prev[1].get("arc") or 0.0, c.get("arc") or 0.0)
            b = max(prev[1].get("arc") or 0.0, c.get("arc") or 0.0)
            if b - a < 1.0:
                _append_raw(out, p)
            else:
                seg = subline(poly, cum, a, b)
                # subline() always walks forward along the shape. If the fix
                # order is backward (a train or a loop), reverse it, otherwise
                # the line goes out and back over the same stretch.
                if (c.get("arc") or 0.0) < (prev[1].get("arc") or 0.0):
                    seg.reverse()
                if len(seg) >= 2:
                    _append_curve(out, seg, int(prev[0]["timestamp"]), int(p["timestamp"]),
                                  SPACING.get(mode, 25.0), p.get("accuracy"))
                else:
                    _append_raw(out, p)
        else:
            lat, lon, h = _candidate_point(c, shape)
            out.append({"lat": lat, "lon": lon, "timestamp": int(p["timestamp"]),
                        "accuracy": p.get("accuracy"), "inserted": False, "course": h})
        prev = (p, c, shape)
    return out


def build_track(track_id: int, matched: dict[str, Any]) -> dict[str, Any]:
    fixes = matched["fixes"]
    sections = matched["sections"]
    state_by = ["raw"] * len(fixes)
    cand_by: list[dict[str, Any] | None] = [None] * len(fixes)
    for sec in sections:
        for k, idx in enumerate(range(sec["start"], sec["end"])):
            state_by[idx] = sec["states"][k]
            cand_by[idx] = sec["candidates"][k]

    runs: list[dict[str, Any]] = []
    i = 0
    while i < len(fixes):
        j = i + 1
        while j < len(fixes) and state_by[j] == state_by[i]:
            j += 1
        sec = next((s for s in sections if s["start"] <= i < s["end"]), None)
        shapes = (sec.get("match") or {}).get("shapes", {}) if sec else {}
        mode = state_by[i]
        if mode in ("raw", "stationary", "gap", "spike", "raw_stop"):
            pts = [_raw_point(p) for p in fixes[i:j]]
            engine = "raw"
            label_mode = "stationary" if mode == "stationary" else "raw"
        else:
            pts = _matched_run(fixes[i:j], mode, cand_by[i:j], shapes)
            engine = "rail" if mode == "rail" else f"matched:{mode}"
            label_mode = LABEL.get(mode, mode)
            if mode == "rail":
                raw_run = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                              for a, b in zip(fixes[i:j], fixes[i + 1:j]))
                gen_run = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                              for a, b in zip(pts, pts[1:]))
                # A route pattern can be a loop or a different service. If it
                # does not resemble the raw train distance, keep the record.
                if gen_run > 1.35 * raw_run + 150 or gen_run < 0.65 * raw_run - 150:
                    pts = [_raw_point(p) for p in fixes[i:j]]
                    engine = "raw"
                    label_mode = "raw"
        runs.append({"start": i, "end": j, "mode": label_mode, "engine": engine,
                     "points": pts, "n_in": j - i, "start_ts": int(fixes[i]["timestamp"]),
                     "end_ts": int(fixes[j - 1]["timestamp"])})
        i = j

    out: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for k, r in enumerate(runs):
        pts = r["points"]
        # Remove a point that exactly repeats the previous run's endpoint.
        if out and pts and out[-1]["timestamp"] == pts[0]["timestamp"] and \
                abs(out[-1]["lat"] - pts[0]["lat"]) < 1e-9 and \
                abs(out[-1]["lon"] - pts[0]["lon"]) < 1e-9:
            pts = pts[1:]
        if not pts:
            continue
        out.extend(pts)
        if len(pts) < 2:
            continue
        parts.append({
            "i": len(parts), "start": r["start_ts"], "end": r["end_ts"],
            "mode": r["mode"], "engine": r["engine"],
            "label": f"{r['mode']} · {r['engine']}",
            "n_in": r["n_in"], "n_out": len(pts),
            "distance": round(sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                                  for a, b in zip(pts, pts[1:])) / 1000.0, 3),
            "points": [[p["lat"], p["lon"]] for p in pts],
        })

    # Stable sort by time. Inserted vertices and raw fixes are already ordered.
    out.sort(key=lambda p: p["timestamp"])

    # One-second timestamps cannot express 50 m at 120 km/h without looking
    # like a 180 km/h spike. Never let a synthetic vertex create one: keep raw
    # fixes (they are the record) but drop inserted points that exceed the
    # threshold.
    clean: list[dict[str, Any]] = []
    for p in out:
        if clean:
            q = clean[-1]
            dt = max(1, int(p["timestamp"]) - int(q["timestamp"]))
            d = haversine(q["lat"], q["lon"], p["lat"], p["lon"])
            if d / dt * 3.6 > 180.0 and p.get("inserted"):
                continue
            if d / dt * 3.6 > 180.0 and q.get("inserted"):
                clean[-1] = p
                continue
        clean.append(p)
    out = clean
    raw_pts = [_raw_point(p) for p in fixes]
    raw_km = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                 for a, b in zip(raw_pts, raw_pts[1:])) / 1000.0
    gen_km = sum(haversine(a["lat"], a["lon"], b["lat"], b["lon"])
                 for a, b in zip(out, out[1:])) / 1000.0
    matched_parts = [p for p in parts if p["mode"] in ("driving", "cycling", "walking", "train")]
    return {
        "id": track_id, "km": round(raw_km, 2), "distance": round(gen_km, 2),
        "nraw": len(raw_pts), "ngen": len(out),
        "raw": [[p["lat"], p["lon"]] for p in raw_pts],
        "gen": [[p["lat"], p["lon"]] for p in out],
        "parts": parts,
        "points": out,
        "matched_km": round(sum(p["distance"] for p in matched_parts), 2),
        "raw_km": round(sum(p["distance"] for p in parts if p["engine"] == "raw"), 2),
    }
