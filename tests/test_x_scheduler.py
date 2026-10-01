from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from trade_news_analysis import scheduler
from trade_news_analysis.config import Settings
from trade_news_analysis.services.coordinator import PipelineBusyError


@pytest.mark.parametrize("enabled", [False, True])
def test_analysis_retry_runs_every_minute_only_when_analysis_is_enabled(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, enabled: bool,
) -> None:
    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    coordinator = Mock()
    coordinator.submit_analysis_retry.side_effect = [PipelineBusyError("busy"), None]
    scheduler.start_scheduler(settings.model_copy(update={"auto_analyze": enabled}), coordinator)
    jobs = [call for call in background.add_job.call_args_list
            if call.kwargs.get("id") == "analysis-retry"]
    assert len(jobs) == int(enabled)
    if enabled:
        assert jobs[0].kwargs["minutes"] == 1
        jobs[0].args[0]()
        jobs[0].args[0]()
        assert coordinator.submit_analysis_retry.call_count == 2


def test_busy_x_schedule_retries_before_next_six_hour_interval(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    coordinator = Mock()
    coordinator.submit_x_ingestion.side_effect = [PipelineBusyError("busy"), None]
    scheduler.start_scheduler(settings.model_copy(update={"x_browser_enabled": True}), coordinator)
    callback = next(call.args[0] for call in background.add_job.call_args_list
                    if call.kwargs.get("id") == "x-post-ingestion")

    before = datetime.now(UTC)
    callback()

    retry = background.add_job.call_args
    assert retry.args == (callback, "date")
    assert retry.kwargs["id"] == "x-post-ingestion-retry"
    assert retry.kwargs["replace_existing"] is True
    assert before + timedelta(seconds=59) < retry.kwargs["run_date"]
    assert retry.kwargs["run_date"] < datetime.now(UTC) + timedelta(seconds=61)
    job_count = background.add_job.call_count
    callback()
    assert coordinator.submit_x_ingestion.call_count == 2
    assert background.add_job.call_count == job_count


def test_disabled_x_schedule_has_no_x_job(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    scheduler.start_scheduler(settings.model_copy(update={"x_browser_enabled": False}), Mock())
    assert not any(call.kwargs.get("id", "").startswith("x-post")
                   for call in background.add_job.call_args_list)


def test_first_x_collection_runs_one_minute_after_startup(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    before = datetime.now(UTC)
    scheduler.start_scheduler(settings.model_copy(update={"x_browser_enabled": True}), Mock())
    job = next(call for call in background.add_job.call_args_list
               if call.kwargs.get("id") == "x-post-ingestion")
    assert job.args[1] == "interval"
    assert job.kwargs["hours"] == settings.x_fetch_interval_hours
    assert before + timedelta(seconds=59) < job.kwargs["next_run_time"]
    assert job.kwargs["next_run_time"] < datetime.now(UTC) + timedelta(seconds=61)


def test_research_starts_soon_after_restart_and_retries_busy_pipeline(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    background = Mock()
    monkeypatch.setattr(scheduler, "BackgroundScheduler", Mock(return_value=background))
    coordinator = Mock()
    coordinator.submit_research.side_effect = [PipelineBusyError("busy"), 1]
    before = datetime.now(UTC)
    scheduler.start_scheduler(settings, coordinator)
    job = next(call for call in background.add_job.call_args_list
               if call.kwargs.get("id") == "research-refresh")
    assert job.kwargs["hours"] == settings.research_refresh_interval_hours
    assert before + timedelta(seconds=59) < job.kwargs["next_run_time"]
    assert job.kwargs["next_run_time"] < datetime.now(UTC) + timedelta(seconds=61)
    callback = job.args[0]
    callback()
    retry = background.add_job.call_args
    assert retry.kwargs["id"] == "research-refresh-retry"
    assert retry.kwargs["run_date"] > before + timedelta(seconds=119)
    callback()
    assert coordinator.submit_research.call_count == 2
