"""Versioned research inputs, kept separate from generated investment judgments."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utc_now


class ResearchFinancialFact(Base):
    __tablename__ = "research_financial_facts"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    concept: Mapped[str] = mapped_column(String(160), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(40))
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    fiscal_period: Mapped[str] = mapped_column(String(24), default="")
    accession: Mapped[str] = mapped_column(String(80), default="")
    form: Mapped[str] = mapped_column(String(24), default="")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ResearchDisclosure(Base):
    __tablename__ = "research_disclosures"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    form: Mapped[str] = mapped_column(String(24), default="")
    accession: Mapped[str] = mapped_column(String(80), default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    content_status: Mapped[str] = mapped_column(String(24), default="link_only")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class ResearchCalendarRevision(Base):
    __tablename__ = "research_calendar_revisions"
    __table_args__ = (
        UniqueConstraint("event_key", "revision", name="uq_research_calendar_revision"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    event_key: Mapped[str] = mapped_column(String(160), index=True)
    revision: Mapped[int] = mapped_column()
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    scheduled_date: Mapped[date] = mapped_column(Date, index=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64))
    time_precision: Mapped[str] = mapped_column(String(24), default="date")
    status: Mapped[str] = mapped_column(String(24), default="scheduled")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ResearchExpectation(Base):
    __tablename__ = "research_expectations"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    event_key: Mapped[str] = mapped_column(String(160), index=True)
    calendar_revision_id: Mapped[int] = mapped_column(ForeignKey("research_calendar_revisions.id"))
    metric: Mapped[str] = mapped_column(String(80))
    financial_period: Mapped[str] = mapped_column(String(64), default="")
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(80))
    source_url: Mapped[str] = mapped_column(Text)
    estimate_kind: Mapped[str] = mapped_column(String(40), default="provider_estimate")
    is_pre_release: Mapped[bool] = mapped_column(Boolean, default=False)
    expected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class ResearchSourceState(Base):
    __tablename__ = "research_source_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    security_id: Mapped[int | None] = mapped_column(ForeignKey("securities.id"), index=True)
    capability: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="missing")
    coverage: Mapped[str] = mapped_column(String(80), default="partial")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_last_run: Mapped[int] = mapped_column(default=0)
    message: Mapped[str] = mapped_column(Text, default="")
