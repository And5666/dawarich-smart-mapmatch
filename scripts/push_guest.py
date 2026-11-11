#!/usr/bin/env python3
"""Replace one Dawarich account's points with a generated track set."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

HOST = "dawarich.andw.net"


def call(base: str, key: str, method: str, path: str, body=None, timeout=120):
    url = f"{base.rstrip('/')}{path}"
    sep = "&" if "?" in path else "?"
    url += f"{sep}api_key={key}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Host": HOST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None, dict(r.headers)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path}: HTTP {exc.code} {exc.read().decode()[:300]}") from exc


def stats(base, key):
    return call(base, key, "GET", "/api/v1/stats")[1]


def all_point_ids(base, key):
    ids = []
    page = 1
    while True:
        _, body, headers = call(base, key, "GET",
                                f"/api/v1/points?page={page}&per_page=1000&order=asc")
        batch = body or []
        ids.extend(int(p["id"]) for p in batch if p.get("id") is not None)
        pages = int(headers.get("x-total-pages") or headers.get("X-Total-Pages") or 1)
        if page >= pages or not batch:
            break
        page += 1
    return ids


def delete_points(base, key, ids, chunk=500):
    for i in range(0, len(ids), chunk):
        call(base, key, "DELETE", "/api/v1/points/bulk_destroy",
             {"point_ids": ids[i:i + chunk]})
        if (i // chunk) % 20 == 19:
            print(f"  deleted {min(i + chunk, len(ids))}/{len(ids)}", flush=True)


def feature(p, device):
    props = {"timestamp": int(p["timestamp"]), "device_id": device}
    for src, dst, cast in (("accuracy", "horizontal_accuracy", float),
                           ("velocity", "speed", float),
                           ("altitude", "altitude", float),
                           ("course", "course", float)):
        if p.get(src) is not None:
            props[dst] = cast(p[src])
    if p.get("inserted"):
        props["mapmatch_inserted"] = True
    return {"type": "Feature",
            "geometry": {"type": "Point", "coordinates": [float(p["lon"]), float(p["lat"])]},
            "properties": props}


def push(base, key, tracks, device, batch=500, per_track=False):
    feats = []
    for t in tracks:
        dev = f"{device}-{t['id']}" if per_track else device
        for p in t.get("points") or []:
            feats.append(feature(p, dev))
    feats.sort(key=lambda f: f["properties"]["timestamp"])
    print(f"points to push: {len(feats)}", flush=True)
    written = 0
    t0 = time.time()
    for i in range(0, len(feats), batch):
        call(base, key, "POST", "/api/v1/points", {"locations": feats[i:i + batch]})
        written += len(feats[i:i + batch])
        if written % 10000 < batch:
            print(f"  pushed {written}/{len(feats)} in {time.time() - t0:.0f}s", flush=True)
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://dawarich_app:3001")
    ap.add_argument("--key-file", default="/data/.dawarich_test_key")
    ap.add_argument("--tracks", default="/data/all_tracks.json")
    ap.add_argument("--device", default="smartmap-v33")
    ap.add_argument("--device-per-track", action="store_true")
    ap.add_argument("--delete-existing", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    key = open(args.key_file).read().strip()
    print("before", stats(args.base, key), flush=True)
    if args.delete_existing:
        ids = all_point_ids(args.base, key)
        print("deleting", len(ids), "points", flush=True)
        if not args.dry_run:
            delete_points(args.base, key, ids)
            print("after delete", stats(args.base, key), flush=True)
    tracks = json.load(open(args.tracks))
    if not args.dry_run:
        print("pushed", push(args.base, key, tracks, args.device, per_track=args.device_per_track), flush=True)
        print("after", stats(args.base, key), flush=True)


if __name__ == "__main__":
    main()
