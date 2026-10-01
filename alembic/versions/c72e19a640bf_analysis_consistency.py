"""Analysis retries, screening revision and explicit outcome interpretation."""

import sqlalchemy as sa

from alembic import op

revision = "c72e19a640bf"
down_revision = "a81d90fbc237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    additions = {
        "events": (
            sa.Column("analysis_attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("analysis_stage", sa.String(20), nullable=False, server_default="discovery"),
            sa.Column("analysis_next_retry_at", sa.DateTime(timezone=True), nullable=True),
        ),
        "event_security_impacts": (
            sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        ),
        "x_posts": (
            sa.Column("screening_version", sa.Integer(), nullable=False, server_default="0"),
        ),
        "signal_outcomes": (
            sa.Column(
                "evaluation_version", sa.String(40), nullable=False, server_default="legacy-v1"
            ),
        ),
    }
    inspector = sa.inspect(op.get_bind())
    for table, columns in additions.items():
        if not inspector.has_table(table):
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, columns in {
        "events": ("analysis_attempts", "analysis_stage", "analysis_next_retry_at"),
        "event_security_impacts": ("retryable",),
        "x_posts": ("screening_version",),
        "signal_outcomes": ("evaluation_version",),
    }.items():
        if inspector.has_table(table):
            existing = {column["name"] for column in inspector.get_columns(table)}
            with op.batch_alter_table(table) as batch:
                for column in columns:
                    if column in existing:
                        batch.drop_column(column)
