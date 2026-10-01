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
from .daily_bars_api import router as daily_bars_router
from .db import (
    SessionFactory,
    build_engine,
    build_session_factory,
    check_database_compatibility,
    initialize_database,
)
from .holdings_api import router as holdings_router
from .research_api import router as research_router
from .scheduler import start_scheduler
from .services.coordinator import PipelineBusyError, PipelineCoordinator
from .services.holdings import HoldingService
from .services.providers import FundamentalDataProvider, InvestorMateFundamentalProvider

logger = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent


def create_app(
    settings: Settings | None = None,
    session_factory: SessionFactory | None = None,
    coordinator: PipelineCoordinator | None = None,
    fundamental_provider: FundamentalDataProvider | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app_settings = settings or get_settings()
        engine = None
        app_coordinator = coordinator
        scheduler = None
        try:
            app_settings.ensure_local_directories()
            app.title = app_settings.app_name
            factory = session_factory
            if factory is None:
                engine = build_engine(app_settings.database_url)
                check_database_compatibility(engine)
                initialize_database(engine, app_settings)
                factory = build_session_factory(engine)
            app_coordinator = app_coordinator or PipelineCoordinator(factory, app_settings)
            holdings = HoldingService(factory)
            app_coordinator.recover_interrupted()
            holdings.recover_interrupted()
            app.state.settings = app_settings
            app.state.session_factory = factory
            app.state.coordinator = app_coordinator
            app.state.holdings = holdings
            app.state.fundamental_provider = (
                fundamental_provider or InvestorMateFundamentalProvider()
            )
            if app_settings.scheduler_enabled:
                scheduler = start_scheduler(app_settings, app_coordinator)
                try:
                    app_coordinator.submit_pipeline("startup")
                except PipelineBusyError:
                    pass
            app.state.scheduler = scheduler
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=True)
            if app_coordinator:
                app_coordinator.shutdown()
            if engine:
                engine.dispose()

    app = FastAPI(
        title=settings.app_name if settings else "金融新闻影响分析平台",
        version=__version__, lifespan=lifespan,
    )
    app.state.templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")
    app.include_router(api_router)
    app.include_router(web_router)
    app.include_router(research_router)
    app.include_router(holdings_router)
    app.include_router(daily_bars_router)
    return app


app = create_app()
