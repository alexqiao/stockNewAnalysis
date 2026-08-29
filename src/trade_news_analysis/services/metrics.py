"""Top-K and market-aware forward-validation metrics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import sqrt
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Security, SecuritySignalSnapshot, SignalOutcome
from .normalization import ensure_aware

MetricRow = tuple[SignalOutcome, SecuritySignalSnapshot, Security]


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left_mean = sum(left for left, _ in pairs) / len(pairs)
    right_mean = sum(right for _, right in pairs) / len(pairs)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_scale = sqrt(sum((left - left_mean) ** 2 for left, _ in pairs))
    right_scale = sqrt(sum((right - right_mean) ** 2 for _, right in pairs))
    return numerator / (left_scale * right_scale) if left_scale and right_scale else None


def _average_ranks(values: list[float]) -> list[float]:
    """Return one-based average ranks, including deterministic tie handling."""
    result = [0.0] * len(values)
    ordered = sorted(range(len(values)), key=lambda index: (values[index], index))
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[start]]:
            end += 1
        average_rank = (start + 1 + end) / 2
        for position in range(start, end):
            result[ordered[position]] = average_rank
        start = end
    return result


def _rank_ic_summary(rows: list[MetricRow]) -> dict[str, Any]:
    """Summarize Qlib-style cross-sectional Spearman Rank IC without annualization."""
    cross_sections: dict[tuple[str, int, datetime], list[MetricRow]] = defaultdict(list)
    for row in rows:
        outcome, snapshot, security = row
        cross_sections[(security.market, snapshot.horizon, ensure_aware(snapshot.as_of))].append(
            (outcome, snapshot, security)
        )

    period_values: list[float] = []
    for period_rows in cross_sections.values():
        if len(period_rows) < 3:
            continue
        scores = [float(snapshot.score) for _, snapshot, _ in period_rows]
        returns = [float(outcome.excess_return_pct) for outcome, _, _ in period_rows]
        correlation = _correlation(
            list(zip(_average_ranks(scores), _average_ranks(returns), strict=True))
        )
        if correlation is not None:
            period_values.append(correlation)

    periods = len(period_values)
    mean = sum(period_values) / periods if periods else None
    standard_deviation = (
        sqrt(sum((value - mean) ** 2 for value in period_values) / (periods - 1))
        if periods >= 2 and mean is not None
        else None
    )
    return {
        "rank_ic_mean": mean,
        "rank_ic_std": standard_deviation,
        "rank_icir": (
            mean / standard_deviation
            if mean is not None
            and standard_deviation is not None
            and standard_deviation != 0.0
            else None
        ),
        "rank_ic_positive_rate": (
            sum(value > 0 for value in period_values) / periods if periods else None
        ),
        "rank_ic_periods": periods,
    }


def _summarize(
    rows: list[MetricRow],
    rank_ic_rows: list[MetricRow] | None = None,
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
        **_rank_ic_summary(rank_ic_rows if rank_ic_rows is not None else rows),
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
    all_rows = cast(
        list[MetricRow],
        list(session.execute(query).tuples().all()),
    )
    rows = [
        row
        for row in all_rows
        if row[1].rank is None or row[1].rank <= top_k
    ]
    markets = {
        name: _summarize(
            [row for row in rows if row[2].market == name],
            [row for row in all_rows if row[2].market == name],
        )
        for name in ("A", "HK", "US")
    }
    return {
        **_summarize(rows, all_rows),
        "top_k": top_k,
        "evidence": "sufficient" if len(rows) >= 30 else "insufficient",
        "by_market": markets,
    }
