from __future__ import annotations

from pathlib import Path

from sqlalchemy import delete, select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import (
    SessionFactory,
    build_engine,
    build_session_factory,
    initialize_database,
)
from trade_news_analysis.models import Security, Watchlist


def _watchlist_rows(factory: SessionFactory) -> list[tuple[str, bool, int]]:
    with factory() as session:
        return list(
            session.execute(
                select(Security.symbol, Watchlist.active, Watchlist.position)
                .join(Watchlist, Watchlist.security_id == Security.id)
                .order_by(Watchlist.position, Watchlist.id)
            ).tuples()
        )


def test_initialize_database_seeds_watchlist_only_once(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'persistent.db'}",
        scheduler_enabled=False,
        seed_watchlist="AAPL,MSFT",
    )
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    factory = build_session_factory(engine)
    assert _watchlist_rows(factory) == [("AAPL", True, 0), ("MSFT", True, 1)]
    with factory() as session:
        research_assets = session.scalars(
            select(Security).where(Security.symbol.in_({"GLD", "GOVT"}))
        ).all()
        assert {item.symbol for item in research_assets} == {"GLD", "GOVT"}
        assert {item.provider_data["opportunity_group"] for item in research_assets} == {
            "黄金",
            "美债",
        }

    with factory() as session:
        session.execute(delete(Watchlist))
        microsoft = session.scalar(select(Security).where(Security.symbol == "MSFT"))
        assert microsoft is not None
        session.add(Watchlist(security_id=microsoft.id, active=False, position=7))
        session.commit()

    initialize_database(engine, settings)
    assert _watchlist_rows(factory) == [("MSFT", False, 7)]

    with factory() as session:
        session.execute(delete(Watchlist))
        session.commit()

    initialize_database(engine, settings)
    assert _watchlist_rows(factory) == []
    engine.dispose()
