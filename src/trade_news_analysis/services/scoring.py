"""Deterministic opportunity scoring and event-to-security signal aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from math import pow

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import EventSecurityImpact, Security, SecuritySignalSnapshot

HORIZONS = (1, 5, 20)
SCORE_WEIGHTS = {
    "demand_certainty": 20.0,
    "transmission_clarity": 20.0,
    "business_purity": 15.0,
    "scale_elasticity": 15.0,
    "market_neglect": 10.0,
    "novelty_unpriced": 10.0,
    "evidence_quality": 5.0,
    "verification_speed": 5.0,
}
DIRECTION_SIGN = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}


def calculate_opportunity_score(values: Mapping[str, float], risk_penalty: float) -> float:
    """Convert 0-5 model dimensions to a transparent 0-100 score."""
    gross = sum(
        max(0.0, min(5.0, float(values.get(name, 0)))) / 5.0 * weight
        for name, weight in SCORE_WEIGHTS.items()
    )
    return round(max(0.0, min(100.0, gross - max(0.0, min(20.0, risk_penalty)))), 2)


def evidence_quality(source_count: int) -> float:
    """Independent sources improve evidence quality without duplicating an event."""
    return min(5.0, 2.0 + max(0, source_count - 1) * 1.5)


def trading_sessions_since(occurred_at: datetime | None, as_of: datetime) -> int:
    """Count weekday sessions without assuming a single market timezone."""
    if occurred_at is None:
        return 0
    start = occurred_at.astimezone(UTC).date()
    end = as_of.astimezone(UTC).date()
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            days += 1
    return days


def freshness_decay(age_sessions: int, horizon: int) -> float:
    return pow(0.5, max(0, age_sessions) / horizon)


def aggregate_security(
    security: Security, horizon: int, as_of: datetime
) -> SecuritySignalSnapshot | None:
    impacts = [
        item
        for item in security.impacts
        if item.status == "complete" and item.is_current and str(horizon) in item.impacts
    ]
    if not impacts:
        return None
    contributions: list[dict[str, float | int | str]] = []
    signed_total = 0.0
    absolute_total = 0.0
    normalizer = 0.0
    for item in impacts:
        horizon_impact = item.impacts[str(horizon)]
        confidence = float(horizon_impact["confidence"])
        direction = str(horizon_impact["direction"])
        decay = freshness_decay(
            trading_sessions_since(item.event.occurred_at or item.created_at, as_of), horizon
        )
        weight = confidence * decay
        contribution = DIRECTION_SIGN.get(direction, 0.0) * item.opportunity_score * weight
        signed_total += contribution
        absolute_total += abs(contribution)
        normalizer += weight
        contributions.append(
            {
                "event_id": item.event_id,
                "direction": direction,
                "opportunity_score": item.opportunity_score,
                "confidence": confidence,
                "decay": round(decay, 4),
                "contribution": round(contribution, 4),
            }
        )
    score = signed_total / normalizer if normalizer else 0.0
    conflict = 1.0 - abs(signed_total) / absolute_total if absolute_total else 0.0
    direction = "bullish" if score > 5 else "bearish" if score < -5 else "neutral"
    mean_confidence = normalizer / len(impacts)
    return SecuritySignalSnapshot(
        security_id=security.id,
        as_of=as_of,
        horizon=horizon,
        score=round(score, 2),
        direction=direction,
        confidence=round(max(0.0, min(1.0, mean_confidence * (1 - conflict))), 4),
        conflict=round(max(0.0, min(1.0, conflict)), 4),
        evidence_event_ids=sorted({item.event_id for item in impacts}),
        components={"events": contributions},
    )


def rebuild_signal_snapshots(
    session: Session, as_of: datetime | None = None
) -> list[SecuritySignalSnapshot]:
    timestamp = as_of or datetime.now(UTC)
    securities = session.scalars(
        select(Security)
        .where(Security.active.is_(True))
        .options(selectinload(Security.impacts).selectinload(EventSecurityImpact.event))
    ).all()
    created: list[SecuritySignalSnapshot] = []
    for security in securities:
        for horizon in HORIZONS:
            snapshot = aggregate_security(security, horizon, timestamp)
            if snapshot:
                session.add(snapshot)
                created.append(snapshot)
    session.flush()
    for horizon in HORIZONS:
        ranked = sorted(
            (item for item in created if item.horizon == horizon and item.score > 0),
            key=lambda item: item.score,
            reverse=True,
        )
        for rank, item in enumerate(ranked, 1):
            item.rank = rank
    session.commit()
    return created
