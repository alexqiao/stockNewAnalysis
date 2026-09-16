from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from trade_news_analysis import scheduler
from trade_news_analysis.config import Settings
from trade_news_analysis.services.coordinator import PipelineBusyError


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
