from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event as Gate
from unittest.mock import Mock, patch

import pytest

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.services.coordinator import PipelineBusyError, PipelineCoordinator


@pytest.mark.parametrize("kind", ["analysis", "evaluation"])
def test_manual_work_is_busy_until_done_and_cannot_queue_duplicates(
    settings: Settings, session_factory: SessionFactory, kind: str,
) -> None:
    started, release = Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)

    def block(*_args: object) -> int:
        started.set()
        assert release.wait(5)
        return 0

    target = "_execute_analysis" if kind == "analysis" else "_execute_evaluation"
    with patch.object(coordinator, target, side_effect=block):
        future = (
            coordinator.submit_analysis(1)
            if kind == "analysis" else coordinator.submit_evaluation()
        )
        try:
            assert started.wait(5)
            assert coordinator.busy is True
            for submit in (
                lambda: coordinator.submit_analysis(1), coordinator.submit_evaluation,
                lambda: coordinator.submit_pipeline("test"), coordinator.submit_research,
                coordinator.submit_analysis_retry,
            ):
                with pytest.raises(PipelineBusyError):
                    submit()
            release.set()
            future.result(5)
            assert coordinator.busy is False
            retry = (
                coordinator.submit_analysis(1)
                if kind == "analysis" else coordinator.submit_evaluation()
            )
            retry.result(5)
        finally:
            release.set()
            coordinator.shutdown()


def test_shutdown_waits_for_worker_without_holding_lock_or_accepting_new_work(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    started, release, shutdown_started = Gate(), Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)

    def block(_event_id: int) -> None:
        started.set()
        assert release.wait(5)
        # Shutdown must not keep this lock while waiting for this worker.
        assert coordinator._lock.acquire(timeout=2)
        coordinator._lock.release()

    original_shutdown = coordinator.x_executor.shutdown

    def shutdown_x(*args: object, **kwargs: object) -> None:
        shutdown_started.set()
        original_shutdown(wait=True, cancel_futures=False)

    with patch.object(coordinator, "_execute_analysis", side_effect=block), patch.object(
        coordinator.x_executor, "shutdown", side_effect=shutdown_x,
    ), ThreadPoolExecutor(max_workers=1) as closer:
        worker = coordinator.submit_analysis(1)
        assert started.wait(5)
        closing = closer.submit(coordinator.shutdown)
        try:
            assert shutdown_started.wait(5)
            assert not closing.done()
            assert not worker.done()
            with pytest.raises(PipelineBusyError):
                coordinator.submit_analysis(2)
            with pytest.raises(PipelineBusyError):
                coordinator.submit_x_ingestion()
        finally:
            release.set()
        closing.result(5)
        assert worker.done()


def test_failed_worker_releases_busy_state_and_can_be_retried(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    coordinator = PipelineCoordinator(session_factory, settings)
    try:
        with patch.object(coordinator, "_execute_evaluation", side_effect=RuntimeError("test")):
            future = coordinator.submit_evaluation()
            with pytest.raises(RuntimeError, match="test"):
                future.result(5)
        assert coordinator.busy is False
        with patch.object(coordinator, "_execute_evaluation", return_value=1):
            assert coordinator.submit_evaluation().result(5) == 1
    finally:
        coordinator.shutdown()


def test_scheduled_evaluation_retries_busy_worker_after_one_minute(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    from trade_news_analysis import scheduler

    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    coordinator = Mock()
    coordinator.submit_evaluation.side_effect = [PipelineBusyError("busy"), None]
    scheduler.start_scheduler(settings, coordinator)
    callback = next(call.args[0] for call in background.add_job.call_args_list
                    if call.kwargs.get("id") == "outcome-evaluation")
    callback()
    assert background.add_job.call_args.args == (callback, "date")
    assert background.add_job.call_args.kwargs["id"] == "outcome-evaluation-retry"
    assert background.add_job.call_args.kwargs["replace_existing"] is True
    job_count = background.add_job.call_count
    callback()
    assert coordinator.submit_evaluation.call_count == 2
    assert background.add_job.call_count == job_count


def test_shutdown_allows_collected_x_posts_to_finish_on_main_worker(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    collected, release, shutdown_started = Gate(), Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)

    def collect(*_args: object, **_kwargs: object) -> None:
        collected.set()
        assert release.wait(5)

    original_shutdown = coordinator.x_executor.shutdown

    def shutdown_x(*_args: object, **_kwargs: object) -> None:
        shutdown_started.set()
        original_shutdown(wait=True, cancel_futures=False)

    with patch.object(coordinator.x_ingestion, "execute", side_effect=collect), patch.object(
        coordinator, "_finish_x_ingestion", return_value={7},
    ), patch.object(coordinator.x_executor, "shutdown", side_effect=shutdown_x), (
        ThreadPoolExecutor(max_workers=1)
    ) as closer:
        worker = coordinator.submit_x_ingestion()
        assert collected.wait(5)
        closing = closer.submit(coordinator.shutdown)
        try:
            assert shutdown_started.wait(5)
            assert not closing.done()
        finally:
            release.set()
        closing.result(5)
        assert worker.result(5) == {7}
        assert coordinator.x_status == "completed"


def test_lifespan_drains_scheduler_then_workers_before_disposing_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from trade_news_analysis import main
    from trade_news_analysis.db import build_engine

    engine = build_engine(settings.database_url)
    order: list[str] = []
    coordinator = Mock(spec=PipelineCoordinator)
    scheduler = Mock()
    original_dispose = engine.dispose

    def shutdown_scheduler(*, wait: bool) -> None:
        assert wait is True
        order.append("scheduler")

    def dispose_engine() -> None:
        order.append("engine")
        original_dispose()

    scheduler.shutdown.side_effect = shutdown_scheduler
    coordinator.shutdown.side_effect = lambda: order.append("workers")
    monkeypatch.setattr(main, "build_engine", lambda _url: engine)
    monkeypatch.setattr(main, "start_scheduler", lambda *_args: scheduler)
    monkeypatch.setattr(engine, "dispose", dispose_engine)
    with TestClient(main.create_app(
        settings.model_copy(update={"scheduler_enabled": True}), coordinator=coordinator,
        fundamental_provider=Mock(),
    )):
        assert order == []
    assert order == ["scheduler", "workers", "engine"]


def test_manual_analysis_never_reactivates_event_without_evidence(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    from trade_news_analysis.models import Event
    from trade_news_analysis.services.analysis import EventAnalyzer

    with session_factory() as session:
        event = Event(
            event_key="reanalyze-excluded", title="Synthetic", status="excluded", error="old",
        )
        session.add(event)
        session.commit()
        event_id = event.id
    completion = Mock(side_effect=AssertionError("excluded event must not call the model"))
    analyzer = EventAnalyzer(settings, completion=completion)
    coordinator = PipelineCoordinator(session_factory, settings, analyzer=analyzer)
    try:
        coordinator.submit_analysis(event_id).result(5)
        completion.assert_not_called()
        with session_factory() as reader:
            persisted = reader.get(Event, event_id)
            assert persisted is not None and persisted.status == "excluded"
            assert not any(impact.is_current for impact in persisted.impacts)
    finally:
        coordinator.shutdown()
