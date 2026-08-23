"""add persistent PE analysis profiles

Revision ID: b6a4e14b72cd
Revises: c18f4d7a3b20
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6a4e14b72cd"
down_revision: str | None = "c18f4d7a3b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pe_analysis_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("security_id", sa.Integer(), nullable=False),
        sa.Column("source_fiscal_year", sa.Integer(), nullable=True),
        sa.Column("source_price", sa.Float(), nullable=True),
        sa.Column("source_market_cap", sa.Float(), nullable=True),
        sa.Column("source_shares_outstanding", sa.Float(), nullable=True),
        sa.Column("source_revenue", sa.Float(), nullable=True),
        sa.Column("source_net_income", sa.Float(), nullable=True),
        sa.Column("fiscal_year_override", sa.Integer(), nullable=True),
        sa.Column("price_override", sa.Float(), nullable=True),
        sa.Column("shares_outstanding_override", sa.Float(), nullable=True),
        sa.Column("revenue_override", sa.Float(), nullable=True),
        sa.Column("net_income_override", sa.Float(), nullable=True),
        sa.Column("assumptions", sa.JSON(), nullable=False),
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column("source_status", sa.String(length=20), nullable=False),
        sa.Column("source_error", sa.Text(), nullable=True),
        sa.Column("source_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pe_analysis_profiles_security_id"),
        "pe_analysis_profiles",
        ["security_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_pe_analysis_profiles_security_id"),
        table_name="pe_analysis_profiles",
    )
    op.drop_table("pe_analysis_profiles")
