"""Versioned analysis, bounded validation, editing conflicts and task phases."""

import sqlalchemy as sa

from alembic import op

revision = "e83f20a7b691"
down_revision = "d46b7c90a125"
branch_labels = None
depends_on = None


def _add(table: str, *columns: sa.Column) -> None:
    existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add("articles", sa.Column("original_source_url", sa.Text(), nullable=True))
    _add("events",
         sa.Column("evidence_version", sa.Integer(), nullable=False, server_default="0"),
         sa.Column("analyzed_evidence_version", sa.Integer(), nullable=True),
         sa.Column("analysis_stale", sa.Boolean(), nullable=False, server_default="0"),
         sa.Column("analysis_stale_reason", sa.Text(), nullable=True))
    _add("event_security_impacts",
         sa.Column("evidence_version", sa.Integer(), nullable=True),
         sa.Column("research_inputs", sa.JSON(), nullable=False, server_default="{}"),
         sa.Column("evidence_rule_version", sa.String(40), nullable=False,
                   server_default="legacy-v1"))
    _add("ingestion_runs",
         sa.Column("phase", sa.String(32), nullable=False, server_default="queued"),
         sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"))
    _add("research_action_tasks",
         sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
         sa.Column("review_due_manual", sa.Boolean(), nullable=False, server_default="0"))
    _add("x_research_claims",
         sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    _add("daily_bar_sync_states", sa.Column("source_version", sa.String(64), nullable=True))
    op.execute(sa.text(
        "UPDATE ingestion_runs SET phase='finished' "
        "WHERE status IN ('complete','completed','partial','failed') AND phase='queued'"
    ))
    # Existing deadlines are commitments; do not reinterpret their historical provenance.
    op.execute(sa.text("UPDATE research_action_tasks SET review_due_manual=1"))
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "daily_bar_revisions" not in tables:
        op.create_table(
            "daily_bar_revisions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("security_id", sa.Integer(),
                      sa.ForeignKey("securities.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source", sa.String(24), nullable=False),
            sa.Column("source_version", sa.String(64), nullable=False),
            sa.Column("previous_version", sa.String(64), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("start_date", sa.Date(), nullable=False),
            sa.Column("end_date", sa.Date(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("changes", sa.JSON(), nullable=False),
            sa.UniqueConstraint("security_id", "source_version", name="uq_daily_bar_revision"),
        )
        op.create_index("ix_daily_bar_revisions_security_id", "daily_bar_revisions",
                        ["security_id"])
    if "action_validation_states" not in tables:
        op.create_table(
            "action_validation_states",
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey(
                "action_decision_snapshots.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("reason", sa.String(300), nullable=False),
            sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("window_start", sa.Date(), nullable=True),
            sa.Column("window_end", sa.Date(), nullable=True),
            sa.Column("missing_dates", sa.JSON(), nullable=False),
            sa.Column("missing_fields", sa.JSON(), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_action_validation_states_status", "action_validation_states",
                        ["status"])
        op.create_index("ix_action_validation_states_next_attempt_at", "action_validation_states",
                        ["next_attempt_at"])
    if "signal_evaluation_audits" not in tables:
        op.create_table(
            "signal_evaluation_audits",
            sa.Column("snapshot_id", sa.Integer(), sa.ForeignKey(
                "security_signal_snapshots.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("evaluation_version", sa.String(40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )


def downgrade() -> None:
    for table in ("signal_evaluation_audits", "action_validation_states", "daily_bar_revisions"):
        if sa.inspect(op.get_bind()).has_table(table):
            op.drop_table(table)
    for table, columns in (
        ("daily_bar_sync_states", ["source_version"]),
        ("x_research_claims", ["revision"]),
        ("research_action_tasks", ["review_due_manual", "revision"]),
        ("ingestion_runs", ["summary", "phase"]),
        ("event_security_impacts", ["evidence_rule_version", "research_inputs",
                                    "evidence_version"]),
        ("events", ["analysis_stale_reason", "analysis_stale", "analyzed_evidence_version",
                    "evidence_version"]),
        ("articles", ["original_source_url"]),
    ):
        existing = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
        for column in columns:
            if column in existing:
                op.drop_column(table, column)
