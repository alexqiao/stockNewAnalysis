"""persist holdings and manual price confirmation time

Revision ID: e2b7c4a91d60
Revises: 4a1c9d8e7f20
Create Date: 2026-09-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b7c4a91d60"
down_revision: str | None = "4a1c9d8e7f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    watchlist_columns = {
        column["name"] for column in inspector.get_columns("watchlist")
    }
    if "holding_status" not in watchlist_columns:
        op.add_column(
            "watchlist",
            sa.Column(
                "holding_status", sa.String(20), nullable=False, server_default="unknown"
            ),
        )
    profile_columns = {
        column["name"] for column in inspector.get_columns("pe_analysis_profiles")
    }
    if "manual_price_updated_at" not in profile_columns:
        op.add_column(
            "pe_analysis_profiles",
            sa.Column("manual_price_updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table_name, column_name in (
        ("pe_analysis_profiles", "manual_price_updated_at"),
        ("watchlist", "holding_status"),
    ):
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if column_name in columns:
            with op.batch_alter_table(table_name) as batch:
                batch.drop_column(column_name)
