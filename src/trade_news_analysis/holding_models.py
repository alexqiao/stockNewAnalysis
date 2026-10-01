"""Broker snapshots are separate from the user's investment constraints."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utc_now


class HoldingSyncRun(Base):
    __tablename__ = "holding_sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_key: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str | None] = mapped_column(String(500))


class HoldingSnapshot(Base):
    __tablename__ = "holding_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("holding_sync_runs.id"), unique=True)
    account_key: Mapped[str] = mapped_column(String(64))
    account_label: Mapped[str] = mapped_column(String(40))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str | None] = mapped_column(String(8))
    net_liquidation: Mapped[float | None] = mapped_column(Float)
    cash_balance: Mapped[float | None] = mapped_column(Float)
    settled_cash: Mapped[float | None] = mapped_column(Float)
    available_funds: Mapped[float | None] = mapped_column(Float)
    exchange_rates: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    data_gaps: Mapped[list[str]] = mapped_column(JSON, default=list)


class HoldingPosition(Base):
    __tablename__ = "holding_positions"
    __table_args__ = (UniqueConstraint("snapshot_id", "con_id", name="uq_holding_contract"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("holding_snapshots.id"), index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    con_id: Mapped[int]
    symbol: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160), default="")
    security_type: Mapped[str] = mapped_column(String(20))
    exchange: Mapped[str] = mapped_column(String(40))
    currency: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    average_cost: Mapped[float | None] = mapped_column(Float)
    market_price: Mapped[float | None] = mapped_column(Float)
    market_value: Mapped[float | None] = mapped_column(Float)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float | None] = mapped_column(Float)
    unsupported_reason: Mapped[str | None] = mapped_column(String(160))
