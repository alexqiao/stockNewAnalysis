"""stock first architecture

Revision ID: 7e2c4a9d91ab
Revises: 04adfcf6661f
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op
from trade_news_analysis.models import (
    Event,
    EventArticle,
    EventSecurityImpact,
    EventTheme,
    Security,
    SecuritySignalSnapshot,
    SignalOutcome,
    Theme,
    Watchlist,
)

revision: str = "7e2c4a9d91ab"
down_revision: str | None = "04adfcf6661f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _datetime(value: object, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return fallback


def upgrade() -> None:
    bind = op.get_bind()
    old_watchlist = list(
        bind.execute(
            sa.text(
                "SELECT symbol, company_name, aliases, active, created_at FROM watchlist"
            )
        ).mappings()
    )
    op.drop_table("outcomes")
    op.drop_table("impact_analyses")
    op.drop_table("article_symbols")
    op.drop_table("watchlist")

    for table in (
        Security.__table__,
        Event.__table__,
        Theme.__table__,
        EventArticle.__table__,
        EventTheme.__table__,
        EventSecurityImpact.__table__,
        SecuritySignalSnapshot.__table__,
        SignalOutcome.__table__,
        Watchlist.__table__,
    ):
        table.create(bind, checkfirst=True)

    inspector = sa.inspect(bind)
    source_columns = {item["name"] for item in inspector.get_columns("source_health")}
    with op.batch_alter_table("source_health") as batch:
        if "capability" not in source_columns:
            batch.add_column(
                sa.Column("capability", sa.String(40), nullable=False, server_default="news")
            )
        if "markets" not in source_columns:
            batch.add_column(sa.Column("markets", sa.JSON(), nullable=False, server_default="[]"))
        if "coverage" not in source_columns:
            batch.add_column(
                sa.Column("coverage", sa.String(20), nullable=False, server_default="partial")
            )
    event_columns = {item["name"] for item in sa.inspect(bind).get_columns("events")}
    if "unresolved_candidates" not in event_columns:
        with op.batch_alter_table("events") as batch:
            batch.add_column(
                sa.Column(
                    "unresolved_candidates", sa.JSON(), nullable=False, server_default="[]"
                )
            )

    now = datetime.now(UTC)
    for row in old_watchlist:
        result = bind.execute(
            Security.__table__.insert().values(
                market="US",
                exchange="NASDAQ",
                symbol=row["symbol"],
                name=row["company_name"] or row["symbol"],
                aliases=row["aliases"] or [],
                industry="",
                business_summary="",
                market_cap=None,
                currency="USD",
                timezone="America/New_York",
                calendar="US",
                active=True,
                provider_data={},
                updated_at=now,
            )
        )
        bind.execute(
            Watchlist.__table__.insert().values(
                security_id=result.inserted_primary_key[0],
                active=row["active"],
                created_at=_datetime(row["created_at"], now),
            )
        )

    clusters = list(
        bind.execute(
            sa.text(
                "SELECT story_cluster_id, MIN(id) AS article_id, "
                "MIN(published_at) AS occurred_at FROM articles GROUP BY story_cluster_id"
            )
        ).mappings()
    )
    for cluster in clusters:
        article = bind.execute(
            sa.text("SELECT title, summary FROM articles WHERE id=:id"),
            {"id": cluster["article_id"]},
        ).mappings().one()
        result = bind.execute(
            Event.__table__.insert().values(
                event_key=cluster["story_cluster_id"],
                status="pending",
                title=article["title"],
                summary=article["summary"],
                event_type="",
                observed_demand="",
                occurred_at=_datetime(cluster["occurred_at"], now),
                created_at=now,
                updated_at=now,
                model="",
            )
        )
        article_ids = bind.execute(
            sa.text("SELECT id FROM articles WHERE story_cluster_id=:key"),
            {"key": cluster["story_cluster_id"]},
        ).scalars()
        for article_id in article_ids:
            bind.execute(
                EventArticle.__table__.insert().values(
                    event_id=result.inserted_primary_key[0], article_id=article_id
                )
            )


def downgrade() -> None:
    raise RuntimeError("旧逐新闻分析已按设计废弃，不能无损降级")
