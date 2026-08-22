"""Database engine/session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from .config import DEFAULT_COMPANIES, Settings
from .models import Base, Watchlist

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
        existing = set(session.scalars(select(Watchlist.symbol)).all())
        for symbol in settings.seed_symbols:
            if symbol not in existing:
                name, aliases = DEFAULT_COMPANIES.get(symbol, ("", []))
                session.add(Watchlist(symbol=symbol, company_name=name, aliases=aliases))
        session.commit()


def session_scope(factory: SessionFactory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
