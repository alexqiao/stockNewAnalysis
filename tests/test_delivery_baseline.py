from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from trade_news_analysis.config import Settings
from trade_news_analysis.db import (
    SCHEMA_REVISION,
    SessionFactory,
    build_engine,
    check_database_compatibility,
    initialize_database,
)
from trade_news_analysis.main import create_app
from trade_news_analysis.models import (
    Event,
    EventSecurityImpact,
    IngestionRun,
    Security,
    SourceHealth,
)
from trade_news_analysis.research_data_models import ResearchFinancialFact
from trade_news_analysis.risk_models import (
    ActionDecisionSnapshot,
    ActionEvaluationResult,
    ActionValidationState,
)
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.coordinator import PipelineBusyError, PipelineCoordinator
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.source_health import describe_source

from . import test_decisions_api as api_fixtures
from .test_analysis import add_pending_event, completion

decisions_client = api_fixtures.decisions_client


def test_schema_check_accepts_empty_and_complete_unversioned_database(
    settings: Settings,
) -> None:
    engine = build_engine(settings.database_url)
    try:
        assert check_database_compatibility(engine) == "empty"
        initialize_database(engine, settings)
        assert check_database_compatibility(engine) == "unversioned_compatible"
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"),
                               {"revision": SCHEMA_REVISION})
        assert check_database_compatibility(engine) == "current"
    finally:
        engine.dispose()


@pytest.mark.parametrize("broken", ["column", "revision", "table"])
def test_incompatible_database_stops_before_seed_recovery_or_scheduler(
    settings: Settings, broken: str,
) -> None:
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    with engine.begin() as connection:
        if broken == "column":
            connection.execute(text("ALTER TABLE events DROP COLUMN analysis_stage"))
        elif broken == "table":
            connection.execute(text("DROP TABLE source_health"))
        else:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('old')"))
    before = set(inspect(engine).get_table_names())
    coordinator = Mock(spec=PipelineCoordinator)
    try:
        with pytest.raises(RuntimeError, match="备份数据库"), TestClient(
            create_app(settings, coordinator=coordinator, fundamental_provider=Mock())
        ):
            pass
        coordinator.recover_interrupted.assert_not_called()
        coordinator.submit_pipeline.assert_not_called()
        assert set(inspect(engine).get_table_names()) == before
    finally:
        engine.dispose()


@pytest.mark.parametrize("age,failures,expected", [
    (None, 0, "never_succeeded"), (5, 0, "fresh"), (61, 0, "stale"),
    (5, 1, "failing"), (61, 3, "failing"),
])
def test_source_health_distinguishes_current_availability(
    settings: Settings, age: int | None, failures: int, expected: str,
) -> None:
    now = datetime(2026, 9, 30, 10, tzinfo=UTC)
    source = SourceHealth(
        source="fixture", capability="news", markets=["US"], coverage="broad",
        last_success_at=now - timedelta(minutes=age) if age is not None else None,
        last_attempt_at=now, last_error="unavailable" if failures else None,
        consecutive_failures=failures,
    )
    result = describe_source(source, settings, now)
    assert result["availability"] == expected
    assert bool(result["next_retry_at"]) is (failures >= 3)


def test_health_keeps_historical_coverage_but_degrades_stale_sources(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        source = SourceHealth(
            source="all-markets", capability="news", markets=["US", "HK", "A"],
            coverage="broad", last_success_at=now - timedelta(hours=2),
        )
        session.add(source)
        session.commit()
        identity = source.id
    response = decisions_client.get("/api/v1/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service_status"] == "ok"
    assert payload["status"] == "degraded"
    assert set(payload["historical_market_coverage"].values()) == {"broad"}
    assert set(payload["market_coverage"].values()) == {"partial"}
    with session_factory() as session:
        source = session.get(SourceHealth, identity)
        assert source is not None
        source.last_success_at = now
        session.commit()
    assert decisions_client.get("/api/v1/health").json()["status"] == "ok"


def test_x_health_uses_its_own_six_hour_refresh_interval(settings: Settings) -> None:
    now = datetime.now(UTC)
    source = SourceHealth(
        source="x", capability="x_posts", markets=["US"], coverage="social",
        last_success_at=now - timedelta(hours=2), consecutive_failures=0,
    )
    assert describe_source(source, settings, now)["availability"] == "fresh"


@pytest.mark.parametrize("mode", ["unavailable", "timeout", "success"])
def test_pipeline_counts_actual_analysis_results_and_committed_impacts(
    session_factory: SessionFactory, settings: Settings, mode: str,
) -> None:
    with session_factory() as session:
        event = add_pending_event(session)
        event.updated_at = datetime.now(UTC) - timedelta(days=1)
        session.commit()
    analyzer = EventAnalyzer(settings, completion={
        "timeout": Mock(side_effect=TimeoutError()), "success": completion,
    }.get(mode))
    coordinator = PipelineCoordinator(
        session_factory, settings.model_copy(update={"research_refresh_enabled": False}),
        source_factory=lambda *_args: [], analyzer=analyzer,
        evaluator=Mock(spec=OutcomeEvaluator, evaluate=Mock(return_value=0)),
    )
    coordinator.ingestion.master_factory = lambda _settings: None
    try:
        run_id = coordinator.ingestion.create_run("test")
        coordinator._execute_pipeline(run_id)
        with session_factory() as session:
            run = session.get(IngestionRun, run_id)
            assert run is not None
            assert run.status == ("complete" if mode == "success" else "partial")
            assert run.summary["failed"] == (0 if mode == "success" else 1)
            assert bool(run.errors) is (mode != "success")
            assert run.analyses_created == (1 if mode == "success" else 0)
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize("failing_stage", ["facts", "market"])
def test_committed_facts_invalidate_analysis_even_when_later_refresh_fails(
    session_factory: SessionFactory, settings: Settings, failing_stage: str,
) -> None:
    with session_factory() as session:
        event = add_pending_event(session)
        EventAnalyzer(settings, completion=completion).analyze_event(session, event)
        identity = event.id
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))

    def commit_facts(*_args: object) -> dict[str, object]:
        with session_factory() as session:
            session.add(ResearchFinancialFact(
                security_id=security_id, fingerprint="new-revenue", source="fixture",
                source_url="https://example.test/filing", concept="Revenue", value=100,
                unit="USD", period_start=date(2025, 1, 1), period_end=date(2025, 12, 31),
                available_at=datetime(2026, 1, 1, tzinfo=UTC),
            ))
            session.commit()
        if failing_stage == "facts":
            raise RuntimeError("subsequent source failed")
        return {"sources": []}

    coordinator = PipelineCoordinator(session_factory, settings)
    try:
        run_id = coordinator.ingestion.create_run("research")
        with patch(
            "trade_news_analysis.services.research_data.ResearchDataService.refresh_isolated",
            side_effect=commit_facts,
        ), patch(
            "trade_news_analysis.services.market_research.MarketResearchService.refresh_isolated",
            side_effect=RuntimeError("market failed"),
        ), pytest.raises(RuntimeError):
            coordinator._execute_research(run_id, None)
        with session_factory() as session:
            event = session.get(Event, identity)
            assert event is not None and event.analysis_stale and event.status == "stale"
            assert not session.scalar(select(EventSecurityImpact.id).where(
                EventSecurityImpact.event_id == identity, EventSecurityImpact.is_current.is_(True),
            ))
    finally:
        coordinator.shutdown()


def test_manual_action_retry_runs_when_scheduled_research_refresh_is_disabled(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        snapshot = ActionDecisionSnapshot(
            security_id=security_id, horizon=5, market="US", action_code="observe",
            direction="neutral", policy_version="fixture", dedupe_key="manual-disabled",
        )
        session.add(snapshot)
        session.flush()
        session.add(ActionValidationState(snapshot_id=snapshot.id, status="queued"))
        session.commit()
    coordinator = PipelineCoordinator(
        session_factory, settings.model_copy(update={"research_refresh_enabled": False}),
        evaluator=Mock(spec=OutcomeEvaluator, evaluate=Mock(return_value=0), provider=Mock()),
    )
    try:
        with patch(
            "trade_news_analysis.services.action_evaluation.evaluate_action_snapshots",
            return_value={"completed": 0},
        ) as evaluate:
            coordinator.submit_evaluation().result(5)
            evaluate.assert_called_once()
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize("fail_stage", [None, "analysis", "evaluation"])
def test_pipeline_remains_running_until_last_stage_and_retains_failure_phase(
    settings: Settings, session_factory: SessionFactory, fail_stage: str | None,
) -> None:
    analyzer = Mock(spec=EventAnalyzer)
    evaluator = Mock(spec=OutcomeEvaluator)
    coordinator = PipelineCoordinator(
        session_factory, settings.model_copy(update={"research_refresh_enabled": False}),
        source_factory=lambda *_args: [], analyzer=analyzer, evaluator=evaluator,
    )
    coordinator.ingestion.master_factory = lambda _settings: None
    run_id = coordinator.ingestion.create_run("test")
    observed = []

    def stage(name: str) -> int:
        with session_factory() as reader:
            run = reader.get(IngestionRun, run_id)
            assert run is not None
            observed.append((name, run.status, run.phase, run.completed_at, run.is_terminal))
        if fail_stage == name:
            raise TimeoutError("synthetic")
        return 0

    analyzer.analyze_pending.side_effect = lambda _session: stage("analysis")
    evaluator.evaluate.side_effect = lambda _session: stage("evaluation")
    try:
        if fail_stage:
            with pytest.raises(TimeoutError):
                coordinator._execute_pipeline(run_id)
        else:
            coordinator._execute_pipeline(run_id)
        assert all(row == (row[0], "running", row[0], None, False) for row in observed)
        with session_factory() as session:
            run = session.get(IngestionRun, run_id)
            assert run is not None and run.is_terminal and run.completed_at is not None
            assert run.status == ("failed" if fail_stage else "complete")
            assert run.phase == (fail_stage or "finished")
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize("busy", [False, True])
def test_manual_validation_retry_commits_before_dispatch_and_preserves_queue_when_busy(
    decisions_client: TestClient, session_factory: SessionFactory,
    monkeypatch: pytest.MonkeyPatch, busy: bool,
) -> None:
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        assert security_id is not None
        snapshot = ActionDecisionSnapshot(
            security_id=security_id, horizon=5, market="US", action_code="observe",
            direction="neutral", policy_version="fixture", dedupe_key="manual-retry",
        )
        session.add(snapshot)
        session.commit()
        identity = snapshot.id

    def dispatch() -> None:
        with session_factory() as session:
            state = session.get(ActionValidationState, identity)
            assert state is not None and state.status == "queued"
        if busy:
            raise PipelineBusyError("fixture")

    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_evaluation", dispatch)
    response = decisions_client.post(f"/api/v1/research/validation/{identity}/retry")
    assert response.status_code == 202
    assert response.json()["dispatch"] == ("deferred" if busy else "queued")
    assert decisions_client.post("/api/v1/research/validation/999999/retry").status_code == 404
    with session_factory() as session:
        session.add(ActionEvaluationResult(snapshot_id=identity, payload={"legacy": True}))
        session.commit()
    assert decisions_client.post(f"/api/v1/research/validation/{identity}/retry").status_code == 409
