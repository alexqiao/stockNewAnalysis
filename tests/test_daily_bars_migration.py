from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from trade_news_analysis.config import get_settings
from trade_news_analysis.daily_bar_models import DailyBar, DailyBarSyncState
from trade_news_analysis.models import Base, Security
from trade_news_analysis.risk_models import MarketResearchSnapshot


@pytest.mark.parametrize("created_by_metadata", [False, True])
def test_daily_bars_upgrade_preserves_existing_data_and_matches_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, created_by_metadata: bool,
) -> None:
    url = f"sqlite:///{tmp_path / 'daily-migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = create_engine(url)
    config = Config("alembic.ini")
    stamp = datetime(2026, 9, 15, 20, tzinfo=UTC)
    try:
        command.upgrade(config, "c72e19a640bf")
        if created_by_metadata:
            Base.metadata.create_all(engine)
        with Session(engine) as session:
            security = Security(
                market="US", exchange="NASDAQ", symbol="MIGRATION", name="保留证券",
            )
            session.add(security)
            session.flush()
            security_id = security.id
            session.add(MarketResearchSnapshot(
                security_id=security_id, as_of=stamp, status="ready",
                payload={"bars": [{"date": "2026-09-15", "close": 123.0}]},
            ))
            if created_by_metadata:
                session.add(DailyBar(
                    security_id=security_id, trade_date=stamp.date(), source="yfinance",
                    open=120.0, high=124.0, low=119.0, close=123.0, volume=1000,
                    adj_close=123.0, dividends=0, stock_splits=0, fetched_at=stamp,
                ))
                session.add(DailyBarSyncState(
                    security_id=security_id, status="success", coverage_end=stamp.date(),
                    last_success_at=stamp,
                ))
            session.commit()

        command.upgrade(config, "d46b7c90a125")
        inspector = inspect(engine)
        for model in (DailyBar, DailyBarSyncState):
            expected_columns = set(model.__table__.columns.keys())
            if model is DailyBarSyncState and not created_by_metadata:
                expected_columns.remove("source_version")  # Added by e83, after this revision.
            assert expected_columns == {
                column["name"] for column in inspector.get_columns(model.__tablename__)
            }
        assert any(index["column_names"] == ["security_id"]
                   for index in inspector.get_indexes("daily_bars"))
        assert any(constraint["column_names"] == ["security_id", "trade_date", "source"]
                   for constraint in inspector.get_unique_constraints("daily_bars"))
        with Session(engine) as session:
            saved = session.get(Security, security_id)
            assert saved is not None and saved.name == "保留证券"
            snapshot = session.scalar(select(MarketResearchSnapshot).where(
                MarketResearchSnapshot.security_id == security_id,
            ))
            assert snapshot is not None
            assert snapshot.payload == {"bars": [{"date": "2026-09-15", "close": 123.0}]}
            bars = list(session.scalars(select(DailyBar)))
            assert len(bars) == int(created_by_metadata)
            if created_by_metadata:
                assert bars[0].close == 123.0
                state = session.get(DailyBarSyncState, security_id)
                assert state is not None and state.status == "success"
            assert session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "d46b7c90a125"
            )

        with Session(engine) as session:
            session.add(DailyBar(
                security_id=security_id, trade_date=date(2026, 9, 16), source="yfinance",
                open=10, high=11, low=9, close=10, fetched_at=stamp,
            ))
            session.commit()
            session.add(DailyBar(
                security_id=security_id, trade_date=date(2026, 9, 16), source="yfinance",
                open=20, high=21, low=19, close=20, fetched_at=stamp,
            ))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
            saved_price = session.scalar(select(DailyBar.close).where(
                DailyBar.security_id == security_id, DailyBar.trade_date == date(2026, 9, 16),
            ))
            assert saved_price == 10

        command.downgrade(config, "c72e19a640bf")
        assert not inspect(engine).has_table("daily_bars")
        assert not inspect(engine).has_table("daily_bar_sync_states")
        with Session(engine) as session:
            assert session.get(Security, security_id) is not None
            assert session.scalar(select(MarketResearchSnapshot.id)) is not None
        command.upgrade(config, "d46b7c90a125")
        assert inspect(engine).has_table("daily_bars")
    finally:
        engine.dispose()
        get_settings.cache_clear()
