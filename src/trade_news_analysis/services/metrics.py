"""Top-K and market-aware forward-validation metrics."""

from __future__ import annotations

from datetime import datetime
from math import sqrt
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Security, SecuritySignalSnapshot, SignalOutcome


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left_mean = sum(left for left, _ in pairs) / len(pairs)
    right_mean = sum(right for _, right in pairs) / len(pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_scale = sqrt(sum((left - left_mean) ** 2 for left, _ in pairs))
    right_scale = sqrt(sum((right - right_mean) ** 2 for _, right in pairs))
    return numerator / (left_scale * right_scale) if left_scale and right_scale else None


def _summarize(
    rows: list[tuple[SignalOutcome, SecuritySignalSnapshot, Security]],
) -> dict[str, Any]:
    n = len(rows)
    limit_rows = [
        outcome.limit_up_hit
        for outcome, _, security in rows
        if security.market == "A" and outcome.limit_up_hit is not None
    ]
    rank_pairs = [
        (float(snapshot.rank), outcome.excess_return_pct)
        for outcome, snapshot, _ in rows
        if snapshot.rank is not None
    ]
    correlation = _correlation(rank_pairs)
    return {
        "sample_size": n,
        "hit_rate": sum(int(outcome.correct) for outcome, _, _ in rows) / n if n else None,
        "average_excess_return_pct": (
            sum(outcome.excess_return_pct for outcome, _, _ in rows) / n if n else None
        ),
        "rank_correlation": -correlation if correlation is not None else None,
        "limit_up_hit_rate": (
            sum(int(value) for value in limit_rows) / len(limit_rows) if limit_rows else None
        ),
    }


def build_metrics(
    session: Session,
    security_id: int | None = None,
    market: str | None = None,
    horizon: int | None = None,
    since: datetime | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    query = (
        select(SignalOutcome, SecuritySignalSnapshot, Security)
        .join(SecuritySignalSnapshot, SignalOutcome.snapshot_id == SecuritySignalSnapshot.id)
        .join(Security, SecuritySignalSnapshot.security_id == Security.id)
    )
    if security_id:
        query = query.where(Security.id == security_id)
    if market:
        query = query.where(Security.market == market)
    if horizon:
        query = query.where(SecuritySignalSnapshot.horizon == horizon)
    if since:
        query = query.where(SecuritySignalSnapshot.as_of >= since)
    query = query.where(
        (SecuritySignalSnapshot.rank.is_(None)) | (SecuritySignalSnapshot.rank <= top_k)
    )
    rows = cast(
        list[tuple[SignalOutcome, SecuritySignalSnapshot, Security]],
        list(session.execute(query).tuples().all()),
    )
    markets = {
        name: _summarize([row for row in rows if row[2].market == name])
        for name in ("A", "HK", "US")
    }
    return {
        **_summarize(rows),
        "top_k": top_k,
        "evidence": "sufficient" if len(rows) >= 30 else "insufficient",
        "by_market": markets,
    }
