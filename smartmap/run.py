"""Batch runner: raw Dawarich fixes -> state model -> generated track + review."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .assemble import build_track
from .connectors import add_connectors
from .infer import infer_track
from .metrics import track_metrics
from .review import build as build_review


def _load_ids(path: str | None, fallback: list[int]) -> list[int]:
    if not path or not os.path.exists(path):
        return fallback
    data = json.load(open(path))
    if isinstance(data, dict):
        data = data.get("ids") or data.get("routes") or []
    out = []
    for x in data:
        if isinstance(x, dict):
            x = x.get("id") or x.get("track_id")
        if x is not None:
            out.append(int(x))
    return out


def _fallback(tid: int, points: list[dict]) -> dict:
    pts = sorted((dict(p) for p in points), key=lambda p: p["timestamp"])
    fixes = []
    for p in pts:
        fixes.append({**p, "_state": "raw", "_speed": 0.0, "_i": len(fixes)})
    n = len(fixes)
    sections = [{"kind": "raw", "start": 0, "end": n, "states": ["raw"] * n,
                 "candidates": [None] * n, "match": None}]
    tr = build_track(tid, {"fixes": fixes, "sections": sections})
    tr["_raw_points"] = fixes
    return tr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw_v5.json")
    ap.add_argument("--ids", default="data/routes_50.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    raw_all = json.load(open(args.raw))
    by_id: dict[int, list[dict]] = collections.defaultdict(list)
    for p in raw_all:
        if p.get("track_id") is not None:
            by_id[int(p["track_id"])].append(p)
    for v in by_id.values():
        v.sort(key=lambda p: (p["timestamp"], p["lat"], p["lon"]))
    ids = _load_ids(args.ids, sorted(by_id))
    if args.limit:
        ids = ids[:args.limit]
    ids = [i for i in ids if i in by_id]
    print(f"{len(ids)} routes, {sum(len(by_id[i]) for i in ids)} fixes, valhalla={os.environ.get('VALHALLA_URL')}", flush=True)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    results: dict[int, dict] = {}
    failures = 0

    def work(tid: int) -> dict:
        tr = infer_track(by_id[tid])
        built = build_track(tid, tr)
        add_connectors(built)
        built["_raw_points"] = tr["fixes"]
        return built

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = {pool.submit(work, tid): tid for tid in ids}
        done = 0
        for fut in as_completed(futs):
            tid = futs[fut]
            try:
                results[tid] = fut.result()
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"  #{tid} failed: {type(exc).__name__}: {exc}", flush=True)
                results[tid] = _fallback(tid, by_id[tid])
            done += 1
            tr = results[tid]
            print(f"  {done}/{len(ids)} #{tid} {tr['km']}km -> {tr['distance']}km "
                  f"{tr['nraw']}->{tr['ngen']} parts={len(tr['parts'])} ({time.time()-t0:.0f}s)", flush=True)

    tracks = [results[i] for i in ids if i in results]
    metrics = []
    for t in tracks:
        try:
            metrics.append(track_metrics(t))
        except Exception as exc:  # noqa: BLE001
            print(f"  metrics #{t['id']} failed: {exc}", flush=True)
    review = build_review(tracks)

    # Keep the review document lean; points carry timestamps for metrics but
    # the review page only needs lat/lon.
    json.dump(tracks, open(out_dir / "tracks.json", "w"))
    json.dump(review, open(out_dir / "review.json", "w"))
    summary = {
        "routes": len(tracks), "failures": failures,
        "points_in": sum(m["nraw"] for m in metrics),
        "points_out": sum(m["ngen"] for m in metrics),
        "raw_km": round(sum(m["raw_km"] for m in metrics), 1),
        "gen_km": round(sum(m["gen_km"] for m in metrics), 1),
        "matched_km": round(sum(m["matched_km"] for m in metrics), 1),
        "duplicates": sum(m["duplicates"] for m in metrics),
        "spikes": sum(m["spikes"] for m in metrics),
        "zigzags": sum(m["zigzags"] for m in metrics),
        "metrics": metrics,
    }
    json.dump(summary, open(out_dir / "summary.json", "w"))
    print(json.dumps({k: v for k, v in summary.items() if k != "metrics"}, indent=1))
    print(f"wrote {out_dir}/tracks.json {out_dir}/review.json {out_dir}/summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
