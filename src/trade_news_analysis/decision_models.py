"""Append-only audit of manually verified fact timing."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, utc_now


class EventTimingRevision(Base):
    __tablename__ = "event_timing_revisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MacroObservationVintage(Base):
    __tablename__ = "macro_observation_vintages"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    series: Mapped[str] = mapped_column(String(80), index=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(40))
    source_url: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    metadata_values: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
