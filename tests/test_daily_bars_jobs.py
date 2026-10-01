from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event as Gate
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import select

from trade_news_analysis import scheduler
from trade_news_analysis.config import Settings
from trade_news_analysis.daily_bar_models import DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import IngestionRun, Security, Watchlist
from trade_news_analysis.schemas import RunResponse
from trade_news_analysis.services.coordinator import PipelineBusyError, PipelineCoordinator


def test_daily_bars_merge_running_default_explicit_and_duplicate_requests(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    started, release = Gate(), Gate()
    calls: list[tuple[list[int] | None, bool]] = []
    coordinator = PipelineCoordinator(session_factory, settings)

    def refresh(
        _factory: SessionFactory, ids: list[int] | None, *, force: bool,
    ) -> dict[str, object]:
        calls.append((ids, force))
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return {"updated": 1, "skipped": 0, "failed": 0, "errors": []}

    with patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        side_effect=refresh,
    ), patch(
        "trade_news_analysis.services.market_research.MarketResearchService.refresh_cached",
        return_value={"updated_security_ids": [], "skipped": 0},
    ) as cached_refresh:
        try:
            run_id = coordinator.submit_daily_bars()
            assert started.wait(5)
            assert coordinator.submit_daily_bars() == run_id
            assert coordinator.submit_daily_bars([7, 8, 7]) == run_id
            assert coordinator.submit_daily_bars([8]) == run_id
            assert coordinator.submit_daily_bars([9], force=True) == run_id
            future = coordinator._daily_bars_future
            assert future is not None
            release.set()
            assert future.result(5)["updated"] == 3
            assert coordinator._cached_research_future is not None
            coordinator._cached_research_future.result(5)
            assert calls == [(None, False), ([7, 8], False), ([9], True)]
            with session_factory() as session:
                expected_ids = {7, 8, 9} | set(session.scalars(
                    select(Watchlist.security_id).where(Watchlist.active.is_(True)),
                ))
                cached_refresh.assert_called_once_with(session_factory, sorted(expected_ids))
                runs = list(session.scalars(select(IngestionRun).where(
                    IngestionRun.trigger == "daily_bars",
                )))
                assert len(runs) == 1
                assert runs[0].status == "completed"
                assert runs[0].articles_seen == runs[0].articles_new == 0
        finally:
            release.set()
            coordinator.shutdown()


def test_daily_bars_force_upgrade_is_not_lost_while_same_stock_is_running(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    started, release = Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)
    calls: list[bool] = []

    def refresh(
        _factory: SessionFactory, ids: list[int] | None, *, force: bool,
    ) -> dict[str, object]:
        assert ids == [7]
        calls.append(force)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
        return {"updated": 1, "failed": 0, "errors": []}

    with patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        side_effect=refresh,
    ):
        try:
            run_id = coordinator.submit_daily_bars([7])
            assert started.wait(5)
            assert coordinator.submit_daily_bars([7]) == run_id
            assert coordinator.submit_daily_bars([7], force=True) == run_id
            assert coordinator.submit_daily_bars([7], force=True) == run_id
            future = coordinator._daily_bars_future
            assert future is not None
            release.set()
            future.result(5)
            assert calls == [False, True]
        finally:
            release.set()
            coordinator.shutdown()


def test_daily_bars_finish_while_news_worker_is_busy_and_auto_updates_are_disabled(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    started, release = Gate(), Gate()
    coordinator = PipelineCoordinator(
        session_factory, settings.model_copy(update={"daily_bars_enabled": False}),
    )

    def analysis(_event_id: int) -> None:
        started.set()
        assert release.wait(5)

    with patch.object(coordinator, "_execute_analysis", side_effect=analysis), patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        return_value={"updated": 1, "failed": 0, "errors": []},
    ) as refresh:
        try:
            analysis_future = coordinator.submit_analysis(1)
            assert started.wait(5)
            run_id = coordinator.submit_daily_bars([7])
            future = coordinator._daily_bars_future
            assert future is not None
            future.result(5)
            refresh.assert_called_once_with(session_factory, [7], force=False)
            assert not analysis_future.done()
            assert coordinator.busy is True
            with session_factory() as session:
                run = session.get(IngestionRun, run_id)
                assert run is not None and run.status == "completed"
            release.set()
            analysis_future.result(5)
            assert coordinator._cached_research_future is not None
            coordinator._cached_research_future.result(5)
            assert coordinator.busy is False
        finally:
            release.set()
            coordinator.shutdown()


def test_running_daily_bars_do_not_make_news_worker_busy(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    started, release = Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)

    def refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
        started.set()
        assert release.wait(5)
        return {"updated": 1, "failed": 0, "errors": []}

    with patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        side_effect=refresh,
    ), patch.object(coordinator, "_execute_analysis") as analyze:
        try:
            coordinator.submit_daily_bars([7])
            assert started.wait(5)
            assert coordinator.busy is False
            daily_future = coordinator._daily_bars_future
            assert daily_future is not None and not daily_future.done()
            coordinator.submit_analysis(1).result(5)
            analyze.assert_called_once_with(1)
            assert not daily_future.done()
            assert coordinator.busy is False
            release.set()
            daily_future.result(5)
        finally:
            release.set()
            coordinator.shutdown()


@pytest.mark.parametrize("unexpected", [False, True])
def test_daily_bars_failed_results_and_exceptions_are_visible_and_retryable(
    settings: Settings, session_factory: SessionFactory, unexpected: bool,
) -> None:
    coordinator = PipelineCoordinator(session_factory, settings)
    with patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        side_effect=RuntimeError("private provider detail") if unexpected else None,
        return_value={"updated": 1, "skipped": 0, "failed": 1, "errors": ["来源暂不可用"]},
    ) as refresh:
        try:
            run_id = coordinator.submit_daily_bars([7, 8])
            future = coordinator._daily_bars_future
            assert future is not None
            future.result(5)
            with session_factory() as session:
                run = session.get(IngestionRun, run_id)
                assert run is not None and run.status == ("failed" if unexpected else "partial")
                assert run.completed_at is not None and run.errors
                assert "private provider detail" not in str(run.errors)
                assert run.articles_seen == run.articles_new == run.analyses_created == 0
            refresh.side_effect = None
            refresh.return_value = {"updated": 1, "failed": 0, "errors": []}
            retry_id = coordinator.submit_daily_bars([7])
            assert retry_id != run_id
            future = coordinator._daily_bars_future
            assert future is not None
            future.result(5)
            assert coordinator._cached_research_future is not None
            coordinator._cached_research_future.result(5)
            assert coordinator.busy is False
        finally:
            coordinator.shutdown()


@pytest.mark.parametrize("release_first", ["news", "daily"])
def test_shutdown_waits_for_both_daily_and_news_workers_and_rejects_submissions(
    settings: Settings, session_factory: SessionFactory, release_first: str,
) -> None:
    news_started, daily_started, closing_started = Gate(), Gate(), Gate()
    release_news, release_daily = Gate(), Gate()
    coordinator = PipelineCoordinator(session_factory, settings)

    def refresh(*_args: object, **_kwargs: object) -> dict[str, object]:
        daily_started.set()
        assert release_daily.wait(5)
        assert coordinator._lock.acquire(timeout=2)
        coordinator._lock.release()
        return {"updated": 1, "failed": 0, "errors": []}

    def analysis(_event_id: int) -> None:
        news_started.set()
        assert release_news.wait(5)
        assert coordinator._lock.acquire(timeout=2)
        coordinator._lock.release()

    original = coordinator.x_executor.shutdown

    def shutdown_x(*_args: object, **_kwargs: object) -> None:
        closing_started.set()
        original(wait=True, cancel_futures=False)

    with patch(
        "trade_news_analysis.services.daily_bars.DailyBarService.refresh_isolated",
        side_effect=refresh,
    ), patch.object(coordinator, "_execute_analysis", side_effect=analysis), patch.object(
        coordinator.x_executor, "shutdown", side_effect=shutdown_x,
    ), (
        ThreadPoolExecutor(max_workers=1)
    ) as closer:
        news_future = coordinator.submit_analysis(1)
        assert news_started.wait(5)
        coordinator.submit_daily_bars([7])
        assert daily_started.wait(5)
        daily_future = coordinator._daily_bars_future
        assert daily_future is not None
        closing = closer.submit(coordinator.shutdown)
        try:
            assert closing_started.wait(5)
            assert not closing.done()
            with pytest.raises(PipelineBusyError, match="关闭"):
                coordinator.submit_daily_bars([8])
            with pytest.raises(PipelineBusyError):
                coordinator.submit_analysis(2)
            if release_first == "news":
                release_news.set()
                news_future.result(5)
                assert not daily_future.done()
            else:
                release_daily.set()
                daily_future.result(5)
                assert not news_future.done()
            assert not closing.done()
        finally:
            release_news.set()
            release_daily.set()
        closing.result(5)
        assert daily_future.done() and news_future.done()


def test_real_daily_bar_failure_is_compatible_with_run_status_response(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        assert security_id is not None
    coordinator = PipelineCoordinator(session_factory, settings)
    with patch(
        "trade_news_analysis.services.providers.YahooMarketDataProvider.history_range",
        side_effect=RuntimeError("private provider detail"),
    ) as fetch:
        try:
            run_id = coordinator.submit_daily_bars([security_id], force=True)
            future = coordinator._daily_bars_future
            assert future is not None
            result = future.result(5)
            assert result["failed"] == 1
            fetch.assert_called_once()
            with session_factory() as session:
                run = session.get(IngestionRun, run_id)
                assert run is not None
                payload = RunResponse.model_validate(run).model_dump(mode="json")
                assert payload["status"] == "failed"
                assert payload["errors"] and all(
                    isinstance(error, str) for error in payload["errors"]
                )
                assert "private provider detail" not in str(payload["errors"])
                assert run.completed_at is not None
        finally:
            coordinator.shutdown()


@pytest.mark.parametrize("enabled", [False, True])
def test_daily_bars_schedule_checks_every_fifteen_minutes_and_after_startup(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, enabled: bool,
) -> None:
    background, coordinator = Mock(), Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    before = datetime.now(UTC)
    scheduler.start_scheduler(
        settings.model_copy(update={"daily_bars_enabled": enabled}), coordinator,
    )
    jobs = [call for call in background.add_job.call_args_list
            if call.kwargs.get("id") == "daily-bars-refresh"]
    assert len(jobs) == int(enabled)
    if enabled:
        job = jobs[0]
        assert job.args[1] == "interval"
        assert job.kwargs["minutes"] == 15
        assert job.kwargs["max_instances"] == 1 and job.kwargs["coalesce"] is True
        assert before + timedelta(seconds=59) < job.kwargs["next_run_time"]
        assert job.kwargs["next_run_time"] < datetime.now(UTC) + timedelta(seconds=61)
        coordinator.submit_daily_bars.side_effect = [1, PipelineBusyError("关闭")]
        job.args[0]()
        job.args[0]()
        assert coordinator.submit_daily_bars.call_count == 2


def test_daily_bars_recovery_preserves_good_data_and_clears_retry_delay(
    settings: Settings, session_factory: SessionFactory,
) -> None:
    stamp = datetime(2026, 9, 18, 20, tzinfo=UTC)
    with session_factory() as session:
        securities = list(session.scalars(select(Security).order_by(Security.id).limit(2)))
        for index, security in enumerate(securities):
            session.add(DailyBarSyncState(
                security_id=security.id, source="yfinance", currency=security.currency,
                timezone=security.timezone, status="running" if index == 0 else "success",
                last_success_at=stamp, coverage_start=stamp.date(), coverage_end=stamp.date(),
                next_retry_at=stamp + timedelta(days=1), failure_count=2,
            ))
        run = IngestionRun(trigger="daily_bars", status="running")
        session.add(run)
        session.commit()
        run_id, first_id, second_id = run.id, securities[0].id, securities[1].id
    coordinator = PipelineCoordinator(session_factory, settings)
    try:
        coordinator.recover_interrupted()
        with session_factory() as session:
            recovered = session.get(DailyBarSyncState, first_id)
            assert recovered is not None and recovered.status == "failed"
            assert recovered.next_retry_at is None
            assert recovered.error_kind == "interrupted" and recovered.error
            assert recovered.failure_count == 2
            assert recovered.last_success_at is not None
            assert recovered.coverage_end == stamp.date()
            untouched = session.get(DailyBarSyncState, second_id)
            assert untouched is not None and untouched.status == "success"
            assert untouched.next_retry_at is not None
            recovered_run = session.get(IngestionRun, run_id)
            assert recovered_run is not None and recovered_run.status == "failed"
    finally:
        coordinator.shutdown()
