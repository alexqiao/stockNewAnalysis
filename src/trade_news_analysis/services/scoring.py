"""Deterministic opportunity scoring and event-to-security signal aggregation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from math import pow
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import Event, EventSecurityImpact, Security, SecuritySignalSnapshot
from .evidence import is_narrative_only

HORIZONS = (1, 5, 20)
SIGNAL_TTL_MULTIPLIER = 3
MIN_SIGNAL_CONFIDENCE = 0.15
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
    if source_count <= 0:
        return 0.0
    return min(5.0, 2.0 + max(0, source_count - 1) * 1.5)


def trading_sessions_since(occurred_at: datetime | None, as_of: datetime) -> int:
    """Count weekday sessions without assuming a single market timezone."""
    if occurred_at is None:
        return 0
    start = _utc(occurred_at).date()
    end = _utc(as_of).date()
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def event_is_available(event: Event, as_of: datetime) -> bool:
    """Reject withdrawn and future-dated evidence without rewriting source records."""
    return (
        event.status != "excluded"
        and not is_narrative_only(event)
        and (
            event.occurred_at is None
            or _utc(event.occurred_at) <= _utc(as_of) + timedelta(minutes=5)
        )
    )


def freshness_decay(age_sessions: int, horizon: int) -> float:
    return pow(0.5, max(0, age_sessions) / horizon)


def _base_contribution(component: Mapping[str, Any]) -> float:
    stored = component.get("base_contribution")
    if stored is not None:
        return float(stored)
    sign = DIRECTION_SIGN.get(str(component.get("direction")), 0.0)
    return (
        sign
        * float(component.get("opportunity_score") or 0.0)
        * float(component.get("confidence") or 0.0)
    )


def summarize_components(
    components: list[dict[str, Any]],
) -> tuple[float, float, float, float, str]:
    """Return research score, decision score, confidence, conflict, and direction."""
    directional = [
        item
        for item in components
        if item.get("direction") in {"bullish", "bearish"} and not item.get("expired")
    ]
    if not directional:
        return 0.0, 0.0, 0.0, 0.0, "neutral"

    base_weight = sum(float(item["confidence"]) for item in directional)
    if base_weight <= 0:
        return 0.0, 0.0, 0.0, 0.0, "neutral"
    base_signed = sum(_base_contribution(item) for item in directional)
    decayed_signed = sum(float(item["contribution"]) for item in directional)
    decayed_absolute = sum(abs(float(item["contribution"])) for item in directional)
    conflict = (
        1.0 - abs(decayed_signed) / decayed_absolute if decayed_absolute else 0.0
    )
    conflict = max(0.0, min(1.0, conflict))
    research_score = base_signed / base_weight
    decision_score = decayed_signed / len(directional) * (1 - conflict)
    confidence = (
        sum(float(item["confidence"]) * float(item["decay"]) for item in directional)
        / len(directional)
        * (1 - conflict)
    )
    confidence = max(0.0, min(1.0, confidence))
    if confidence < MIN_SIGNAL_CONFIDENCE or abs(decision_score) <= 5:
        direction = "neutral"
        decision_score = 0.0
    else:
        direction = "bullish" if decision_score > 0 else "bearish"
    return research_score, decision_score, confidence, conflict, direction


def aggregate_security(
    security: Security, horizon: int, as_of: datetime
) -> SecuritySignalSnapshot | None:
    impacts = [
        item
        for item in security.impacts
        if item.status == "complete" and item.is_current and str(horizon) in item.impacts
        and event_is_available(item.event, as_of)
    ]
    if not impacts:
        return None
    contributions: list[dict[str, Any]] = []
    for item in impacts:
        horizon_impact = item.impacts[str(horizon)]
        confidence = float(horizon_impact["confidence"])
        direction = str(horizon_impact["direction"])
        age_sessions = trading_sessions_since(
            item.event.occurred_at or item.created_at, as_of
        )
        expired = age_sessions > horizon * SIGNAL_TTL_MULTIPLIER
        decay = 0.0 if expired else freshness_decay(age_sessions, horizon)
        sign = DIRECTION_SIGN.get(direction, 0.0)
        base_contribution = sign * item.opportunity_score * confidence
        contribution = base_contribution * decay
        contributions.append(
            {
                "event_id": item.event_id,
                "direction": direction,
                "opportunity_score": item.opportunity_score,
                "confidence": confidence,
                "age_sessions": age_sessions,
                "decay": round(decay, 4),
                "expired": expired,
                "base_contribution": round(base_contribution, 4),
                "contribution": round(contribution, 4),
            }
        )
    research_score, score, confidence, conflict, direction = summarize_components(
        contributions
    )
    active_event_ids = sorted(
        {
            int(item["event_id"])
            for item in contributions
            if not item["expired"] and item["direction"] in {"bullish", "bearish"}
        }
    )
    return SecuritySignalSnapshot(
        security_id=security.id,
        as_of=as_of,
        horizon=horizon,
        score=round(score, 2),
        direction=direction,
        confidence=round(confidence, 4),
        conflict=round(conflict, 4),
        evidence_event_ids=active_event_ids,
        components={
            "research_score": round(research_score, 2),
            "decision_score": round(score, 2),
            "events": contributions,
        },
    )


def _snapshot_state(snapshot: SecuritySignalSnapshot) -> tuple[object, ...]:
    """Comparable state that deliberately excludes timestamp and rank."""
    return (
        snapshot.horizon,
        snapshot.score,
        snapshot.direction,
        snapshot.confidence,
        snapshot.conflict,
        tuple(snapshot.evidence_event_ids or []),
        snapshot.components,
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
    ordered_snapshots = select(
        SecuritySignalSnapshot.id,
        func.row_number()
        .over(
            partition_by=(
                SecuritySignalSnapshot.security_id,
                SecuritySignalSnapshot.horizon,
            ),
            order_by=(SecuritySignalSnapshot.as_of.desc(), SecuritySignalSnapshot.id.desc()),
        )
        .label("recency"),
    ).subquery()
    latest_ids = select(ordered_snapshots.c.id).where(
        ordered_snapshots.c.recency == 1
    )
    latest = {
        (item.security_id, item.horizon): item
        for item in session.scalars(
            select(SecuritySignalSnapshot).where(SecuritySignalSnapshot.id.in_(latest_ids))
        )
    }
    created: list[SecuritySignalSnapshot] = []
    for security in securities:
        for horizon in HORIZONS:
            snapshot = aggregate_security(security, horizon, timestamp)
            previous = latest.get((security.id, horizon))
            if snapshot is None and previous is not None:
                snapshot = SecuritySignalSnapshot(
                    security_id=security.id,
                    as_of=timestamp,
                    horizon=horizon,
                    score=0.0,
                    direction="neutral",
                    confidence=0.0,
                    conflict=0.0,
                    rank=None,
                    evidence_event_ids=[],
                    components={"research_score": 0.0, "decision_score": 0.0, "events": []},
                )
            if snapshot and (
                previous is None or _snapshot_state(previous) != _snapshot_state(snapshot)
            ):
                session.add(snapshot)
                created.append(snapshot)
    session.flush()
    if created:
        current = dict(latest)
        current.update({(item.security_id, item.horizon): item for item in created})
        security_markets = {item.id: item.market for item in securities}
        for market in {security_markets[item.security_id] for item in created}:
            for horizon in HORIZONS:
                for side in ("bullish", "bearish"):
                    ranked = sorted(
                        (
                            item
                            for item in current.values()
                            if item.horizon == horizon
                            and item.direction == side
                            and security_markets.get(item.security_id) == market
                        ),
                        key=lambda item: (
                            abs(item.score),
                            item.confidence,
                            -item.security_id,
                        ),
                        reverse=True,
                    )
                    ranks = {
                        item.security_id: rank for rank, item in enumerate(ranked, 1)
                    }
                    for item in created:
                        if (
                            item.horizon == horizon
                            and item.direction == side
                            and security_markets[item.security_id] == market
                        ):
                            item.rank = ranks.get(item.security_id)
    session.commit()
    return created
