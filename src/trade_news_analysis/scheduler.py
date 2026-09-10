"""In-process scheduler for the local single-worker deployment."""

from __future__ import annotations

import logging

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
        coordinator.submit_evaluation()

    def scheduled_telegram_digest() -> None:
        coordinator.submit_telegram_digest()

    def scheduled_telegram_commands() -> None:
        coordinator.poll_telegram_commands()

    def scheduled_x_ingestion() -> None:
        try:
            coordinator.submit_x_ingestion()
        except PipelineBusyError:
            logger.info("Skipping scheduled X ingestion because the pipeline is active")

    scheduler.add_job(
        scheduled_pipeline,
        "interval",
        minutes=settings.ingest_interval_minutes,
        id="news-ingestion",
        max_instances=1,
        coalesce=True,
    )
    if settings.x_browser_enabled:
        scheduler.add_job(
            scheduled_x_ingestion,
            "interval",
            hours=settings.x_fetch_interval_hours,
            id="x-post-ingestion",
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
