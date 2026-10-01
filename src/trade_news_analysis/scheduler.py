"""In-process scheduler for the local single-worker deployment."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .services.coordinator import PipelineBusyError, PipelineCoordinator

logger = logging.getLogger(__name__)


def start_scheduler(settings: Settings, coordinator: PipelineCoordinator) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.app_timezone)

    def scheduled_pipeline() -> None:
        try:
            coordinator.submit_pipeline("schedule")
        except PipelineBusyError:
            logger.info("Skipping scheduled ingestion because the previous run is active")

    def scheduled_evaluation() -> None:
        try:
            coordinator.submit_evaluation()
        except PipelineBusyError:
            scheduler.add_job(
                scheduled_evaluation, "date",
                run_date=datetime.now(UTC) + timedelta(minutes=1),
                id="outcome-evaluation-retry", replace_existing=True,
            )

    def scheduled_analysis_retry() -> None:
        try:
            coordinator.submit_analysis_retry()
        except PipelineBusyError:
            pass  # Due work is retried on the next minute tick.

    def scheduled_telegram_digest() -> None:
        coordinator.submit_telegram_digest()

    def scheduled_telegram_commands() -> None:
        coordinator.poll_telegram_commands()

    def scheduled_x_ingestion() -> None:
        try:
            coordinator.submit_x_ingestion()
        except PipelineBusyError:
            logger.info("X ingestion is active; retrying scheduled X ingestion in one minute")
            scheduler.add_job(
                scheduled_x_ingestion,
                "date",
                run_date=datetime.now(UTC) + timedelta(minutes=1),
                id="x-post-ingestion-retry",
                replace_existing=True,
            )

    def scheduled_research() -> None:
        try:
            coordinator.submit_research()
        except PipelineBusyError:
            scheduler.add_job(
                scheduled_research, "date",
                run_date=datetime.now(UTC) + timedelta(minutes=2),
                id="research-refresh-retry", replace_existing=True,
            )

    def scheduled_daily_bars() -> None:
        try:
            coordinator.submit_daily_bars()
        except PipelineBusyError:
            logger.info("Skipping daily bars check because the application is shutting down")

    scheduler.add_job(
        scheduled_pipeline,
        "interval",
        minutes=settings.ingest_interval_minutes,
        id="news-ingestion",
        max_instances=1,
        coalesce=True,
    )
    if settings.auto_analyze:
        scheduler.add_job(
            scheduled_analysis_retry, "interval", minutes=1,
            id="analysis-retry", max_instances=1, coalesce=True,
        )
    if settings.research_refresh_enabled:
        scheduler.add_job(
            scheduled_research, "interval", hours=settings.research_refresh_interval_hours,
            id="research-refresh", max_instances=1, coalesce=True,
            next_run_time=datetime.now(UTC) + timedelta(minutes=1),
        )
    if settings.daily_bars_enabled:
        scheduler.add_job(
            scheduled_daily_bars, "interval", minutes=15,
            id="daily-bars-refresh", max_instances=1, coalesce=True,
            next_run_time=datetime.now(UTC) + timedelta(minutes=1),
        )
    if settings.x_browser_enabled:
        scheduler.add_job(
            scheduled_x_ingestion,
            "interval",
            hours=settings.x_fetch_interval_hours,
            id="x-post-ingestion",
            next_run_time=datetime.now(UTC) + timedelta(minutes=1),
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        scheduled_evaluation,
        "cron",
        day_of_week="mon-fri",
        hour=18,
        minute=15,
        id="outcome-evaluation",
        max_instances=1,
        coalesce=True,
    )
    if settings.telegram_enabled:
        scheduler.add_job(
            scheduled_telegram_digest,
            "cron",
            day_of_week="mon-fri",
            hour=settings.telegram_digest_hour,
            minute=settings.telegram_digest_minute,
            timezone=settings.telegram_digest_timezone,
            id="telegram-opportunity-digest",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            scheduled_telegram_commands,
            "interval",
            seconds=settings.telegram_command_poll_seconds,
            id="telegram-command-polling",
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    return scheduler
