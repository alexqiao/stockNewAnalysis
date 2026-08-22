from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Event, EventSecurityImpact, Security
from trade_news_analysis.services.scoring import (
    aggregate_security,
    calculate_opportunity_score,
    evidence_quality,
    freshness_decay,
)


def test_weighted_score_and_source_corroboration() -> None:
    values = {
        "demand_certainty": 5,
        "transmission_clarity": 5,
        "business_purity": 5,
        "scale_elasticity": 5,
        "market_neglect": 5,
        "novelty_unpriced": 5,
        "evidence_quality": 5,
        "verification_speed": 5,
    }
    assert calculate_opportunity_score(values, risk_penalty=10) == 90
    assert evidence_quality(1) == 2
    assert evidence_quality(3) == 5
    assert freshness_decay(5, 5) == 0.5


def test_opposing_events_create_high_conflict(session: Session) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    bullish = Event(event_key="bull", title="需求上升", status="complete", occurred_at=now)
    bearish = Event(event_key="bear", title="订单取消", status="complete", occurred_at=now)
    session.add_all([bullish, bearish])
    session.flush()
    for event, direction in ((bullish, "bullish"), (bearish, "bearish")):
        session.add(
            EventSecurityImpact(
                event_id=event.id,
                security_id=security.id,
                status="complete",
                opportunity_score=80,
                impacts={
                    str(horizon): {
                        "direction": direction,
                        "confidence": 0.8,
                        "reason": event.title,
                    }
                    for horizon in (1, 5, 20)
                },
            )
        )
    session.commit()
    session.refresh(security)
    snapshot = aggregate_security(security, 5, now)
    assert snapshot is not None
    assert snapshot.direction == "neutral"
    assert snapshot.score == 0
    assert snapshot.conflict == 1
