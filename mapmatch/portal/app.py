"""FastAPI application factory.

    uvicorn mapmatch.portal.app:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..engine import valhalla
from . import api, auth, site, trips
from .config import Config
from .db import Database
from .jobs import Jobs

STATIC = Path(__file__).with_name("static")


def create_app(cfg: Config | None = None, transport: Any = None, start_workers: bool = True) -> FastAPI:
    cfg = cfg or Config()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(cfg.db_path)
    auth.bootstrap_admin(db, cfg.admin_user, cfg.admin_password)
    if not auth.admin_count(db):
        logging.getLogger("mapmatch").warning(
            "no admin account: set ADMIN_USER and ADMIN_PASSWORD to create one")
    # One Valhalla for the whole instance; an admin sets it in the portal and it
    # is kept in the database rather than the container environment.
    valhalla.configure(site.valhalla_url(db))
    trips.refresh_previews(db)
    jobs = Jobs(db, cfg, transport)
    state = SimpleNamespace(db=db, cfg=cfg, jobs=jobs)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_workers:
            jobs.start(scheduler=cfg.scheduler)
        yield
        jobs.stop()

    app = FastAPI(title="Mapmatch", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.portal = state
    app.include_router(api.build(state))
    prefix = cfg.prefix
    app.mount(prefix + "/static", StaticFiles(directory=STATIC), name="static")

    @app.get(prefix + "/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    if prefix:
        @app.get(prefix, include_in_schema=False)
        def bare() -> RedirectResponse:
            return RedirectResponse(prefix + "/")

    @app.get(prefix + "/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"ok": "true"}

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn mapmatch.portal.app:app` builds the app lazily from the environment.
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
