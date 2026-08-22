from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import (
    SessionFactory,
    build_engine,
    build_session_factory,
    initialize_database,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=False,
        seed_watchlist="AAPL,MSFT",
        auto_analyze=True,
    )


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[SessionFactory]:
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    factory = build_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def session(session_factory: SessionFactory) -> Iterator[Session]:
    with session_factory() as db_session:
        yield db_session
