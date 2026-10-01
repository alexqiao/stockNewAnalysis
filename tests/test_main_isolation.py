from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event as ThreadEvent
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.holding_models import HoldingSyncRun
from trade_news_analysis.main import create_app
from trade_news_analysis.models import Event, IngestionRun
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.coordinator import PipelineBusyError, PipelineCoordinator


def test_import_and_factory_do_not_read_settings_or_create_database(tmp_path: Path) -> None:
    script = """
from pathlib import Path
from trade_news_analysis import config
def forbidden():
    raise AssertionError('settings must not be read on import')
config.get_settings = forbidden
from trade_news_analysis.main import app, create_app
create_app()
assert not Path('database.db').exists()
assert not Path('data').exists()
"""
    env = {**os.environ, "DATABASE_URL": "sqlite:///database.db"}
    subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, check=True)


def test_database_initialization_occurs_only_during_lifespan(
    settings: Settings, tmp_path: Path,
) -> None:
    path = tmp_path / "startup" / "app.db"
    app = create_app(settings.model_copy(update={"database_url": f"sqlite:///{path}"}))
    assert not path.exists()
    with TestClient(app) as client:
        assert path.exists()
        assert client.get("/api/v1/watchlist").status_code == 200


def test_startup_recovers_only_unfinished_runs(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    statuses = ["queued", "running", "complete", "completed", "partial", "failed"]
    with session_factory() as session:
        for status in statuses:
            session.add(IngestionRun(trigger="test", status=status, errors=["saved detail"]))
        session.add(HoldingSyncRun(account_key="synthetic"))
        session.commit()
    coordinator = PipelineCoordinator(
        session_factory, settings, source_factory=lambda _securities, _settings: [],
    )
    app = create_app(settings, session_factory, coordinator)
    with session_factory() as session:
        actual = list(session.scalars(select(IngestionRun.status).order_by(IngestionRun.id)))
        assert actual == statuses
    with TestClient(app):
        with session_factory() as session:
            runs = list(session.scalars(select(IngestionRun).order_by(IngestionRun.id)))
            assert [run.status for run in runs] == ["failed", "failed", *statuses[2:]]
            assert all(run.completed_at is not None for run in runs[:2])
            assert all("服务重启" in run.errors[-1] for run in runs[:2])
            assert all(run.errors == ["saved detail"] for run in runs[2:])
            assert session.scalar(select(HoldingSyncRun.status)) == "failed"
        assert coordinator.busy is False
        coordinator.submit_pipeline("test")
        coordinator.wait_for_pipeline()


def test_lifespan_cleans_up_when_startup_fails(
    settings: Settings, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    from trade_news_analysis import main

    coordinator = Mock(spec=PipelineCoordinator)
    coordinator.recover_interrupted.side_effect = RuntimeError("startup failed")
    provider = Mock()
    monkeypatch.setattr(main, "InvestorMateFundamentalProvider", provider)
    with pytest.raises(RuntimeError, match="startup failed"), TestClient(
        create_app(settings, session_factory, coordinator)
    ):
        pass
    coordinator.shutdown.assert_called_once()
    provider.assert_not_called()


@pytest.mark.parametrize("status,attempts,delay,due", [
    ("error", 1, -1, True), ("partial", 2, -1, True),
    ("error", 3, -1, False), ("error", 1, 60, False),
    ("complete", 1, -1, False), ("pending", 0, -1, False),
])
def test_retry_worker_only_queues_due_unfinished_analysis(
    settings: Settings, session_factory: SessionFactory,
    status: str, attempts: int, delay: int, due: bool,
) -> None:
    with session_factory() as session:
        session.add(Event(
            event_key="due-analysis", title="Synthetic event", status=status,
            analysis_attempts=attempts,
            analysis_next_retry_at=datetime.now(UTC) + timedelta(minutes=delay),
        ))
        session.commit()
    analyzer = Mock(spec=EventAnalyzer)
    analyzer.analyze_pending.return_value = 0
    coordinator = PipelineCoordinator(session_factory, settings, analyzer=analyzer)
    try:
        future = coordinator.submit_analysis_retry()
        if due:
            assert future is not None
            assert future.result(timeout=5) == 0
            analyzer.analyze_pending.assert_called_once()
        else:
            assert future is None
            analyzer.analyze_pending.assert_not_called()
    finally:
        coordinator.shutdown()


def test_busy_retry_worker_is_not_enqueued_twice(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        session.add(Event(
            event_key="busy-analysis", title="Synthetic event", status="error",
            analysis_attempts=1,
            analysis_next_retry_at=datetime.now(UTC) - timedelta(minutes=1),
        ))
        session.commit()
    started, release = ThreadEvent(), ThreadEvent()

    def block(_session: object) -> int:
        started.set()
        assert release.wait(timeout=5)
        return 0

    analyzer = Mock(spec=EventAnalyzer)
    analyzer.analyze_pending.side_effect = block
    coordinator = PipelineCoordinator(session_factory, settings, analyzer=analyzer)
    try:
        future = coordinator.submit_analysis_retry()
        assert future is not None
        assert started.wait(timeout=5)
        with pytest.raises(PipelineBusyError):
            coordinator.submit_analysis_retry()
        release.set()
        assert future.result(timeout=5) == 0
    finally:
        release.set()
        coordinator.shutdown()
