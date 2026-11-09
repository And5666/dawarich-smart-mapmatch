"""Build the review document consumed by the existing part-level page."""
from __future__ import annotations


def _thin(points, limit):
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def build(tracks):
    out = []
    for t in tracks:
        parts = []
        for p in t["parts"]:
            if len(p.get("points") or []) < 2:
                continue
            parts.append({
                "i": len(parts), "start": p["start"], "end": p["end"],
                "mode": p["mode"], "engine": p["engine"], "label": p["label"],
                "n_in": p.get("n_in", 0), "n_out": p.get("n_out", 0),
                "distance": p.get("distance", 0),
                "points": p["points"],
            })
        if not parts:
            parts = [{"i": 0, "start": t["id"], "end": t["id"], "mode": "raw",
                      "engine": "raw", "label": "raw · raw", "points": t["raw"]}]
        out.append({
            "id": t["id"], "km": t["km"], "distance": t["distance"],
            "nraw": t["nraw"], "ngen": t["ngen"],
            "matched_km": t.get("matched_km", 0),
            "raw_km": t.get("raw_km", 0),
            "connections": 0,
            "parts": parts,
            "raw": _thin(t["raw"], 2200),
            "gen": _thin(t["gen"], 3500),
        })
    out.sort(key=lambda r: (-len(r["parts"]), r["id"]))
    return out
