"""Read only the forward window, extending solely for confirmed suspensions."""

from __future__ import annotations

from datetime import date

import pandas as pd

from ..models import Security
from .history_repository import HistoryRepository
from .market_research import number

SUSPENSION_BLOCK_SESSIONS = 20
MAX_SUSPENSION_EXTRA_SESSIONS = 100


def read_validation_history(
    repository: HistoryRepository, security: Security, closed: pd.DataFrame, horizon: int,
) -> tuple[pd.DataFrame, date, str | None]:
    """Bound work even for old snapshots; missing data never proves a suspension."""
    limit = min(len(closed), horizon + MAX_SUSPENSION_EXTRA_SESSIONS)
    count = min(horizon, len(closed))
    start = closed.index[0].date()
    while True:
        end = closed.index[count - 1].date()
        frame = repository.history(security, start, end)
        indexed = frame.copy()
        indexed.index = pd.to_datetime(indexed.index)
        if indexed.index.tz is not None:
            indexed.index = indexed.index.tz_convert(security.timezone).tz_localize(None)
        indexed.index = indexed.index.normalize()
        if indexed.index.has_duplicates:
            return frame, end, None
        tradable = 0
        suspended = False
        for stamp in closed.index[:count]:
            if stamp not in indexed.index:
                return frame, end, None
            volume = number(indexed.loc[stamp, "Volume"]) if "Volume" in indexed else 1.0
            if volume is None or volume < 0:
                return frame, end, None
            if volume == 0:
                suspended = True
                continue
            tradable += 1
            if tradable == horizon:
                return frame, stamp.date(), None
        if not suspended or count >= limit:
            status = (
                "suspension_limit" if count == horizon + MAX_SUSPENSION_EXTRA_SESSIONS else None
            )
            return frame, end, status
        count = min(count + SUSPENSION_BLOCK_SESSIONS, limit)
