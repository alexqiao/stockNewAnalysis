"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import __version__
from .api import api_router, web_router
from .config import Settings, get_settings
from .db import SessionFactory, build_engine, build_session_factory, initialize_database
from .scheduler import start_scheduler
from .services.coordinator import PipelineBusyError, PipelineCoordinator

logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent


def create_app(
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    coordinator: PipelineCoordinator | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()
    app_settings.ensure_local_directories()
    engine = None
    if session_factory is None:
        engine = build_engine(app_settings.database_url)
        initialize_database(engine, app_settings)
        session_factory = build_session_factory(engine)
    app_coordinator = coordinator or PipelineCoordinator(session_factory, app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        scheduler = None
        if app_settings.scheduler_enabled:
            scheduler = start_scheduler(app_settings, app_coordinator)
            try:
                app_coordinator.submit_pipeline("startup")
            except PipelineBusyError:
                pass
        app.state.scheduler = scheduler
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        app_coordinator.shutdown()
        if engine:
            engine.dispose()

    app = FastAPI(title=app_settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = app_settings
    app.state.session_factory = session_factory
    app.state.coordinator = app_coordinator
    app.state.templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.include_router(api_router)
    app.include_router(web_router)
    return app


app = create_app()
