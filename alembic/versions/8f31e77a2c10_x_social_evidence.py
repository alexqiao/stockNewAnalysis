"""add curated X accounts and social evidence

Revision ID: 8f31e77a2c10
Revises: d37a21f098ce
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "8f31e77a2c10"
down_revision: str | None = "d37a21f098ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_HANDLES = (
    "joely7758521",
    "JasonZX",
    "jimmyhuli",
    "Money_or_Life_X",
    "darrencao2024",
    "xiaomustock",
    "tychozzz",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    article_columns = {column["name"] for column in inspector.get_columns("articles")}
    article_indexes = {index["name"] for index in inspector.get_indexes("articles")}
    desired_columns = {
        "content_kind": sa.Column(
            "content_kind", sa.String(length=20), nullable=False, server_default="news"
        ),
        "evidence_role": sa.Column(
            "evidence_role", sa.String(length=30), nullable=False, server_default="reporting"
        ),
        "author_handle": sa.Column("author_handle", sa.String(length=64), nullable=True),
        "analysis_eligible": sa.Column(
            "analysis_eligible", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    }
    desired_indexes = {
        "ix_articles_content_kind": "content_kind",
        "ix_articles_evidence_role": "evidence_role",
        "ix_articles_author_handle": "author_handle",
        "ix_articles_analysis_eligible": "analysis_eligible",
    }
    if (
        not desired_columns.keys() <= article_columns
        or not desired_indexes.keys() <= article_indexes
    ):
        with op.batch_alter_table("articles") as batch:
            for name, column in desired_columns.items():
                if name not in article_columns:
                    batch.add_column(column)
            for index_name, column_name in desired_indexes.items():
                if index_name not in article_indexes:
                    batch.create_index(index_name, [column_name])

    created_x_accounts = not inspector.has_table("x_accounts")
    if created_x_accounts:
        op.create_table(
            "x_accounts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("handle", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column("account_type", sa.String(length=20), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_x_accounts_handle", "x_accounts", ["handle"], unique=True)
        op.create_index("ix_x_accounts_account_type", "x_accounts", ["account_type"])
        op.create_index("ix_x_accounts_active", "x_accounts", ["active"])

    if not inspector.has_table("x_posts"):
        op.create_table(
            "x_posts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("post_id", sa.String(length=32), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("post_type", sa.String(length=20), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("quoted_post_id", sa.String(length=32), nullable=True),
            sa.Column("quoted_author_handle", sa.String(length=64), nullable=True),
            sa.Column("quoted_text", sa.Text(), nullable=False),
            sa.Column("external_links", sa.JSON(), nullable=False),
            sa.Column("media", sa.JSON(), nullable=False),
            sa.Column("public_metrics", sa.JSON(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("screening_status", sa.String(length=20), nullable=False),
            sa.Column("screening", sa.JSON(), nullable=False),
            sa.Column("promoted_article_id", sa.Integer(), nullable=True),
            sa.Column("related_event_id", sa.Integer(), nullable=True),
            sa.Column("raw_data", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["x_accounts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["promoted_article_id"], ["articles.id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(["related_event_id"], ["events.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_x_posts_account_id", "x_posts", ["account_id"])
        op.create_index("ix_x_posts_post_id", "x_posts", ["post_id"], unique=True)
        op.create_index("ix_x_posts_post_type", "x_posts", ["post_type"])
        op.create_index("ix_x_posts_published_at", "x_posts", ["published_at"])
        op.create_index("ix_x_posts_screening_status", "x_posts", ["screening_status"])
        op.create_index(
            "ix_x_posts_promoted_article_id",
            "x_posts",
            ["promoted_article_id"],
            unique=True,
        )
        op.create_index("ix_x_posts_related_event_id", "x_posts", ["related_event_id"])

    accounts = sa.table(
        "x_accounts",
        sa.column("handle", sa.String),
        sa.column("display_name", sa.String),
        sa.column("account_type", sa.String),
        sa.column("tags", sa.JSON),
        sa.column("priority", sa.Integer),
        sa.column("active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    if created_x_accounts:
        op.bulk_insert(
            accounts,
            [
                {
                    "handle": handle,
                    "display_name": handle,
                    "account_type": "commentator",
                    "tags": [],
                    "priority": position,
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                }
                for position, handle in enumerate(DEFAULT_HANDLES)
            ],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("x_posts"):
        op.drop_table("x_posts")
    if inspector.has_table("x_accounts"):
        op.drop_table("x_accounts")

    article_columns = {column["name"] for column in inspector.get_columns("articles")}
    article_indexes = {index["name"] for index in inspector.get_indexes("articles")}
    desired_indexes = {
        "ix_articles_analysis_eligible",
        "ix_articles_author_handle",
        "ix_articles_evidence_role",
        "ix_articles_content_kind",
    }
    desired_columns = {
        "analysis_eligible",
        "author_handle",
        "evidence_role",
        "content_kind",
    }
    if desired_indexes & article_indexes or desired_columns & article_columns:
        with op.batch_alter_table("articles") as batch:
            for index_name in desired_indexes & article_indexes:
                batch.drop_index(index_name)
            for column_name in desired_columns & article_columns:
                batch.drop_column(column_name)
