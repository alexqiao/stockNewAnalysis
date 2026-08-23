"""persist watchlist display order

Revision ID: c18f4d7a3b20
Revises: 7e2c4a9d91ab
Create Date: 2026-08-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c18f4d7a3b20"
down_revision: str | None = "7e2c4a9d91ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("watchlist")}
    if "position" not in columns:
        op.add_column(
            "watchlist",
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        )
    item_ids = bind.execute(
        sa.text("SELECT id FROM watchlist ORDER BY created_at, id")
    ).scalars()
    for position, item_id in enumerate(item_ids):
        bind.execute(
            sa.text("UPDATE watchlist SET position=:position WHERE id=:id"),
            {"position": position, "id": item_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("watchlist")}
    if "position" in columns:
        with op.batch_alter_table("watchlist") as batch:
            batch.drop_column("position")
