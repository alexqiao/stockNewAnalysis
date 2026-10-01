"""Independent daily prices and synchronization state."""

import sqlalchemy as sa

from alembic import op

revision = "d46b7c90a125"
down_revision = "c72e19a640bf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("daily_bars"):
        op.create_table(
            "daily_bars",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("security_id", sa.Integer(), nullable=False),
            sa.Column("trade_date", sa.Date(), nullable=False),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("open", sa.Float(), nullable=False),
            sa.Column("high", sa.Float(), nullable=False),
            sa.Column("low", sa.Float(), nullable=False),
            sa.Column("close", sa.Float(), nullable=False),
            sa.Column("volume", sa.Float(), nullable=True),
            sa.Column("amount", sa.Float(), nullable=True),
            sa.Column("adj_close", sa.Float(), nullable=True),
            sa.Column("dividends", sa.Float(), nullable=False),
            sa.Column("stock_splits", sa.Float(), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("security_id", "trade_date", "source", name="uq_daily_bar_source"),
        )
    if "ix_daily_bars_security_id" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("daily_bars")
    }:
        op.create_index("ix_daily_bars_security_id", "daily_bars", ["security_id"])
    if not inspector.has_table("daily_bar_sync_states"):
        op.create_table(
            "daily_bar_sync_states",
            sa.Column("security_id", sa.Integer(), primary_key=True),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("currency", sa.String(8), nullable=True),
            sa.Column("timezone", sa.String(64), nullable=True),
            sa.Column("coverage_start", sa.Date(), nullable=True),
            sa.Column("coverage_end", sa.Date(), nullable=True),
            sa.Column("checked_through", sa.Date(), nullable=True),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_full_refresh_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("failure_count", sa.Integer(), nullable=False),
            sa.Column("error", sa.String(500), nullable=True),
            sa.Column("error_kind", sa.String(32), nullable=True),
            sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("daily_bar_sync_states", "daily_bars"):
        if inspector.has_table(table):
            op.drop_table(table)
