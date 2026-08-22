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

    scheduler.add_job(
        scheduled_pipeline,
        "interval",
        minutes=settings.ingest_interval_minutes,
        id="news-ingestion",
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
    scheduler.start()
    return scheduler
