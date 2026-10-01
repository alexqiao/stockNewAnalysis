"""Persistent daily prices, independent of research snapshots."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, String, UniqueConstraint, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from .models import Base, utc_now


class DailyBar(Base):
    __tablename__ = "daily_bars"
    __table_args__ = (
        UniqueConstraint("security_id", "trade_date", "source", name="uq_daily_bar_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    trade_date: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(24), default="yfinance")
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    adj_close: Mapped[float | None] = mapped_column(Float)
    dividends: Mapped[float] = mapped_column(Float, default=0)
    stock_splits: Mapped[float] = mapped_column(Float, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DailyBarSyncState(Base):
    __tablename__ = "daily_bar_sync_states"

    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True
    )
    source: Mapped[str] = mapped_column(String(24), default="yfinance")
    currency: Mapped[str | None] = mapped_column(String(8))
    timezone: Mapped[str | None] = mapped_column(String(64))
    coverage_start: Mapped[date | None] = mapped_column(Date)
    coverage_end: Mapped[date | None] = mapped_column(Date)
    checked_through: Mapped[date | None] = mapped_column(Date)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_full_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default="pending")
    failure_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(String(500))
    error_kind: Mapped[str | None] = mapped_column(String(32))
    source_version: Mapped[str | None] = mapped_column(String(64))


class DailyBarRevision(Base):
    """Append-only differences; old prices survive corrections to the current cache."""

    __tablename__ = "daily_bar_revisions"
    __table_args__ = (
        UniqueConstraint("security_id", "source_version", name="uq_daily_bar_revision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(24))
    source_version: Mapped[str] = mapped_column(String(64))
    previous_version: Mapped[str | None] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


@event.listens_for(DailyBarRevision, "before_update")
def _immutable_revision(
    _mapper: Mapper[Any], _connection: Connection, _target: DailyBarRevision,
) -> None:
    raise ValueError("历史行情修订不可修改，请追加新版本")
