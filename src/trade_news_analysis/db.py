"""Database engine/session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from . import (  # noqa: F401
    daily_bar_models,
    decision_models,
    holding_models,
    research_data_models,
    risk_models,
    workflow_models,
)
from .config import (
    DEFAULT_COMPANIES,
    DEFAULT_INDUSTRIES,
    DEFAULT_SECURITY_META,
    DEFAULT_X_ACCOUNTS,
    OPPORTUNITY_ASSETS,
    Settings,
)
from .models import Base, Security, Watchlist, XAccount

SessionFactory = sessionmaker[Session]
SCHEMA_REVISION = "e83f20a7b691"


def check_database_compatibility(engine: Engine) -> str:
    """Inspect before any create_all, seed, recovery or scheduler writes."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables or tables == {"alembic_version"}:
        if "alembic_version" in tables:
            with engine.connect() as connection:
                if connection.scalar(text("SELECT version_num FROM alembic_version")):
                    raise RuntimeError("数据库版本存在但业务表缺失，请从备份恢复后再启动。")
        return "empty"
    missing = []
    for name, table in Base.metadata.tables.items():
        if name not in tables:
            missing.append(name)
        else:
            columns = {column["name"] for column in inspector.get_columns(name)}
            missing.extend(f"{name}.{column.name}" for column in table.columns
                           if column.name not in columns)
    revisions: list[str] = []
    if "alembic_version" in tables:
        with engine.connect() as connection:
            revisions = list(connection.scalars(text("SELECT version_num FROM alembic_version")))
    if missing or (revisions and revisions != [SCHEMA_REVISION]):
        detail = "、".join(missing[:5]) if missing else "迁移版本与当前程序不一致"
        raise RuntimeError(
            f"数据库结构不兼容（{detail}）。请停止旧服务并备份数据库，执行 "
            "uv run --no-sync alembic upgrade head 后重新启动；程序不会自动迁移。"
        )
    return "current" if revisions else "unversioned_compatible"


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
    should_seed_watchlist = not inspect(engine).has_table(Watchlist.__tablename__)
    should_seed_x_accounts = not inspect(engine).has_table(XAccount.__tablename__)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        for position, symbol in enumerate(settings.seed_symbols):
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
                    industry=DEFAULT_INDUSTRIES.get(symbol, ""),
                    currency=currency,
                    timezone=timezone,
                    calendar=calendar,
                )
                session.add(security)
                session.flush()
            elif not security.industry and symbol in DEFAULT_INDUSTRIES:
                security.industry = DEFAULT_INDUSTRIES[symbol]
            if should_seed_watchlist and not session.scalar(
                select(Watchlist).where(Watchlist.security_id == security.id)
            ):
                session.add(Watchlist(security_id=security.id, position=position))
        for asset in OPPORTUNITY_ASSETS:
            security = session.scalar(
                select(Security).where(
                    Security.market == "US", Security.symbol == asset.symbol
                )
            )
            if security is None:
                security = Security(
                    market="US",
                    exchange=asset.exchange,
                    symbol=asset.symbol,
                    name=asset.name,
                    aliases=list(asset.aliases),
                    industry=asset.group,
                    currency="USD",
                    timezone="America/New_York",
                    calendar="US",
                )
                session.add(security)
            else:
                security.aliases = sorted(set([*(security.aliases or []), *asset.aliases]))
                security.industry = security.industry or asset.group
            security.provider_data = {
                **(security.provider_data or {}),
                "research_asset": True,
                "opportunity_group": asset.group,
                "opportunity_scope": asset.scope,
            }
        if should_seed_x_accounts:
            for position, handle in enumerate(DEFAULT_X_ACCOUNTS):
                session.add(
                    XAccount(
                        handle=handle,
                        display_name=handle,
                        account_type="commentator",
                        priority=position,
                    )
                )
        session.commit()


def session_scope(factory: SessionFactory) -> Iterator[Session]:
    session = factory()
    try:
        yield session
    finally:
        session.close()
