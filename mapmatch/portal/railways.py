"""Railway networks for train matching outside Great Britain.

An admin builds one from an OpenStreetMap extract in the mounted Valhalla
data directory (the same .osm.pbf the map server was built from). It is
written to <PORTAL_DATA>/rail and the matcher picks it up at once. Only files
built here can be removed; the ones shipped with mapmatch stay.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..engine import railnet
from ..tools import railways as tool

_lock = threading.Lock()
_status: dict[str, Any] = {"running": False, "source": "", "message": "", "error": "", "finished": ""}


def rail_dir(cfg: Any) -> Path:
    return Path(cfg.data_dir) / "rail"


def configure(cfg: Any) -> None:
    railnet.configure(rail_dir(cfg))


def sources(cfg: Any) -> list[dict[str, Any]]:
    """OpenStreetMap extracts an admin can build from."""
    root = cfg.valhalla_data_dir
    if not root or not Path(root).is_dir():
        return []
    out = []
    for p in sorted(Path(root).iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.name.endswith(".pbf"):
            out.append({"name": p.name, "size": p.stat().st_size, "network": tool.name_of(p.name)})
    return out


def state(cfg: Any) -> dict[str, Any]:
    mine = rail_dir(cfg).resolve()
    nets = []
    for n in railnet.networks():
        st = os.stat(n.path)
        nets.append({"name": n.name, "file": os.path.basename(n.path), "km": round(n.km), "stations": len(n.stations),
                     "built": n.built or datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                     "removable": Path(n.path).resolve().parent == mine})
    return {"networks": nets, "sources": sources(cfg), "mounted": bool(cfg.valhalla_data_dir and Path(cfg.valhalla_data_dir).is_dir()),
            "can_build": tool.available(), "status": dict(_status)}


def build(cfg: Any, source: str) -> dict[str, Any]:
    """Start building from one extract, in the background."""
    if not tool.available():
        raise ValueError("this server can't read map extracts: install pyosmium (pip install osmium)")
    names = {s["name"] for s in sources(cfg)}
    if source not in names:
        raise ValueError("no such extract in the map data directory")
    with _lock:
        if _status["running"]:
            raise ValueError("a railway network is already being built")
        _status.update(running=True, source=source, message="starting", error="", finished="")

    def run() -> None:
        try:
            res = tool.build(str(Path(cfg.valhalla_data_dir) / source), str(rail_dir(cfg)),
                             progress=lambda m: _status.update(message=m))
            railnet.reload()
            _status.update(message=f"{res['lines']} lines, {res['stations']} stations")
        except Exception as exc:  # noqa: BLE001 - shown to the admin
            _status.update(error=f"{type(exc).__name__}: {exc}"[:400])
        finally:
            _status.update(running=False, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    threading.Thread(target=run, name="railways", daemon=True).start()
    return dict(_status)


def remove(cfg: Any, file: str) -> None:
    base = rail_dir(cfg).resolve()
    target = (base / file).resolve()
    if target.parent != base or not target.name.startswith("railways-") or not target.is_file():
        raise ValueError("only networks built on this server can be removed")
    target.unlink()
    railnet.reload()
