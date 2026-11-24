"""Benchmark the matcher on a Dawarich CSV export.

    VALHALLA_URL=http://host:8002 python -m mapmatch.tools.bench \
        --csv data/test-account-points.csv --trips tracker --compare bench/old-tracker.json

--trips tracker  one trip per tracker_id (the account's existing route ids),
                 "*-loose" points left out; comparable with bench/old-tracker.json.
--trips auto     the whole stream, split by the new engine's own segmenter.

Writes bench/<engine>-<trips>.json (ids and metrics only, no coordinates).
The HTML page that showed the worst trips on a map was retired once the engine
was good enough to stop eyeballing; it is archived under `_attic` on the server.
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


# ---------------------------------------------------------------- engine
# The pre-redesign engine (smartmap/) was removed after this harness
# recorded its results in bench/old-tracker.json; use --compare to diff.
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
        "straight_25m": sum(1 for p in pieces if p["kind"] in ("straight", "join") and p["km"] >= 0.025),
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
        "straight_25m": sum(m.get("straight_25m", 0) for m in ms),
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / "data/test-account-points.csv"))
    ap.add_argument("--engine", choices=("new",), default="new")
    ap.add_argument("--compare", default="", help="earlier bench JSON to print deltas against")
    ap.add_argument("--trips", choices=("tracker", "auto"), default="tracker")
    ap.add_argument("--only", default="", help="comma-separated trip ids")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "bench"))
    args = ap.parse_args(argv)
    os.environ.setdefault("VALHALLA_CACHE_DIR", str(ROOT / "bench/cache"))

    points = gio.read_csv(args.csv)
    if args.trips == "auto":
        trips = trips_auto(points)
        agree = agreement([t for _, t in trips], points)
    else:
        trips = trips_tracker(points)
        agree = None
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        trips = [t for t in trips if t[0] in keep]
    run = run_new
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
    name = f"{args.engine}-{args.trips}"
    json.dump({"summary": summary, "failures": failures, "trips": ms}, open(out / f"{name}.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))
    if args.compare:
        base = json.load(open(args.compare))["summary"]
        print(f"\n{'metric':28s} {'before':>10s} {'now':>10s}")
        for k, v in summary.items():
            if isinstance(v, (int, float)) and k in base and k != "seconds":
                print(f"{k:28s} {base[k]!s:>10s} {v!s:>10s}")
    print(f"wrote {out / (name + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
