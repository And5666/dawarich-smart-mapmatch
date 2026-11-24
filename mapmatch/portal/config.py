"""Portal settings from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("PORTAL_DATA", "./portal-data")))
    base_path: str = field(default_factory=lambda: "/" + os.environ.get("PORTAL_BASE_PATH", "").strip("/"))
    dawarich_url: str = field(default_factory=lambda: os.environ.get("DAWARICH_URL", "http://dawarich_app:3000").rstrip("/"))
    dawarich_host_header: str = field(default_factory=lambda: os.environ.get("DAWARICH_HOST_HEADER", ""))
    match_workers: int = field(default_factory=lambda: int(os.environ.get("MATCH_WORKERS", "4")))
    push_dry_run: bool = field(default_factory=lambda: _flag("PUSH_DRY_RUN"))
    secure_cookie: bool = field(default_factory=lambda: _flag("PORTAL_SECURE_COOKIE"))
    session_days: int = field(default_factory=lambda: int(os.environ.get("PORTAL_SESSION_DAYS", "30")))
    scheduler: bool = field(default_factory=lambda: _flag("PORTAL_SCHEDULER", True))

    @property
    def prefix(self) -> str:
        return "" if self.base_path == "/" else self.base_path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "portal.db"
