from pathlib import Path

from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect, text

from alembic import command
from trade_news_analysis import decision_models, research_data_models, risk_models, workflow_models
from trade_news_analysis.config import get_settings
from trade_news_analysis.models import Base


def test_frozen_research_migration_preserves_original_schema_and_events(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE events (id INTEGER PRIMARY KEY, title TEXT)"))
        connection.execute(text("CREATE TABLE securities (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO events(id,title) VALUES (1,'已存在的事件')"))
    try:
        config = Config("alembic.ini")
        command.stamp(config, "e2b7c4a91d60")
        command.upgrade(config, "f9c23a0b617d")
        modules = {
            module.__name__
            for module in (
                decision_models,
                research_data_models,
                risk_models,
                workflow_models,
            )
        }
        inspector = inspect(engine)
        later_tables = {"action_validation_states", "signal_evaluation_audits"}
        later_columns = {
            "research_action_tasks": {"revision", "review_due_manual"},
            "x_research_claims": {"revision"},
        }
        for mapper in Base.registry.mappers:
            if mapper.class_.__module__ in modules:
                table = mapper.local_table
                if table.name in later_tables:
                    continue
                assert set(table.columns.keys()) - later_columns.get(table.name, set()) == {
                    column["name"] for column in inspector.get_columns(table.name)
                }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT title FROM events WHERE id=1")) == "已存在的事件"
            assert connection.scalar(text("SELECT fact_time_verified FROM events WHERE id=1")) == 0
    finally:
        engine.dispose()
        get_settings.cache_clear()
