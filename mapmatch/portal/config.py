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
    # When PUSH_DRY_RUN is set at all, only an explicit 0/false/no/off means live:
    # a typo ("y", "ture") must not silently turn dry run off. Unset keeps upstream's default.
    push_dry_run: bool = field(default_factory=lambda: (os.environ.get("PUSH_DRY_RUN") is not None
                               and os.environ["PUSH_DRY_RUN"].strip().lower() not in ("0", "false", "no", "off", "")))
    secure_cookie: bool = field(default_factory=lambda: _flag("PORTAL_SECURE_COOKIE"))
    # When the portal is one page of a larger site: a link back to it in the top bar,
    # an extra stylesheet (fonts, colours) loaded after the portal's own, and the
    # localStorage key that site keeps its light/dark choice under, to share it.
    home_url: str = field(default_factory=lambda: os.environ.get("PORTAL_HOME_URL", "").strip())
    home_label: str = field(default_factory=lambda: os.environ.get("PORTAL_HOME_LABEL", "Dashboard").strip() or "Dashboard")
    extra_css: str = field(default_factory=lambda: os.environ.get("PORTAL_EXTRA_CSS", "").strip())
    theme_key: str = field(default_factory=lambda: os.environ.get("PORTAL_THEME_KEY", "").strip())
    # An offline basemap instead of CARTO's hosted one: the URL of a Protomaps .pmtiles file
    # and of the folder holding pmtiles.js, basemaps.js, fonts/ and sprites/v4/. Both or neither.
    pmtiles_url: str = field(default_factory=lambda: os.environ.get("PORTAL_PMTILES_URL", "").strip())
    pmtiles_assets: str = field(default_factory=lambda: os.environ.get("PORTAL_PMTILES_ASSETS", "").strip())
    session_days: int = field(default_factory=lambda: int(os.environ.get("PORTAL_SESSION_DAYS", "30")))
    scheduler: bool = field(default_factory=lambda: _flag("PORTAL_SCHEDULER", True))
    # First/admin account. Password empty means no environment admin is made.
    admin_user: str = field(default_factory=lambda: os.environ.get("ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    # Optional read-only mount of the Valhalla data directory, so an admin can
    # list and download the extracts and tiles behind the configured server.
    valhalla_data_dir: str = field(default_factory=lambda: os.environ.get("VALHALLA_DATA_DIR", ""))

    @property
    def prefix(self) -> str:
        return "" if self.base_path == "/" else self.base_path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "portal.db"
