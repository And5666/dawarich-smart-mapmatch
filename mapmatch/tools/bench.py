"""Benchmark the matcher on a Dawarich CSV export.

    VALHALLA_URL=http://host:8002 python -m mapmatch.tools.bench \
        --csv data/test-account-points.csv --engine new --trips tracker

--trips tracker  one trip per tracker_id (the account's existing route ids),
                 "*-loose" points left out; like-for-like with the old engine.
--trips auto     the whole stream, split by the new engine's own segmenter.

Writes bench/<engine>-<trips>.json (ids and metrics only, no coordinates) and
bench/report/<engine>-<trips>.html (private: contains coordinates).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from mapmatch.engine import io as gio
from mapmatch.engine.quality import deviations, path_km, percentile

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- engines
def run_old(points: list[dict[str, Any]], tid: int) -> dict[str, Any]:
    """The pre-redesign smartmap pipeline, normalized for comparison."""
    sys.path.insert(0, str(ROOT))
    import smartmap.valhalla as sv
    from mapmatch.engine import valhalla as nv

    def _post(path, body, timeout):  # share the response cache, keep the old error type
        try:
            return nv.post(path, body, timeout)
        except nv.ValhallaError as exc:
            raise sv.ValhallaError(str(exc)) from exc
    sv._post = _post
    from smartmap.assemble import build_track
    from smartmap.connectors import add_connectors, repair_internal_gaps, rescue_raw_parts
    from smartmap.infer import infer_track
    from smartmap.stream import from_parts

    pts = [{k: p[k] for k in ("lat", "lon", "timestamp", "accuracy", "velocity", "altitude")} for p in points]
    tr = infer_track(pts)
    built = build_track(tid, tr)
    built["_raw_points"] = tr["fixes"]
    repair_internal_gaps(built)
    rescue_raw_parts(built)
    add_connectors(built)
    stream = from_parts(built)
    pieces = []
    for p in built["parts"]:
        mode, eng = p["mode"], p["engine"]
        if mode == "connector":
            kind = "straight" if "straight" in p["label"] else "filled"
        elif mode == "stationary":
            kind = "stay"
        elif mode == "raw":
            kind = "raw"
        elif eng.startswith(("repaired", "rescued")):
            kind = "filled"
        else:
            kind = "matched"
        pieces.append({"mode": mode, "kind": kind, "km": p["distance"], "n_in": p.get("n_in", 0),
                       "coords": p["points"]})
    segs = [p for p in pieces if p["mode"] != "connector"]
    return {"pieces": pieces, "segments": len(segs), "stream": stream}


def run_new(points: list[dict[str, Any]], tid: int) -> dict[str, Any]:
    from mapmatch.engine.api import match_trip
    res = match_trip(points)
    pieces = []
    for s in res["segments"]:
        for pc in s["pieces"]:
            pieces.append({"mode": s["mode"], "kind": pc["kind"], "km": pc["km"],
                           "n_in": pc.get("n_in", 0), "coords": [c[:2] for c in pc["coords"]]})
    return {"pieces": pieces, "segments": len(res["segments"]), "stream": res["stream"],
            "modes": [s["mode"] for s in res["segments"]]}


# ---------------------------------------------------------------- trips
def trips_tracker(points: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for p in points:
        if p["tracker_id"].endswith("-loose"):
            continue
        by[p["tracker_id"]].append(p)
    out = []
    for k, v in by.items():
        suffix = k.rsplit("-", 1)[-1]
        out.append((int(suffix) if suffix.isdigit() else abs(hash(k)) % 10**9, v))
    return sorted(out)


def trips_auto(points: list[dict[str, Any]]) -> list[tuple[int, list[dict[str, Any]]]]:
    from mapmatch.engine.trips import split_trips
    return [(i, t) for i, t in enumerate(split_trips(points))]


def agreement(auto: list[list[dict[str, Any]]], points: list[dict[str, Any]]) -> dict[str, Any]:
    """How the auto trips line up with the account's tracker routes."""
    windows: dict[str, list[int]] = {}
    for p in points:
        k = p["tracker_id"]
        if k.endswith("-loose"):
            continue
        w = windows.setdefault(k, [p["timestamp"], p["timestamp"]])
        w[0] = min(w[0], p["timestamp"])
        w[1] = max(w[1], p["timestamp"])
    route_of_trip = []
    loose_only = 0
    for t in auto:
        keys = collections.Counter(p["tracker_id"] for p in t if not p["tracker_id"].endswith("-loose"))
        if not keys:
            loose_only += 1
        route_of_trip.append(set(keys))
    trips_per_route = collections.Counter(k for s in route_of_trip for k in s)
    exact = sum(1 for k in windows if trips_per_route.get(k) == 1
                and any(s == {k} for s in route_of_trip))
    return {
        "routes": len(windows), "auto_trips": len(auto),
        "routes_one_trip": exact,
        "routes_split": sum(1 for k in windows if trips_per_route.get(k, 0) > 1),
        "routes_missing": sum(1 for k in windows if trips_per_route.get(k, 0) == 0),
        "trips_merging_routes": sum(1 for s in route_of_trip if len(s) > 1),
        "trips_loose_only": loose_only,
    }


# ---------------------------------------------------------------- metrics
def trip_metrics(tid: int, raw: list[dict[str, Any]], res: dict[str, Any]) -> dict[str, Any]:
    raw_km = path_km(raw)
    pieces = res["pieces"]
    km = collections.Counter()
    for p in pieces:
        km[p["kind"]] += p["km"]
    out_km = path_km(res["stream"])
    errs = deviations(raw, res["stream"])
    return {
        "id": tid, "n_in": len(raw), "n_out": len(res["stream"]),
        "raw_km": round(raw_km, 3), "out_km": round(out_km, 3),
        "segments": res["segments"],
        "single_fix_raw": sum(1 for p in pieces if p["kind"] == "raw" and p["n_in"] <= 1),
        "straight": sum(1 for p in pieces if p["kind"] == "straight"),
        "km": {k: round(v, 3) for k, v in km.items()},
        "unmatched_frac": round((km["raw"] + km["straight"]) / max(0.01, raw_km), 3),
        "p50": round(percentile(errs, 0.5), 1), "p95": round(percentile(errs, 0.95), 1),
        "modes": res.get("modes"),
    }


def summarize(ms: list[dict[str, Any]], failures: list[Any], secs: float) -> dict[str, Any]:
    km = collections.Counter()
    for m in ms:
        km.update(m["km"])
    raw_km = sum(m["raw_km"] for m in ms)
    out_km = sum(m["out_km"] for m in ms)
    return {
        "trips": len(ms), "failures": len(failures),
        "straight": sum(m["straight"] for m in ms),
        "single_fix_raw": sum(m["single_fix_raw"] for m in ms),
        "segments_per_trip": round(sum(m["segments"] for m in ms) / max(1, len(ms)), 2),
        "trips_over_20pct_unmatched": sum(1 for m in ms if m["unmatched_frac"] > 0.2),
        "unmatched_km": round(km["raw"] + km["straight"], 1),
        "km_by_kind": {k: round(v, 1) for k, v in sorted(km.items())},
        "out_over_raw_km": round(out_km / max(0.01, raw_km), 3),
        "median_p95": percentile([m["p95"] for m in ms], 0.5),
        "mean_p95": round(sum(m["p95"] for m in ms) / max(1, len(ms)), 1),
        "seconds": round(secs, 1),
    }


def html_report(path: Path, rows: list[dict[str, Any]]) -> None:
    data = json.dumps(rows, separators=(",", ":"))
    path.write_text("""<!doctype html><meta charset=utf-8><title>bench</title>
<link rel=stylesheet href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{margin:0;font:13px system-ui;display:grid;grid-template-columns:300px 1fr;height:100vh}
#l{overflow:auto;border-right:1px solid #ccc}#l div{padding:6px 10px;cursor:pointer;border-bottom:1px solid #eee}
#l div:hover{background:#eef}#m{height:100vh}</style><div id=l></div><div id=m></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
const R=""" + data + """;const C={matched:'#1f77b4',filled:'#2ca02c',raw:'#d62728',straight:'#ff00ff',stay:'#999'};
const M=L.map('m');L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19}).addTo(M);let G=L.layerGroup().addTo(M);
function show(r){G.clearLayers();L.polyline(r.raw,{color:'#ff7f0e',weight:2,dashArray:'3 4'}).addTo(G);r.raw.forEach(p=>L.circleMarker(p,{radius:2,color:'#ff7f0e'}).addTo(G));
r.pieces.forEach(p=>{if(p.coords.length>1)L.polyline(p.coords,{color:C[p.kind]||'#000',weight:4,opacity:.8}).bindTooltip(p.mode+' '+p.kind+' '+p.km.toFixed(2)+'km').addTo(G)});M.fitBounds(r.raw)}
const l=document.getElementById('l');R.forEach(r=>{let d=document.createElement('div');d.innerHTML=`<b>#${r.id}</b> ${(r.m.unmatched_frac*100).toFixed(0)}% unmatched · ${r.m.segments} seg · p95 ${r.m.p95}m<br><small>${(r.m.modes||[]).join(' › ')}</small>`;d.onclick=()=>show(r);l.appendChild(d)});if(R.length)show(R[0]);
</script>""")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data/test-account-points.csv"))
    ap.add_argument("--engine", choices=("old", "new"), default="new")
    ap.add_argument("--trips", choices=("tracker", "auto"), default="tracker")
    ap.add_argument("--only", default="", help="comma-separated trip ids")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "bench"))
    ap.add_argument("--worst", type=int, default=25)
    args = ap.parse_args(argv)
    os.environ.setdefault("VALHALLA_CACHE_DIR", str(ROOT / "bench/cache"))

    points = gio.read_csv(args.csv)
    if args.trips == "auto":
        if args.engine == "old":
            ap.error("--trips auto needs --engine new")
        trips = trips_auto(points)
        agree = agreement([t for _, t in trips], points)
    else:
        trips = trips_tracker(points)
        agree = None
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        trips = [t for t in trips if t[0] in keep]
    run = run_old if args.engine == "old" else run_new
    print(f"{len(trips)} trips, {sum(len(t) for _, t in trips)} fixes, engine={args.engine}", flush=True)

    t0 = time.time()
    results: dict[int, dict[str, Any]] = {}
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(run, pts, tid): (tid, pts) for tid, pts in trips}
        for fut in as_completed(futs):
            tid, pts = futs[fut]
            try:
                results[tid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                import traceback
                failures.append({"id": tid, "error": f"{type(exc).__name__}: {exc}"})
                print(f"  #{tid} FAILED {type(exc).__name__}: {exc}", flush=True)
                traceback.print_exc()
    secs = time.time() - t0
    by_id = dict(trips)
    ms = [trip_metrics(tid, by_id[tid], results[tid]) for tid, _ in trips if tid in results]
    summary = summarize(ms, failures, secs)
    if agree:
        summary["segmentation"] = agree
    out = Path(args.out)
    (out / "report").mkdir(parents=True, exist_ok=True)
    name = f"{args.engine}-{args.trips}"
    json.dump({"summary": summary, "failures": failures, "trips": ms}, open(out / f"{name}.json", "w"), indent=1)
    worst = sorted(ms, key=lambda m: (-m["unmatched_frac"], -m["p95"]))[:args.worst]
    focus = [m for m in ms if m["id"] in (3247, 3347, 3193, 3345, 3361) and m not in worst]
    rows = []
    for m in worst + focus:
        r = results[m["id"]]
        rows.append({"id": m["id"], "m": m, "raw": [[p["lat"], p["lon"]] for p in by_id[m["id"]]],
                     "pieces": [{k: p[k] for k in ("mode", "kind", "km", "coords")} for p in r["pieces"]]})
    html_report(out / "report" / f"{name}.html", rows)
    print(json.dumps(summary, indent=1))
    print(f"wrote {out / (name + '.json')} and {out / 'report' / (name + '.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
