"""SQLAlchemy persistence models."""

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

    symbols: Mapped[list[ArticleSymbol]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )


class ArticleSymbol(Base):
    __tablename__ = "article_symbols"
    __table_args__ = (UniqueConstraint("article_id", "symbol", name="uq_article_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    match_method: Mapped[str] = mapped_column(String(30), default="source_query")
    in_title: Mapped[bool] = mapped_column(Boolean, default=False)

    article: Mapped[Article] = relationship(back_populates="symbols")
    analyses: Mapped[list[ImpactAnalysis]] = relationship(
        back_populates="article_symbol", cascade="all, delete-orphan"
    )


class ImpactAnalysis(Base):
    __tablename__ = "impact_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_symbol_id: Mapped[int] = mapped_column(
        ForeignKey("article_symbols.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    model: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    event_type: Mapped[str | None] = mapped_column(String(80))
    novelty: Mapped[float | None] = mapped_column(Float)
    priced_in: Mapped[float | None] = mapped_column(Float)
    impacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    financial_channels: Mapped[list[str]] = mapped_column(JSON, default=list)
    observed_demand: Mapped[str | None] = mapped_column(Text)
    thesis: Mapped[str | None] = mapped_column(Text)
    catalysts: Mapped[list[str]] = mapped_column(JSON, default=list)
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    falsifiers: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw_response: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    article_symbol: Mapped[ArticleSymbol] = relationship(back_populates="analyses")
    outcomes: Mapped[list[Outcome]] = relationship(
        back_populates="analysis", cascade="all, delete-orphan"
    )


class Outcome(Base):
    __tablename__ = "outcomes"
    __table_args__ = (UniqueConstraint("analysis_id", "horizon", name="uq_analysis_horizon"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("impact_analyses.id", ondelete="CASCADE"), index=True
    )
    horizon: Mapped[int] = mapped_column(Integer, index=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    analysis: Mapped[ImpactAnalysis] = relationship(back_populates="outcomes")


class Watchlist(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(160), default="")
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


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
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    items_last_run: Mapped[int] = mapped_column(Integer, default=0)
