from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from trade_news_analysis.config import Settings, get_settings
from trade_news_analysis.db import build_engine, initialize_database
from trade_news_analysis.models import PEAnalysisProfile, Security, Watchlist


def test_upgrade_handles_tables_created_before_migrations(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    database_path = tmp_path / "migration.db"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")

    try:
        command.upgrade(config, "c18f4d7a3b20")
        engine = build_engine(database_url)
        initialize_database(
            engine,
            Settings(database_url=database_url, scheduler_enabled=False),
        )
        engine.dispose()

        get_settings.cache_clear()
        command.upgrade(config, "head")

        engine = build_engine(database_url)
        inspector = inspect(engine)
        article_columns = {column["name"] for column in inspector.get_columns("articles")}
        assert {
            "content_kind",
            "evidence_role",
            "author_handle",
            "analysis_eligible",
        } <= article_columns
        event_columns = {column["name"] for column in inspector.get_columns("events")}
        assert {
            "demand_status",
            "evidence_grade",
            "evidence_score",
            "missing_proof",
        } <= event_columns
        watchlist_columns = {
            column["name"] for column in inspector.get_columns("watchlist")
        }
        assert {"position", "holding_status"} <= watchlist_columns
        profile_columns = {
            column["name"] for column in inspector.get_columns("pe_analysis_profiles")
        }
        assert "manual_price_updated_at" in profile_columns
        with engine.connect() as connection:
            assert connection.scalar(text("select version_num from alembic_version")) == (
                "e83f20a7b691"
            )
        engine.dispose()
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("columns_already_exist", [False, True])
def test_holding_status_migration_preserves_watchlist(
    tmp_path: Path, monkeypatch: MonkeyPatch, columns_already_exist: bool
) -> None:
    database_url = f"sqlite:///{tmp_path / 'holdings.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = build_engine(database_url)

    try:
        command.upgrade(config, "c18f4d7a3b20")
        initialize_database(
            engine,
            Settings(
                database_url=database_url,
                scheduler_enabled=False,
                seed_watchlist="AAPL,MSFT",
            ),
        )
        command.upgrade(config, "4a1c9d8e7f20")
        with Session(engine) as session:
            securities = list(
                session.scalars(
                    select(Security)
                    .where(Security.symbol.in_(["AAPL", "MSFT"]))
                    .order_by(Security.symbol)
                )
            )
            session.add_all(
                [
                    Watchlist(
                        security_id=securities[0].id,
                        position=9,
                        active=True,
                        holding_status="long",
                    ),
                    Watchlist(
                        security_id=securities[1].id,
                        position=2,
                        active=False,
                        holding_status="flat",
                    ),
                    PEAnalysisProfile(security_id=securities[0].id, price_override=150),
                ]
            )
            session.commit()

        if not columns_already_exist:
            # Historical revisions import current models; remove the new columns
            # to reproduce a database created before this model change.
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE watchlist DROP COLUMN holding_status"))
                connection.execute(
                    text("ALTER TABLE pe_analysis_profiles DROP COLUMN manual_price_updated_at")
                )

        command.upgrade(config, "head")

        with Session(engine) as session:
            entries = list(session.scalars(select(Watchlist).order_by(Watchlist.position)))
            assert [entry.position for entry in entries] == [2, 9]
            assert [entry.active for entry in entries] == [False, True]
            assert [entry.holding_status for entry in entries] == (
                ["flat", "long"] if columns_already_exist else ["unknown", "unknown"]
            )
            profile = session.scalar(select(PEAnalysisProfile))
            assert profile is not None
            assert profile.price_override == 150
            assert profile.manual_price_updated_at is None
        assert next(
            column
            for column in inspect(engine).get_columns("pe_analysis_profiles")
            if column["name"] == "manual_price_updated_at"
        )["nullable"] is True
    finally:
        engine.dispose()
        get_settings.cache_clear()
