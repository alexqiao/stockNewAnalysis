"""Aggregate forward-validation evidence without overstating small samples."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ArticleSymbol, ImpactAnalysis, Outcome


def build_metrics(
    session: Session,
    symbol: str | None = None,
    horizon: int | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    query = (
        select(Outcome, ImpactAnalysis, ArticleSymbol)
        .join(ImpactAnalysis, Outcome.analysis_id == ImpactAnalysis.id)
        .join(ArticleSymbol, ImpactAnalysis.article_symbol_id == ArticleSymbol.id)
    )
    if symbol:
        query = query.where(ArticleSymbol.symbol == symbol.upper())
    if horizon:
        query = query.where(Outcome.horizon == horizon)
    if since:
        query = query.where(Outcome.created_at >= since)
    rows = session.execute(query).all()
    n = len(rows)
    accuracy = sum(int(outcome.correct) for outcome, _, _ in rows) / n if n else None
    average_excess = sum(outcome.excess_return_pct for outcome, _, _ in rows) / n if n else None
    buckets: dict[str, list[bool]] = {"low": [], "medium": [], "high": []}
    for outcome, analysis, _ in rows:
        confidence = float(analysis.impacts[str(outcome.horizon)]["confidence"])
        label = "low" if confidence < 0.4 else "medium" if confidence < 0.7 else "high"
        buckets[label].append(outcome.correct)
    return {
        "sample_size": n,
        "evidence": "sufficient" if n >= 30 else "insufficient",
        "accuracy": accuracy,
        "average_excess_return_pct": average_excess,
        "confidence_buckets": {
            label: {
                "sample_size": len(values),
                "accuracy": sum(values) / len(values) if values else None,
            }
            for label, values in buckets.items()
        },
    }
