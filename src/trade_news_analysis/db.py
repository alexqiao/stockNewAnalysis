"""Database engine/session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import DEFAULT_COMPANIES, DEFAULT_SECURITY_META, Settings
from .models import Base, Security, Watchlist

SessionFactory = sessionmaker[Session]


def build_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///"):
        db_path = Path(database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs = (
        {"connect_args": {"check_same_thread": False}} if database_url.startswith("sqlite") else {}
    )
    return create_engine(database_url, **kwargs)


def build_session_factory(engine: Engine) -> SessionFactory:
    return sessionmaker(bind=engine, expire_on_commit=False)


def initialize_database(engine: Engine, settings: Settings) -> None:
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        for symbol in settings.seed_symbols:
            security = session.scalar(
                select(Security).where(Security.market == "US", Security.symbol == symbol)
            )
            if security is None:
                name, aliases = DEFAULT_COMPANIES.get(symbol, (symbol, []))
                market, exchange, currency, timezone, calendar = DEFAULT_SECURITY_META.get(
                    symbol, ("US", "UNKNOWN", "USD", "America/New_York", "US")
                )
                security = Security(
                    market=market,
                    exchange=exchange,
                    symbol=symbol,
                    name=name,
                    aliases=aliases,
                    currency=currency,
                    timezone=timezone,
                    calendar=calendar,
                )
                session.add(security)
                session.flush()
            if not session.scalar(select(Watchlist).where(Watchlist.security_id == security.id)):
                session.add(Watchlist(security_id=security.id))
        session.commit()


def session_scope(factory: SessionFactory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
