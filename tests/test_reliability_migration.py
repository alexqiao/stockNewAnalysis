from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import (
    JSON,
    Boolean,
    Connection,
    Date,
    DateTime,
    Engine,
    Float,
    Integer,
    MetaData,
    Table,
    create_engine,
    inspect,
    text,
)

from alembic import command
from trade_news_analysis.config import get_settings
from trade_news_analysis.db import SCHEMA_REVISION, check_database_compatibility
from trade_news_analysis.models import Base

PREVIOUS_REVISION = "d46b7c90a125"
NEW_COLUMNS = {
    "articles": {"original_source_url"},
    "events": {
        "evidence_version", "analyzed_evidence_version", "analysis_stale", "analysis_stale_reason",
    },
    "event_security_impacts": {"evidence_version", "research_inputs", "evidence_rule_version"},
    "ingestion_runs": {"phase", "summary"},
    "research_action_tasks": {"revision", "review_due_manual"},
    "x_research_claims": {"revision"},
    "daily_bar_sync_states": {"source_version"},
}
NEW_TABLES = {"daily_bar_revisions", "action_validation_states", "signal_evaluation_audits"}
STAMP = datetime(2026, 9, 15, 20, tzinfo=UTC)


def _strip_new_schema(engine: Engine) -> None:
    # Earlier revisions import current ORM tables. Remove their future fields so
    # this test exercises ALTER TABLE against a genuinely old physical schema.
    with engine.begin() as connection:
        for table in sorted(NEW_TABLES):
            if inspect(connection).has_table(table):
                connection.execute(text(f'DROP TABLE "{table}"'))
        for table, columns in NEW_COLUMNS.items():
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for column in sorted(columns & existing):
                connection.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}"'))


def _insert(connection: Connection, table_name: str, **values: Any) -> None:
    table = Table(table_name, MetaData(), autoload_with=connection)
    defaults: dict[str, Any] = {}
    for column in table.columns:
        if column.nullable or column.primary_key or column.name in values:
            continue
        kind = column.type
        if isinstance(kind, JSON):
            defaults[column.name] = {}
        elif isinstance(kind, Boolean):
            defaults[column.name] = False
        elif isinstance(kind, DateTime):
            defaults[column.name] = STAMP
        elif isinstance(kind, Date):
            defaults[column.name] = STAMP.date()
        elif isinstance(kind, Integer | Float):
            defaults[column.name] = 0
        else:
            defaults[column.name] = ""
    connection.execute(table.insert().values(**{**defaults, **values}))


def _seed_business_rows(engine: Engine, *, with_new_columns: bool) -> None:
    with engine.begin() as connection:
        _insert(connection, "securities", id=1, market="US", exchange="NASDAQ", symbol="KEEP",
                name="保留证券", currency="USD", timezone="America/New_York", active=True)
        _insert(connection, "articles", id=1, fingerprint="a" * 64, source="Wire",
                canonical_url="https://example.com/retained", title="保留报道",
                story_cluster_id="retain", analysis_eligible=True)
        _insert(connection, "events", id=1, event_key="retain", title="历史判断",
                status="complete", summary="历史依据", evidence_score=4.5,
                **({"evidence_version": 8, "analyzed_evidence_version": 7,
                    "analysis_stale": True, "analysis_stale_reason": "待人工重评"}
                   if with_new_columns else {}))
        _insert(connection, "event_security_impacts", id=1, event_id=1, security_id=1,
                status="complete", is_current=True, thesis="不可丢失的旧结论", opportunity_score=68,
                **({"evidence_version": 7, "research_inputs": {"saved": "original input"},
                    "evidence_rule_version": "original-sources-v2"}
                   if with_new_columns else {}))
        statuses = ("complete", "completed", "partial", "failed", "running")
        for run_id, status in enumerate(statuses, 1):
            _insert(connection, "ingestion_runs", id=run_id, trigger="migration-test",
                    status=status, articles_seen=12, articles_new=3,
                    **({"phase": "queued", "summary": {"preserved": True}}
                       if with_new_columns else {}))
        _insert(connection, "research_action_tasks", id=1, task_key="preserved-task", security_id=1,
                horizon=5, kind="evidence", status="pending", note="保留人工复核期限和备注",
                review_due_at=datetime(2026, 10, 31, 9, tzinfo=UTC),
                **({"revision": 4, "review_due_manual": True} if with_new_columns else {}))
        _insert(connection, "x_research_claims", id=1, claim_key="preserved-claim", author="author",
                claim_text="保留主张", post_url="https://example.com/post", status="refuted",
                note="已核实反证", **({"revision": 6} if with_new_columns else {}))
        _insert(connection, "daily_bar_sync_states", security_id=1, status="success",
                source="yfinance", currency="USD",
                **({"source_version": "existing-version"} if with_new_columns else {}))
        _insert(connection, "action_decision_snapshots", id=1, security_id=1, horizon=5,
                policy_version="old-policy", market="US", action_code="reduce", direction="bearish",
                dedupe_key="original-dedupe",
                inputs={"recording_version": "legacy-v1", "price": 100})
        _insert(connection, "action_evaluation_results", id=1, snapshot_id=1,
                payload={"net_long_return_pct": -3.5, "interpretation": "historical"})
        _insert(connection, "security_signal_snapshots", id=1, security_id=1, horizon=5,
                score=-50, direction="bearish", components={"source": "immutable-history"})
        _insert(connection, "signal_outcomes", id=1, snapshot_id=1, entry_price=100, exit_price=95,
                benchmark_entry=100, benchmark_exit=99, return_pct=-5, benchmark_return_pct=-1,
                excess_return_pct=-4, predicted_direction="bearish", actual_direction="bearish",
                correct=True, evaluation_version="legacy-v1")


def _stored_rows(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    tables = {*NEW_COLUMNS, "securities", "action_decision_snapshots", "action_evaluation_results",
              "security_signal_snapshots", "signal_outcomes"}
    with engine.connect() as connection:
        return {
            table: [dict(row) for row in connection.execute(
                text(f'SELECT * FROM "{table}" ORDER BY 1')
            ).mappings()]
            for table in tables
        }


def _assert_old_values_unchanged(
    engine: Engine, before: dict[str, list[dict[str, Any]]],
) -> None:
    after = _stored_rows(engine)
    for table, rows in before.items():
        assert len(after[table]) == len(rows)
        for old, current in zip(rows, after[table], strict=True):
            assert {key: value for key, value in current.items() if key in old} == old


@pytest.mark.parametrize("columns_already_exist", [False, True])
def test_reliability_upgrade_preserves_business_data_and_matches_current_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, columns_already_exist: bool,
) -> None:
    url = f"sqlite:///{tmp_path / 'reliability.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config("alembic.ini")
    engine = create_engine(url)
    try:
        command.upgrade(config, PREVIOUS_REVISION)
        if columns_already_exist:
            command.upgrade(config, "head")
            command.stamp(config, PREVIOUS_REVISION)
        else:
            _strip_new_schema(engine)
            for table, columns in NEW_COLUMNS.items():
                assert not columns.intersection(
                    column["name"] for column in inspect(engine).get_columns(table)
                )
        _seed_business_rows(engine, with_new_columns=columns_already_exist)
        before = _stored_rows(engine)
        # Only explicit migration additions/backfills may change; capture all old columns.
        for table, rows in before.items():
            for row in rows:
                for column in NEW_COLUMNS.get(table, set()):
                    row.pop(column, None)
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            check_database_compatibility(engine)

        command.upgrade(config, "head")
        assert check_database_compatibility(engine) == "current"
        _assert_old_values_unchanged(engine, before)
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            assert set(table.columns.keys()) == {
                column["name"] for column in inspector.get_columns(name)
            }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                SCHEMA_REVISION
            )
            assert connection.scalar(text(
                "SELECT review_due_manual FROM research_action_tasks"
            )) == 1
            assert connection.scalar(text("SELECT revision FROM research_action_tasks")) == (
                4 if columns_already_exist else 1
            )
            assert connection.scalar(text("SELECT revision FROM x_research_claims")) == (
                6 if columns_already_exist else 1
            )
            assert list(connection.scalars(text(
                "SELECT phase FROM ingestion_runs ORDER BY id"
            ))) == [
                "finished", "finished", "finished", "finished", "queued",
            ]
            assert connection.scalar(text("SELECT evidence_version FROM events")) == (
                8 if columns_already_exist else 0
            )
            assert connection.scalar(text("SELECT analysis_stale FROM events")) == (
                1 if columns_already_exist else 0
            )
            assert connection.scalar(text("SELECT analyzed_evidence_version FROM events")) == (
                7 if columns_already_exist else None
            )
            assert connection.scalar(text("SELECT source_version FROM daily_bar_sync_states")) == (
                "existing-version" if columns_already_exist else None
            )
            assert connection.scalar(text(
                "SELECT evidence_rule_version FROM event_security_impacts"
            )) == ("original-sources-v2" if columns_already_exist else "legacy-v1")
            impact_table = Table("event_security_impacts", MetaData(), autoload_with=connection)
            assert connection.scalar(impact_table.select().with_only_columns(
                impact_table.c.research_inputs
            )) == ({"saved": "original input"} if columns_already_exist else {})
        with engine.begin() as connection:
            columns = [column["name"] for column in inspect(connection).get_columns(
                "research_action_tasks"
            ) if column["name"] not in {"review_due_manual", "revision"}]
            names = ", ".join(f'"{name}"' for name in columns)
            expressions = ", ".join(
                "2" if name == "id" else "'new-task'" if name == "task_key" else f'"{name}"'
                for name in columns
            )
            connection.execute(text(
                f"INSERT INTO research_action_tasks ({names}) SELECT {expressions} "
                "FROM research_action_tasks WHERE id=1"
            ))
            assert connection.execute(text(
                "SELECT revision, review_due_manual FROM research_action_tasks WHERE id=2"
            )).one() == (1, 0)
            connection.execute(text("DELETE FROM research_action_tasks WHERE id=2"))

        command.downgrade(config, PREVIOUS_REVISION)
        assert not NEW_TABLES.intersection(inspect(engine).get_table_names())
        for table, columns in NEW_COLUMNS.items():
            assert not columns.intersection(
                column["name"] for column in inspect(engine).get_columns(table)
            )
        _assert_old_values_unchanged(engine, before)
        with pytest.raises(RuntimeError, match="alembic upgrade head"):
            check_database_compatibility(engine)
        command.upgrade(config, "head")
        assert check_database_compatibility(engine) == "current"
        _assert_old_values_unchanged(engine, before)
    finally:
        engine.dispose()
        get_settings.cache_clear()
