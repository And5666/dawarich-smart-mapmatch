"""Read GPS fixes from a Dawarich CSV export or the Dawarich points API.

Every reader returns the same normalized dict:
{"lat", "lon", "timestamp", "accuracy", "velocity", "altitude", "tracker_id", "id"}
Missing numeric fields are None.
"""
from __future__ import annotations

import csv
import io
from typing import Any, Iterable


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize(p: dict[str, Any]) -> dict[str, Any] | None:
    lat = _num(p.get("lat", p.get("latitude")))
    lon = _num(p.get("lon", p.get("longitude")))
    ts = _num(p.get("timestamp"))
    if lat is None or lon is None or ts is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return {
        "lat": lat, "lon": lon, "timestamp": int(ts),
        "accuracy": _num(p.get("accuracy", p.get("horizontal_accuracy"))),
        "velocity": _num(p.get("velocity", p.get("speed"))),
        "altitude": _num(p.get("altitude")),
        "tracker_id": (p.get("tracker_id") or p.get("device_id") or "") or "",
        "id": p.get("id"),
    }


def read_csv_text(text: str) -> list[dict[str, Any]]:
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        p = normalize(row)
        if p:
            out.append(p)
    out.sort(key=lambda p: (p["timestamp"], p["lat"], p["lon"]))
    return out


def read_csv(path: str) -> list[dict[str, Any]]:
    with open(path, newline="") as f:
        return read_csv_text(f.read())


def from_api(points: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [q for q in (normalize(p) for p in points) if q]
    out.sort(key=lambda p: (p["timestamp"], p["lat"], p["lon"]))
    return out
