"""Persistent research checklists and manually reviewed social claims."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base, utc_now


class ActionTask(Base):
    __tablename__ = "research_action_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    horizon: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(40))
    source_kind: Mapped[str] = mapped_column(String(40), default="")
    source_id: Mapped[int | None] = mapped_column(Integer)
    source_version: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    expiration_reason: Mapped[str] = mapped_column(String(40), default="")
    review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    review_due_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    history: Mapped[list[ActionTaskHistory]] = relationship(
        cascade="all, delete-orphan", order_by="ActionTaskHistory.id"
    )


class ActionTaskHistory(Base):
    __tablename__ = "research_action_task_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("research_action_tasks.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class XClaim(Base):
    __tablename__ = "x_research_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    post_id: Mapped[int | None] = mapped_column(
        ForeignKey("x_posts.id", ondelete="SET NULL"), index=True
    )
    author: Mapped[str] = mapped_column(String(64))
    post_url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    claim_text: Mapped[str] = mapped_column(Text)
    claim_kind: Mapped[str] = mapped_column(String(32), default="fact_claim")
    verification_needs: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    source_version: Mapped[str] = mapped_column(String(64))
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    expiration_reason: Mapped[str] = mapped_column(String(40), default="")
    review_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    securities: Mapped[list[XClaimSecurity]] = relationship(cascade="all, delete-orphan")
    evidence: Mapped[list[XClaimEvidence]] = relationship(
        cascade="all, delete-orphan", order_by="XClaimEvidence.id"
    )
    history: Mapped[list[XClaimHistory]] = relationship(
        cascade="all, delete-orphan", order_by="XClaimHistory.id"
    )


class XClaimSecurity(Base):
    __tablename__ = "x_research_claim_securities"
    __table_args__ = (UniqueConstraint("claim_id", "security_id", name="uq_x_claim_security"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        ForeignKey("x_research_claims.id", ondelete="CASCADE"), index=True
    )
    security_id: Mapped[int] = mapped_column(ForeignKey("securities.id"), index=True)
    match_basis: Mapped[str] = mapped_column(String(20))


class XClaimEvidence(Base):
    __tablename__ = "x_research_claim_evidence"
    __table_args__ = (
        UniqueConstraint("claim_id", "source_key", "stance", name="uq_x_claim_evidence_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        ForeignKey("x_research_claims.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    source_key: Mapped[str] = mapped_column(String(64))
    stance: Mapped[str] = mapped_column(String(20))
    is_official: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class XClaimHistory(Base):
    __tablename__ = "x_research_claim_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    claim_id: Mapped[int] = mapped_column(
        ForeignKey("x_research_claims.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
