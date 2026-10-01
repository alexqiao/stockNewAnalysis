"""Separate historical coverage from source availability at the current time."""

from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import Settings
from ..models import SourceHealth
from .normalization import ensure_aware


def describe_source(
    source: SourceHealth, settings: Settings, now: datetime | None = None,
) -> dict[str, Any]:
    current = ensure_aware(now or datetime.now(UTC))
    interval = timedelta(minutes=settings.ingest_interval_minutes)
    if source.capability == "research":
        interval = timedelta(hours=settings.research_refresh_interval_hours)
    elif source.capability in {"social", "x_posts"}:
        interval = timedelta(hours=settings.x_fetch_interval_hours)
    last_success = ensure_aware(source.last_success_at) if source.last_success_at else None
    last_attempt = ensure_aware(source.last_attempt_at) if source.last_attempt_at else None
    if last_success is None:
        state = "never_succeeded"
    elif source.last_error and source.consecutive_failures and (
        last_attempt is None or last_attempt >= last_success
    ):
        state = "failing"
    elif current - last_success > interval * 2 or last_success > current + timedelta(minutes=5):
        state = "stale"
    else:
        state = "fresh"
    retry_at = None
    if (source.consecutive_failures or 0) >= 3 and last_attempt:
        from .ingestion import SOURCE_FAILURE_BACKOFF

        retry_at = max(current, last_attempt + SOURCE_FAILURE_BACKOFF)
    return {
        "source": source.source, "capability": source.capability,
        "markets": source.markets, "coverage": source.coverage,
        "last_attempt_at": source.last_attempt_at, "last_success_at": source.last_success_at,
        "last_error": source.last_error, "consecutive_failures": source.consecutive_failures,
        "items_last_run": source.items_last_run, "availability": state,
        "fresh_until": last_success + interval * 2 if last_success else None,
        "next_retry_at": retry_at,
    }
