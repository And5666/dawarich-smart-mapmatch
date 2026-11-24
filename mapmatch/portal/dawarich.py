"""Dawarich API client (points read, delete and write)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import httpx

MAPMATCH_DEVICE = "mapmatch"
# Every point the portal writes carries this as its Wi-Fi name (Dawarich keeps
# the Overland `wifi` property as the point's ssid). The device is left alone:
# Dawarich builds tracks per device, so matched points must sit on the same
# device as the GPS around them or its tracks jump across the gap.
MARK = "mapmatch"
PAGE = 1000
BATCH = 500


class DawarichError(RuntimeError):
    pass


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


class Client:
    def __init__(self, base_url: str, api_key: str, host_header: str = "",
                 transport: httpx.BaseTransport | None = None, timeout: float = 120.0):
        if not base_url or not api_key:
            raise DawarichError("Dawarich URL and API key are required")
        headers = {"Accept": "application/json"}
        if host_header:
            headers["Host"] = host_header
        self.key = api_key
        self.http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers,
                                 timeout=httpx.Timeout(timeout, connect=10.0), transport=transport)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        params = {"api_key": self.key, **kw.pop("params", {})}
        try:
            r = self.http.request(method, path, params=params, **kw)
        except httpx.HTTPError as exc:
            raise DawarichError(f"{method} {path}: {exc}") from exc
        if r.status_code in (401, 403):
            raise DawarichError("Dawarich rejected the API key")
        if r.status_code >= 300:
            raise DawarichError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
        return r

    def points(self, start: int | None = None, end: int | None = None,
               progress: Callable[[int, int], None] | None = None,
               on_page: Callable[[list[dict[str, Any]], int, int], None] | None = None) -> list[dict[str, Any]]:
        """All points in [start, end], oldest first, with ids and tracker ids.
        `on_page(batch, page, pages)` sees each page as it arrives."""
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            # Dawarich hides points its anomaly filter flagged unless asked; the
            # archive has to hold those too, or a push leaves them behind.
            params: dict[str, Any] = {"page": page, "per_page": PAGE, "order": "asc",
                                      "include_anomalies": "true"}
            if start is not None:
                params["start_at"] = _iso(start)
            if end is not None:
                params["end_at"] = _iso(end)
            r = self._req("GET", "/api/v1/points", params=params)
            batch = r.json()
            if not isinstance(batch, list):
                raise DawarichError("unexpected points response")
            out.extend(batch)
            pages = int(r.headers.get("x-total-pages") or 1)
            if on_page:
                on_page(batch, page, pages)
            if progress:
                progress(page, pages)
            if page >= pages or not batch:
                break
            page += 1
        return out

    def me(self) -> dict[str, Any]:
        """The Dawarich account this key belongs to."""
        r = self._req("GET", "/api/v1/users/me")
        user = (r.json() or {}).get("user") or {}
        if not user.get("id"):
            raise DawarichError("Dawarich did not say who this key belongs to")
        return user

    def count(self) -> int:
        r = self._req("GET", "/api/v1/points", params={"per_page": 1, "page": 1})
        total = r.headers.get("x-total-count")
        if total is not None:
            return int(total)
        return int(r.headers.get("x-total-pages") or 0)

    def delete(self, ids: list[int]) -> int:
        for i in range(0, len(ids), BATCH):
            self._req("DELETE", "/api/v1/points/bulk_destroy", json={"point_ids": ids[i:i + BATCH]})
        return len(ids)

    def tracks(self, start: int, end: int) -> list[tuple[int, int]]:
        """(start, end) of every Dawarich track overlapping [start, end].

        Empty when this Dawarich has no tracks API; callers then work on the
        trip windows alone."""
        out: list[tuple[int, int]] = []
        page = 1
        try:
            while True:
                r = self._req("GET", "/api/v1/tracks", params={"start_at": _iso(start), "end_at": _iso(end),
                                                               "page": page, "per_page": 500})
                body = r.json()
                feats = body.get("features") if isinstance(body, dict) else None
                if not feats:
                    break
                for f in feats:
                    pr = f.get("properties") or {}
                    try:
                        a = int(datetime.fromisoformat(str(pr["start_at"])).timestamp())
                        b = int(datetime.fromisoformat(str(pr["end_at"])).timestamp())
                    except (KeyError, ValueError):
                        continue
                    out.append((min(a, b), max(a, b)))
                if page >= int(r.headers.get("x-total-pages") or 1):
                    break
                page += 1
        except DawarichError:
            return out
        return out

    def post(self, points: list[dict[str, Any]]) -> int:
        """Write points as Overland-style GeoJSON features.

        Each point may carry `tracker` (the Dawarich device; empty means none)
        and `ours` (mark it as written by the portal)."""
        feats = []
        for p in sorted(points, key=lambda p: int(p["timestamp"])):
            props: dict[str, Any] = {"timestamp": int(p["timestamp"])}
            if p.get("tracker"):
                props["device_id"] = str(p["tracker"])
            if p.get("ours"):
                props["wifi"] = MARK
            for src, dst in (("accuracy", "horizontal_accuracy"), ("velocity", "speed"),
                             ("altitude", "altitude")):
                if p.get(src) is not None:
                    props[dst] = p[src]
            if p.get("mode"):
                props["motion"] = [p["mode"]]
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": [float(p["lon"]), float(p["lat"])]},
                          "properties": props})
        for i in range(0, len(feats), BATCH):
            self._req("POST", "/api/v1/points", json={"locations": feats[i:i + BATCH]})
        return len(feats)


def is_mapmatch(p: dict[str, Any]) -> bool:
    """Written by this portal: marked by Wi-Fi name, or (older pushes) by device."""
    return p.get("ssid") == MARK or str(p.get("tracker_id") or "").startswith(MAPMATCH_DEVICE)
