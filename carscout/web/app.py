from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from carscout import __version__
from carscout.core.db import get_engine
from carscout.web import api, pages


def create_app() -> FastAPI:
    app = FastAPI(title="CarScout", version=__version__, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(pages.router)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        try:
            with get_engine().connect() as c:
                rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=503)
        return JSONResponse({"ok": True, "version": __version__, "schema": rev})

    return app


app = create_app()
