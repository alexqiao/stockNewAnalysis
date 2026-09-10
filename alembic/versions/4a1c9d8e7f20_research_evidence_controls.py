"""add structured research evidence controls

Revision ID: 4a1c9d8e7f20
Revises: 8f31e77a2c10
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4a1c9d8e7f20"
down_revision: str | None = "8f31e77a2c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

THEME_ALIASES = {
    "AI芯片": "AI计算芯片",
    "AI算力芯片": "AI计算芯片",
    "AI加速计算芯片": "AI计算芯片",
    "AI数据中心GPU": "数据中心GPU",
    "AI算力资本开支": "AI数据中心资本开支",
    "AI基础设施资本开支": "AI数据中心资本开支",
    "AI算力基础设施": "AI数据中心基础设施",
    "数据中心基础设施": "AI数据中心基础设施",
}


def _merge_theme_aliases(bind: sa.Connection) -> None:
    themes = sa.table(
        "themes",
        sa.column("id", sa.Integer),
        sa.column("slug", sa.String),
        sa.column("name", sa.String),
    )
    event_themes = sa.table(
        "event_themes",
        sa.column("event_id", sa.Integer),
        sa.column("theme_id", sa.Integer),
    )
    impact_themes = sa.table(
        "event_security_impact_themes",
        sa.column("impact_id", sa.Integer),
        sa.column("theme_id", sa.Integer),
    )
    for alias, canonical in THEME_ALIASES.items():
        alias_ids = list(
            bind.scalars(sa.select(themes.c.id).where(themes.c.name == alias))
        )
        if not alias_ids:
            continue
        canonical_id = bind.scalar(
            sa.select(themes.c.id).where(themes.c.name == canonical).limit(1)
        )
        if canonical_id is None:
            canonical_id = alias_ids.pop(0)
            bind.execute(
                sa.update(themes)
                .where(themes.c.id == canonical_id)
                .values(name=canonical, slug=canonical.casefold())
            )
        for alias_id in alias_ids:
            for table, owner_column in (
                (event_themes, event_themes.c.event_id),
                (impact_themes, impact_themes.c.impact_id),
            ):
                owners = list(
                    bind.scalars(
                        sa.select(owner_column).where(table.c.theme_id == alias_id)
                    )
                )
                for owner_id in owners:
                    exists = bind.scalar(
                        sa.select(sa.literal(1))
                        .where(
                            owner_column == owner_id,
                            table.c.theme_id == canonical_id,
                        )
                        .limit(1)
                    )
                    if exists is None:
                        bind.execute(
                            sa.insert(table).values(
                                {owner_column.name: owner_id, "theme_id": canonical_id}
                            )
                        )
                bind.execute(sa.delete(table).where(table.c.theme_id == alias_id))
            bind.execute(sa.delete(themes).where(themes.c.id == alias_id))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("events")}
    indexes = {index["name"] for index in inspector.get_indexes("events")}
    with op.batch_alter_table("events") as batch:
        if "demand_status" not in columns:
            batch.add_column(
                sa.Column(
                    "demand_status",
                    sa.String(length=20),
                    nullable=False,
                    server_default="unknown",
                )
            )
        if "evidence_grade" not in columns:
            batch.add_column(
                sa.Column(
                    "evidence_grade",
                    sa.String(length=20),
                    nullable=False,
                    server_default="none",
                )
            )
        if "evidence_score" not in columns:
            batch.add_column(
                sa.Column(
                    "evidence_score", sa.Float(), nullable=False, server_default="0"
                )
            )
        if "missing_proof" not in columns:
            batch.add_column(
                sa.Column(
                    "missing_proof", sa.JSON(), nullable=False, server_default="[]"
                )
            )
        if "ix_events_demand_status" not in indexes:
            batch.create_index("ix_events_demand_status", ["demand_status"])
        if "ix_events_evidence_grade" not in indexes:
            batch.create_index("ix_events_evidence_grade", ["evidence_grade"])
    bind.execute(
        sa.text(
            "UPDATE events SET demand_status = 'narrative_only' "
            "WHERE observed_demand LIKE '%仅有叙事%'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE event_security_impacts SET is_current = false "
            "WHERE event_id IN ("
            "SELECT id FROM events WHERE demand_status = 'narrative_only'"
            ")"
        )
    )
    if inspector.has_table("event_security_impact_themes"):
        _merge_theme_aliases(bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("events")}
    indexes = {index["name"] for index in inspector.get_indexes("events")}
    with op.batch_alter_table("events") as batch:
        for index_name in (
            "ix_events_evidence_grade",
            "ix_events_demand_status",
        ):
            if index_name in indexes:
                batch.drop_index(index_name)
        for column_name in (
            "missing_proof",
            "evidence_score",
            "evidence_grade",
            "demand_status",
        ):
            if column_name in columns:
                batch.drop_column(column_name)
