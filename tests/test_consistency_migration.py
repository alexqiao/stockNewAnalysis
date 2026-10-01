from pathlib import Path

import pytest
from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect, text

from alembic import command
from trade_news_analysis.config import get_settings


@pytest.mark.parametrize("columns_already_exist", [False, True])
def test_consistency_migration_preserves_historical_rows_and_is_additive(
    tmp_path: Path, monkeypatch: MonkeyPatch, columns_already_exist: bool,
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy-consistency.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    additions = {
        "events": (
            "analysis_attempts INTEGER NOT NULL DEFAULT 0, "
            "analysis_stage VARCHAR(20) NOT NULL DEFAULT 'discovery', "
            "analysis_next_retry_at DATETIME"
        ),
        "event_security_impacts": "retryable BOOLEAN NOT NULL DEFAULT 0",
        "x_posts": "screening_version INTEGER NOT NULL DEFAULT 0",
        "signal_outcomes": "evaluation_version VARCHAR(40) NOT NULL DEFAULT 'legacy-v1'",
    }
    with engine.begin() as connection:
        for table, fields in additions.items():
            columns = f", {fields}" if columns_already_exist else ""
            connection.execute(text(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, saved_value TEXT{columns})"
            ))
            connection.execute(text(f"INSERT INTO {table}(id,saved_value) VALUES (1,'untouched')"))
    config = Config("alembic.ini")
    try:
        command.stamp(config, "a81d90fbc237")
        command.upgrade(config, "c72e19a640bf")
        with engine.connect() as connection:
            for table in additions:
                assert connection.scalar(text(f"SELECT saved_value FROM {table}")) == "untouched"
            assert connection.scalar(text("SELECT analysis_attempts FROM events")) == 0
            assert connection.scalar(text("SELECT analysis_stage FROM events")) == "discovery"
            assert connection.scalar(text("SELECT analysis_next_retry_at FROM events")) is None
            assert connection.scalar(text("SELECT retryable FROM event_security_impacts")) == 0
            assert connection.scalar(text("SELECT screening_version FROM x_posts")) == 0
            assert connection.scalar(
                text("SELECT evaluation_version FROM signal_outcomes")
            ) == "legacy-v1"
        command.downgrade(config, "a81d90fbc237")
        assert {column["name"] for column in inspect(engine).get_columns("events")} == {
            "id", "saved_value",
        }
        command.upgrade(config, "c72e19a640bf")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT saved_value FROM signal_outcomes")) == "untouched"
    finally:
        engine.dispose()
        get_settings.cache_clear()
