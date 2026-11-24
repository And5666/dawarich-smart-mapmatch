"""In-memory stand-in for the Dawarich points API, as an httpx transport."""
from __future__ import annotations

import json
import math
from datetime import datetime
from urllib.parse import parse_qs

import httpx


class FakeDawarich:
    def __init__(self, api_key: str = "secret"):
        self.api_key = api_key
        self.points: dict[int, dict] = {}
        self.next_id = 1
        self.requests: list[tuple[str, str]] = []
        self.fail_post = False

    # seed with normalized engine-style points
    def seed(self, pts: list[dict], tracker: str | None = "phone", anomaly: bool = False) -> None:
        for p in pts:
            self._add(p["lat"], p["lon"], int(p["timestamp"]), tracker, p.get("accuracy"), p.get("velocity"),
                      anomaly=anomaly)

    def _add(self, lat, lon, ts, tracker, acc=None, vel=None, ssid=None, anomaly=False) -> None:
        # Like Dawarich: a point at the same place and second is merged, not added.
        for q in self.points.values():
            if q["timestamp"] == ts and float(q["latitude"]) == float(lat) and float(q["longitude"]) == float(lon):
                q.update({"tracker_id": tracker, "ssid": ssid, "accuracy": acc})
                return
        pid = self.next_id
        self.next_id += 1
        self.points[pid] = {"id": pid, "latitude": str(lat), "longitude": str(lon), "timestamp": ts,
                            "accuracy": acc, "velocity": None if vel is None else str(vel),
                            "altitude": None, "tracker_id": tracker, "ssid": ssid, "anomaly": anomaly}

    def originals(self) -> set[tuple[int, int, int]]:
        return {(p["timestamp"], round(float(p["latitude"]) * 1e6), round(float(p["longitude"]) * 1e6))
                for p in self.points.values() if p.get("ssid") != "mapmatch"}

    def devices(self) -> dict[tuple[int, int, int], str | None]:
        return {(p["timestamp"], round(float(p["latitude"]) * 1e6), round(float(p["longitude"]) * 1e6)): p["tracker_id"]
                for p in self.points.values()}

    def ours(self) -> list[dict]:
        return [p for p in self.points.values() if p.get("ssid") == "mapmatch"]

    def tracks(self, gap: int = 1800) -> list[list[dict]]:
        """Dawarich-style tracks: per device, split where the time gap is long."""
        out: list[list[dict]] = []
        by: dict = {}
        for p in sorted(self.points.values(), key=lambda p: (p["timestamp"], p["id"])):
            by.setdefault(p["tracker_id"], []).append(p)
        for pts in by.values():
            cur = [pts[0]]
            for a, b in zip(pts, pts[1:]):
                if b["timestamp"] - a["timestamp"] > gap:
                    out.append(cur)
                    cur = []
                cur.append(b)
            out.append(cur)
        return [t for t in out if len(t) > 1]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        q = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        self.requests.append((request.method, request.url.path))
        if q.get("api_key") != self.api_key:
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/users/me":
            return httpx.Response(200, json={"user": {"id": 7, "email": "rider@example.com"}})
        if request.method == "GET" and path == "/api/v1/points":
            pts = sorted(self.points.values(), key=lambda p: (p["timestamp"], p["id"]))
            if q.get("include_anomalies") != "true":
                pts = [p for p in pts if not p.get("anomaly")]
            if "start_at" in q:
                a = int(datetime.fromisoformat(q["start_at"]).timestamp())
                pts = [p for p in pts if p["timestamp"] >= a]
            if "end_at" in q:
                b = int(datetime.fromisoformat(q["end_at"]).timestamp())
                pts = [p for p in pts if p["timestamp"] <= b]
            per = int(q.get("per_page", 100))
            page = int(q.get("page", 1))
            pages = max(1, math.ceil(len(pts) / per))
            return httpx.Response(200, json=pts[(page - 1) * per: page * per],
                                  headers={"x-total-pages": str(pages), "x-total-count": str(len(pts))})
        if request.method == "GET" and path == "/api/v1/tracks":
            a = int(datetime.fromisoformat(q["start_at"]).timestamp())
            b = int(datetime.fromisoformat(q["end_at"]).timestamp())
            feats = [{"type": "Feature", "properties": {
                "start_at": datetime.fromtimestamp(t[0]["timestamp"]).astimezone().isoformat(),
                "end_at": datetime.fromtimestamp(t[-1]["timestamp"]).astimezone().isoformat()}}
                for t in self.tracks() if t[0]["timestamp"] <= b and a <= t[-1]["timestamp"]]
            return httpx.Response(200, json={"type": "FeatureCollection", "features": feats},
                                  headers={"x-total-pages": "1"})
        if request.method == "DELETE" and path == "/api/v1/points/bulk_destroy":
            ids = json.loads(request.content)["point_ids"]
            for i in ids:
                self.points.pop(int(i), None)
            return httpx.Response(200, json={"count": len(ids)})
        if request.method == "POST" and path == "/api/v1/points":
            if self.fail_post:
                return httpx.Response(500, text="boom")
            for f in json.loads(request.content)["locations"]:
                lon, lat = f["geometry"]["coordinates"]
                pr = f["properties"]
                self._add(lat, lon, int(pr["timestamp"]), pr.get("device_id"),
                          pr.get("horizontal_accuracy"), pr.get("speed"), pr.get("wifi"))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"error": "not found"})
