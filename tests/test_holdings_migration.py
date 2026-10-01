from pathlib import Path

from alembic.config import Config
from pytest import MonkeyPatch
from sqlalchemy import create_engine, inspect, text

from alembic import command
from trade_news_analysis.config import get_settings
from trade_news_analysis.holding_models import HoldingPosition, HoldingSnapshot, HoldingSyncRun


def test_holdings_migration_preserves_existing_data_and_has_all_columns(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    url = f"sqlite:///{tmp_path / 'holdings.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, "f9c23a0b617d")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO portfolio_risk_profiles(id,total_value,currency,updated_at) "
                    "VALUES (1,100000,'USD',CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "head")
        for model in (HoldingSyncRun, HoldingSnapshot, HoldingPosition):
            assert set(model.__table__.columns.keys()) == {
                column["name"] for column in inspect(engine).get_columns(model.__tablename__)
            }
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT total_value FROM portfolio_risk_profiles")) == 100000
            )
        command.downgrade(config, "f9c23a0b617d")
        assert not inspect(engine).has_table("holding_snapshots")
        assert inspect(engine).has_table("portfolio_risk_profiles")
        command.upgrade(config, "head")
        assert inspect(engine).has_table("holding_snapshots")
    finally:
        engine.dispose()
        get_settings.cache_clear()
