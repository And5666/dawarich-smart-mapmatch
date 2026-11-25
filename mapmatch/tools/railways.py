"""Build a railway network for train matching from an OpenStreetMap extract.

    python -m mapmatch.tools.railways italy-latest.osm.pbf --out /data/rail

The same .osm.pbf extracts the map server (Valhalla) is built from work here,
for example from download.geofabrik.de. Running lines are kept (rail,
light_rail, subway, narrow_gauge, monorail); sidings, yards, spurs and
disused or abandoned track are left out. The result is
railways-<name>.json.gz, which mapmatch.engine.railnet reads.

Needs pyosmium: pip install osmium (or pip install "mapmatch[rail]").
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable

RUNNING = {"rail", "light_rail", "subway", "narrow_gauge", "monorail"}
NOT_RUNNING_SERVICE = {"yard", "siding", "spur"}
STATIONS = {"station", "halt"}


def available() -> bool:
    try:
        import osmium  # noqa: F401
    except ImportError:
        return False
    return True


def name_of(pbf: str) -> str:
    base = os.path.basename(pbf)
    base = re.sub(r"(-latest)?\.osm\.pbf$|\.pbf$", "", base)
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-") or "railways"


def build(pbf: str, out_dir: str, name: str | None = None,
          progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Read the extract and write railways-<name>.json.gz into out_dir."""
    import osmium

    say = progress or (lambda _m: None)
    name = name or name_of(pbf)
    index: dict[int, int] = {}
    nodes: list[list[float]] = []
    ways: list[list[int]] = []
    stations: list[list[float]] = []

    class Handler(osmium.SimpleHandler):
        def node(self, n: Any) -> None:
            if n.tags.get("railway") in STATIONS and n.location.valid():
                stations.append([round(n.location.lat, 6), round(n.location.lon, 6)])

        def way(self, w: Any) -> None:
            tags = w.tags
            if tags.get("railway") not in RUNNING or tags.get("service") in NOT_RUNNING_SERVICE:
                return
            if tags.get("disused") == "yes" or tags.get("abandoned") == "yes":
                return
            idx = []
            for n in w.nodes:
                if not n.location.valid():
                    continue
                k = index.get(n.ref)
                if k is None:
                    k = index[n.ref] = len(nodes)
                    nodes.append([round(n.location.lat, 6), round(n.location.lon, 6)])
                idx.append(k)
            if len(idx) >= 2:
                ways.append(idx)

    say(f"reading {os.path.basename(pbf)}")
    Handler().apply_file(pbf, locations=True)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"railways-{name}.json.gz")
    tmp = out + ".tmp"
    with gzip.open(tmp, "wt") as f:
        json.dump({"name": name, "source": os.path.basename(pbf),
                   "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "nodes": nodes, "ways": ways, "stations": stations}, f, separators=(",", ":"))
    os.replace(tmp, out)
    say(f"wrote {len(ways)} lines, {len(stations)} stations")
    return {"file": out, "name": name, "lines": len(ways), "nodes": len(nodes), "stations": len(stations)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mapmatch.tools.railways", description=__doc__.split("\n")[0])
    ap.add_argument("pbf", help="an OpenStreetMap extract (.osm.pbf)")
    ap.add_argument("--out", default=os.path.join(os.environ.get("PORTAL_DATA", "./portal-data"), "rail"),
                    help="where to write it (default: the portal's data directory, rail/)")
    ap.add_argument("--name", help="name for the network (default: from the file name)")
    args = ap.parse_args(argv)
    if not available():
        print("pyosmium is needed: pip install osmium", file=sys.stderr)
        return 2
    res = build(args.pbf, args.out, args.name, progress=print)
    print(f"{res['file']}: {res['lines']} lines, {res['nodes']} nodes, {res['stations']} stations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
