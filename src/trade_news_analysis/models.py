"""SQLAlchemy persistence models for event-centric market research."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Article(Base):
    """Raw evidence. Articles are never the primary research conclusion."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    canonical_url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    story_cluster_id: Mapped[str] = mapped_column(String(64), index=True)
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    event_links: Mapped[list[EventArticle]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class Security(Base):
    """A market-qualified security; a bare ticker is never globally unique."""

    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("market", "exchange", "symbol", name="uq_security_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    market: Mapped[str] = mapped_column(String(8), index=True)
    exchange: Mapped[str] = mapped_column(String(20), index=True)
    symbol: Mapped[str] = mapped_column(String(24), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    industry: Mapped[str] = mapped_column(String(160), default="", index=True)
    business_summary: Mapped[str] = mapped_column(Text, default="")
    market_cap: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    calendar: Mapped[str] = mapped_column(String(32), default="US")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    impacts: Mapped[list[EventSecurityImpact]] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[SecuritySignalSnapshot]] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )
    watchlist_entry: Mapped[Watchlist | None] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )
    pe_analysis_profile: Mapped[PEAnalysisProfile | None] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )


class Event(Base):
    """A canonical market event backed by one or more articles."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    event_type: Mapped[str] = mapped_column(String(80), default="")
    observed_demand: Mapped[str] = mapped_column(Text, default="")
    unresolved_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    model: Mapped[str] = mapped_column(String(120), default="")
    raw_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    article_links: Mapped[list[EventArticle]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    theme_links: Mapped[list[EventTheme]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )
    impacts: Mapped[list[EventSecurityImpact]] = relationship(
        back_populates="event", cascade="all, delete-orphan"
    )


class EventArticle(Base):
    __tablename__ = "event_articles"
    __table_args__ = (UniqueConstraint("event_id", "article_id", name="uq_event_article"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )

    event: Mapped[Event] = relationship(back_populates="article_links")
    article: Mapped[Article] = relationship(back_populates="event_links")


class Theme(Base):
    __tablename__ = "themes"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event_links: Mapped[list[EventTheme]] = relationship(
        back_populates="theme", cascade="all, delete-orphan"
    )
    impact_links: Mapped[list[EventSecurityImpactTheme]] = relationship(
        back_populates="theme", cascade="all, delete-orphan"
    )


class EventTheme(Base):
    __tablename__ = "event_themes"
    __table_args__ = (UniqueConstraint("event_id", "theme_id", name="uq_event_theme"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    theme_id: Mapped[int] = mapped_column(ForeignKey("themes.id", ondelete="CASCADE"), index=True)

    event: Mapped[Event] = relationship(back_populates="theme_links")
    theme: Mapped[Theme] = relationship(back_populates="event_links")


class EventSecurityImpact(Base):
    """A verifiable event-to-security transmission hypothesis."""

    __tablename__ = "event_security_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    chain_level: Mapped[int] = mapped_column(Integer, default=1)
    impacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    demand_certainty: Mapped[float] = mapped_column(Float, default=0)
    transmission_clarity: Mapped[float] = mapped_column(Float, default=0)
    business_purity: Mapped[float] = mapped_column(Float, default=0)
    scale_elasticity: Mapped[float] = mapped_column(Float, default=0)
    market_neglect: Mapped[float] = mapped_column(Float, default=0)
    novelty_unpriced: Mapped[float] = mapped_column(Float, default=0)
    evidence_quality: Mapped[float] = mapped_column(Float, default=0)
    verification_speed: Mapped[float] = mapped_column(Float, default=0)
    risk_penalty: Mapped[float] = mapped_column(Float, default=0)
    opportunity_score: Mapped[float] = mapped_column(Float, default=0, index=True)
    financial_channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    thesis: Mapped[str] = mapped_column(Text, default="")
    catalysts: Mapped[list[str]] = mapped_column(JSON, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    falsifiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    event: Mapped[Event] = relationship(back_populates="impacts")
    security: Mapped[Security] = relationship(back_populates="impacts")
    theme_links: Mapped[list[EventSecurityImpactTheme]] = relationship(
        back_populates="impact", cascade="all, delete-orphan"
    )


class EventSecurityImpactTheme(Base):
    """A theme explicitly assigned to one event-to-security transmission."""

    __tablename__ = "event_security_impact_themes"
    __table_args__ = (
        UniqueConstraint("impact_id", "theme_id", name="uq_event_security_impact_theme"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    impact_id: Mapped[int] = mapped_column(
        ForeignKey("event_security_impacts.id", ondelete="CASCADE"), index=True
    )
    theme_id: Mapped[int] = mapped_column(
        ForeignKey("themes.id", ondelete="CASCADE"), index=True
    )

    impact: Mapped[EventSecurityImpact] = relationship(back_populates="theme_links")
    theme: Mapped[Theme] = relationship(back_populates="impact_links")


class SecuritySignalSnapshot(Base):
    __tablename__ = "security_signal_snapshots"
    __table_args__ = (
        UniqueConstraint("security_id", "as_of", "horizon", name="uq_security_snapshot"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), index=True
    )
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    horizon: Mapped[int] = mapped_column(Integer, index=True)
    score: Mapped[float] = mapped_column(Float, default=0, index=True)
    direction: Mapped[str] = mapped_column(String(10), default="neutral", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    conflict: Mapped[float] = mapped_column(Float, default=0)
    rank: Mapped[int | None] = mapped_column(Integer)
    evidence_event_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    security: Mapped[Security] = relationship(back_populates="snapshots")
    outcome: Mapped[SignalOutcome | None] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("security_signal_snapshots.id", ondelete="CASCADE"), unique=True, index=True
    )
    baseline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    benchmark_entry: Mapped[float] = mapped_column(Float)
    benchmark_exit: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)
    benchmark_return_pct: Mapped[float] = mapped_column(Float)
    excess_return_pct: Mapped[float] = mapped_column(Float)
    predicted_direction: Mapped[str] = mapped_column(String(10))
    actual_direction: Mapped[str] = mapped_column(String(10))
    correct: Mapped[bool] = mapped_column(Boolean)
    limit_up_hit: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    snapshot: Mapped[SecuritySignalSnapshot] = relationship(back_populates="outcome")


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), unique=True, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    security: Mapped[Security] = relationship(back_populates="watchlist_entry")


class PEAnalysisProfile(Base):
    __tablename__ = "pe_analysis_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), unique=True, index=True
    )
    source_fiscal_year: Mapped[int | None] = mapped_column(Integer)
    source_price: Mapped[float | None] = mapped_column(Float)
    source_market_cap: Mapped[float | None] = mapped_column(Float)
    source_shares_outstanding: Mapped[float | None] = mapped_column(Float)
    source_revenue: Mapped[float | None] = mapped_column(Float)
    source_net_income: Mapped[float | None] = mapped_column(Float)
    fiscal_year_override: Mapped[int | None] = mapped_column(Integer)
    price_override: Mapped[float | None] = mapped_column(Float)
    shares_outstanding_override: Mapped[float | None] = mapped_column(Float)
    revenue_override: Mapped[float | None] = mapped_column(Float)
    net_income_override: Mapped[float | None] = mapped_column(Float)
    assumptions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_name: Mapped[str] = mapped_column(String(80), default="investormate/yfinance")
    source_status: Mapped[str] = mapped_column(String(20), default="uninitialized")
    source_error: Mapped[str | None] = mapped_column(Text)
    source_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    security: Mapped[Security] = relationship(back_populates="pe_analysis_profile")


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(20), default="manual")
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    articles_seen: Mapped[int] = mapped_column(Integer, default=0)
    articles_new: Mapped[int] = mapped_column(Integer, default=0)
    analyses_created: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list)


class SourceHealth(Base):
    __tablename__ = "source_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    capability: Mapped[str] = mapped_column(String(40), default="news")
    markets: Mapped[list[str]] = mapped_column(JSON, default=list)
    coverage: Mapped[str] = mapped_column(String(20), default="partial")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    items_last_run: Mapped[int] = mapped_column(Integer, default=0)
