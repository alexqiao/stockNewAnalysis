"""Explicit user risk inputs and versioned market/action research records."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from .models import Base, utc_now


class PortfolioRiskProfile(Base):
    __tablename__ = "portfolio_risk_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    total_value: Mapped[float | None] = mapped_column(Float)
    available_cash: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SecurityRiskProfile(Base):
    __tablename__ = "security_risk_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), unique=True, index=True
    )
    current_weight: Mapped[float | None] = mapped_column(Float)
    current_quantity: Mapped[float | None] = mapped_column(Float)
    average_cost: Mapped[float | None] = mapped_column(Float)
    max_weight: Mapped[float | None] = mapped_column(Float)
    risk_budget_pct: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    sector_limit_pct: Mapped[float | None] = mapped_column(Float)
    sector_current_weight: Mapped[float | None] = mapped_column(Float)
    lot_size: Mapped[int | None] = mapped_column(Integer)
    max_participation_pct: Mapped[float | None] = mapped_column(Float)
    fee_bps: Mapped[float | None] = mapped_column(Float)
    slippage_bps: Mapped[float | None] = mapped_column(Float)
    benchmark_market: Mapped[str | None] = mapped_column(String(8))
    benchmark_symbol: Mapped[str | None] = mapped_column(String(24))
    benchmark_currency: Mapped[str | None] = mapped_column(String(8))
    benchmark_label: Mapped[str | None] = mapped_column(String(160))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MarketResearchSnapshot(Base):
    __tablename__ = "market_research_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActionDecisionSnapshot(Base):
    __tablename__ = "action_decision_snapshots"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_action_decision_dedupe"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    horizon: Mapped[int] = mapped_column(Integer, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    policy_version: Mapped[str] = mapped_column(String(100), index=True)
    market: Mapped[str] = mapped_column(String(8), index=True)
    action_code: Mapped[str] = mapped_column(String(40), index=True)
    direction: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float)
    dedupe_key: Mapped[str] = mapped_column(String(64))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActionEvaluationResult(Base):
    __tablename__ = "action_evaluation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("action_decision_snapshots.id", ondelete="CASCADE"), unique=True, index=True
    )
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ActionValidationState(Base):
    __tablename__ = "action_validation_states"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("action_decision_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32), default="unassessed", index=True)
    reason: Mapped[str] = mapped_column(String(300), default="尚未检查")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    missing_dates: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class SignalEvaluationAudit(Base):
    __tablename__ = "signal_evaluation_audits"

    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("security_signal_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    evaluation_version: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


@event.listens_for(SignalEvaluationAudit, "before_update")
def _immutable_signal_audit(
    _mapper: Mapper[Any], _connection: Connection, _target: SignalEvaluationAudit,
) -> None:
    raise ValueError("信号验证输入不可修改，请保留原始验证记录")


@event.listens_for(ActionDecisionSnapshot, "before_update")
def _immutable_action(
    _mapper: Mapper[Any], _connection: Connection, _target: ActionDecisionSnapshot
) -> None:
    raise ValueError("行动快照不可修改，请记录新版本")


@event.listens_for(ActionEvaluationResult, "before_update")
def _immutable_outcome(
    _mapper: Mapper[Any], _connection: Connection, _target: ActionEvaluationResult
) -> None:
    raise ValueError("行动验证结果不可修改，请保留原始验证记录")
