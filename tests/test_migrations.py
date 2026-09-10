from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import inspect, text

from alembic import command
from trade_news_analysis.config import Settings, get_settings
from trade_news_analysis.db import build_engine, initialize_database


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
        with engine.connect() as connection:
            assert connection.scalar(text("select version_num from alembic_version")) == (
                "4a1c9d8e7f20"
            )
        engine.dispose()
    finally:
        get_settings.cache_clear()
