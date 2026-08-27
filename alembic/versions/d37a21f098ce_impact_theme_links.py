"""link security impacts to their relevant event themes

Revision ID: d37a21f098ce
Revises: b6a4e14b72cd
Create Date: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d37a21f098ce"
down_revision: str | None = "b6a4e14b72cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("event_security_impact_themes"):
        return
    op.create_table(
        "event_security_impact_themes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("impact_id", sa.Integer(), nullable=False),
        sa.Column("theme_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["impact_id"], ["event_security_impacts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["theme_id"], ["themes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "impact_id", "theme_id", name="uq_event_security_impact_theme"
        ),
    )
    op.create_index(
        op.f("ix_event_security_impact_themes_impact_id"),
        "event_security_impact_themes",
        ["impact_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_event_security_impact_themes_theme_id"),
        "event_security_impact_themes",
        ["theme_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("event_security_impact_themes"):
        return
    op.drop_index(
        op.f("ix_event_security_impact_themes_theme_id"),
        table_name="event_security_impact_themes",
    )
    op.drop_index(
        op.f("ix_event_security_impact_themes_impact_id"),
        table_name="event_security_impact_themes",
    )
    op.drop_table("event_security_impact_themes")
