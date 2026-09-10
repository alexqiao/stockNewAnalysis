from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Event, EventSecurityImpact, Security
from trade_news_analysis.services.scoring import (
    aggregate_security,
    calculate_opportunity_score,
    evidence_quality,
    freshness_decay,
    summarize_components,
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


def test_summarize_components_supports_legacy_snapshot_schema() -> None:
    research_score, decision_score, confidence, conflict, direction = (
        summarize_components(
            [
                {
                    "event_id": 1,
                    "direction": "bullish",
                    "opportunity_score": 80.0,
                    "confidence": 0.5,
                    "decay": 0.5,
                    "contribution": 20.0,
                }
            ]
        )
    )

    assert research_score == 80.0
    assert decision_score == 20.0
    assert confidence == 0.25
    assert conflict == 0.0
    assert direction == "bullish"


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


def test_single_event_score_decays_and_expires(session: Session) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    occurred_at = datetime(2026, 8, 3, 12, tzinfo=UTC)
    event = Event(event_key="decay", title="新增订单", status="complete", occurred_at=occurred_at)
    session.add(event)
    session.flush()
    session.add(
        EventSecurityImpact(
            event_id=event.id,
            security_id=security.id,
            status="complete",
            opportunity_score=80,
            impacts={
                str(horizon): {
                    "direction": "bullish",
                    "confidence": 0.8,
                    "reason": event.title,
                }
                for horizon in (1, 5, 20)
            },
        )
    )
    session.commit()
    session.refresh(security)

    fresh = aggregate_security(security, 5, occurred_at)
    older = aggregate_security(security, 5, occurred_at + timedelta(days=7))
    expired = aggregate_security(security, 5, occurred_at + timedelta(days=28))

    assert fresh is not None and older is not None and expired is not None
    assert fresh.components["research_score"] == 80
    assert fresh.score == 64
    assert 0 < older.score < fresh.score
    assert expired.score == 0
    assert expired.direction == "neutral"


def test_rebuild_skips_unchanged_states_and_ranks_within_market(session: Session) -> None:
    from trade_news_analysis.services.scoring import rebuild_signal_snapshots

    securities = session.scalars(
        select(Security).where(Security.symbol.in_(["AAPL", "MSFT"]))
    ).all()
    now = datetime(2026, 8, 3, 12, tzinfo=UTC)
    for index, security in enumerate(securities, 1):
        event = Event(
            event_key=f"rank-{index}",
            title="新增订单",
            status="complete",
            occurred_at=now,
        )
        session.add(event)
        session.flush()
        session.add(
            EventSecurityImpact(
                event_id=event.id,
                security_id=security.id,
                status="complete",
                opportunity_score=80 - index,
                impacts={
                    str(horizon): {
                        "direction": "bullish",
                        "confidence": 0.8,
                        "reason": event.title,
                    }
                    for horizon in (1, 5, 20)
                },
            )
        )
    session.commit()

    first = rebuild_signal_snapshots(session, now)
    second = rebuild_signal_snapshots(session, now + timedelta(minutes=30))

    assert len(first) == 6
    assert second == []
    ranks = [item.rank for item in first if item.horizon == 5]
    assert all(rank is not None for rank in ranks)
    assert sorted(int(rank) for rank in ranks if rank is not None) == [1, 2]
