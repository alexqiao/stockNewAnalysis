"""IBKR account snapshots, positions and sync attempts."""

import sqlalchemy as sa

from alembic import op

revision = "a81d90fbc237"
down_revision = "f9c23a0b617d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("holding_sync_runs"):
        op.create_table(
            "holding_sync_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("account_key", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True)),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("error", sa.String(500)),
        )
    if not sa.inspect(bind).has_table("holding_snapshots"):
        op.create_table(
            "holding_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "run_id",
                sa.Integer(),
                sa.ForeignKey("holding_sync_runs.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column("account_key", sa.String(64), nullable=False),
            sa.Column("account_label", sa.String(40), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("currency", sa.String(8)),
            sa.Column("net_liquidation", sa.Float()),
            sa.Column("cash_balance", sa.Float()),
            sa.Column("settled_cash", sa.Float()),
            sa.Column("available_funds", sa.Float()),
            sa.Column("exchange_rates", sa.JSON(), nullable=False),
            sa.Column("data_gaps", sa.JSON(), nullable=False),
        )
    if not sa.inspect(bind).has_table("holding_positions"):
        op.create_table(
            "holding_positions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "snapshot_id", sa.Integer(), sa.ForeignKey("holding_snapshots.id"), nullable=False
            ),
            sa.Column("security_id", sa.Integer(), sa.ForeignKey("securities.id")),
            sa.Column("con_id", sa.Integer(), nullable=False),
            sa.Column("symbol", sa.String(80), nullable=False),
            sa.Column("name", sa.String(160), nullable=False),
            sa.Column("security_type", sa.String(20), nullable=False),
            sa.Column("exchange", sa.String(40), nullable=False),
            sa.Column("currency", sa.String(8), nullable=False),
            sa.Column("quantity", sa.Float(), nullable=False),
            sa.Column("average_cost", sa.Float()),
            sa.Column("market_price", sa.Float()),
            sa.Column("market_value", sa.Float()),
            sa.Column("unrealized_pnl", sa.Float()),
            sa.Column("weight", sa.Float()),
            sa.Column("unsupported_reason", sa.String(160)),
            sa.UniqueConstraint("snapshot_id", "con_id", name="uq_holding_contract"),
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("holding_positions")}
    for field in ("snapshot_id", "security_id"):
        name = f"ix_holding_positions_{field}"
        if name not in indexes:
            op.create_index(name, "holding_positions", [field])


def downgrade() -> None:
    for table in ("holding_positions", "holding_snapshots", "holding_sync_runs"):
        op.drop_table(table)
