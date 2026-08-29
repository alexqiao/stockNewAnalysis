from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security, SecuritySignalSnapshot, SignalOutcome
from trade_news_analysis.services.metrics import _rank_ic_summary, build_metrics


def metric_row(
    symbol: str,
    score: float,
    excess_return: float,
    as_of: datetime,
) -> tuple[SignalOutcome, SecuritySignalSnapshot, Security]:
    security = Security(market="US", exchange="NASDAQ", symbol=symbol, name=symbol)
    snapshot = SecuritySignalSnapshot(
        security=security,
        as_of=as_of,
        horizon=5,
        score=score,
        direction="bullish",
    )
    outcome = SignalOutcome(snapshot=snapshot, excess_return_pct=excess_return)
    return outcome, snapshot, security


def test_rank_ic_summary_handles_periods_ties_and_icir() -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = first + timedelta(days=1)
    rows = [
        metric_row("A", 1, 10, first),
        metric_row("B", 1, 10, first),
        metric_row("C", 2, 20, first),
        metric_row("A", 1, 30, second),
        metric_row("B", 2, 20, second),
        metric_row("C", 3, 10, second),
    ]

    result = _rank_ic_summary(rows)

    assert result["rank_ic_periods"] == 2
    assert result["rank_ic_mean"] == pytest.approx(0)
    assert result["rank_ic_std"] == pytest.approx(2**0.5)
    assert result["rank_icir"] == pytest.approx(0)
    assert result["rank_ic_positive_rate"] == pytest.approx(0.5)


def test_rank_ic_summary_rejects_small_or_constant_cross_sections() -> None:
    as_of = datetime(2026, 1, 1, tzinfo=UTC)
    too_small = [metric_row("A", 1, 1, as_of), metric_row("B", 2, 2, as_of)]
    constant = [
        metric_row("A", 1, 1, as_of),
        metric_row("B", 1, 2, as_of),
        metric_row("C", 1, 3, as_of),
    ]
    assert _rank_ic_summary(too_small)["rank_ic_periods"] == 0
    assert _rank_ic_summary(constant)["rank_ic_mean"] is None


def test_rank_icir_is_unavailable_when_period_values_have_zero_deviation() -> None:
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = first + timedelta(days=1)
    rows = [
        metric_row(symbol, score, score, as_of)
        for as_of in (first, second)
        for symbol, score in (("A", 1), ("B", 2), ("C", 3))
    ]
    result = _rank_ic_summary(rows)
    assert result["rank_ic_periods"] == 2
    assert result["rank_ic_std"] == pytest.approx(0)
    assert result["rank_icir"] is None


def test_build_metrics_uses_full_cross_section_for_rank_ic(session: Session) -> None:
    securities = session.scalars(
        select(Security).where(Security.market == "US").order_by(Security.id).limit(3)
    ).all()
    assert len(securities) == 3
    as_of = datetime(2026, 1, 5, 12, tzinfo=UTC)
    observed_at = as_of + timedelta(days=10)
    for rank, (security, score, excess_return) in enumerate(
        zip(securities, [10.0, 20.0, 30.0], [1.0, 2.0, 3.0], strict=True),
        1,
    ):
        snapshot = SecuritySignalSnapshot(
            security_id=security.id,
            as_of=as_of,
            horizon=5,
            score=score,
            direction="bullish",
            confidence=0.8,
            conflict=0,
            rank=rank,
        )
        session.add(snapshot)
        session.flush()
        session.add(
            SignalOutcome(
                snapshot_id=snapshot.id,
                baseline_at=as_of,
                observed_at=observed_at,
                entry_price=100,
                exit_price=101 + excess_return,
                benchmark_entry=100,
                benchmark_exit=101,
                return_pct=1 + excess_return,
                benchmark_return_pct=1,
                excess_return_pct=excess_return,
                predicted_direction="bullish",
                actual_direction="bullish",
                correct=True,
            )
        )
    session.commit()

    result = build_metrics(session, top_k=1)
    assert result["sample_size"] == 1
    assert result["rank_ic_periods"] == 1
    assert result["rank_ic_mean"] == pytest.approx(1)
    assert result["by_market"]["US"]["rank_ic_mean"] == pytest.approx(1)

    assert build_metrics(session, market="A")["rank_ic_periods"] == 0
    assert build_metrics(session, market="US", horizon=1)["rank_ic_periods"] == 0
    assert build_metrics(session, market="US", horizon=5)["rank_ic_periods"] == 1

    single_security = build_metrics(session, security_id=securities[0].id)
    assert single_security["rank_ic_periods"] == 0
    assert single_security["rank_ic_mean"] is None
