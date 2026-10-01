from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security, SecuritySignalSnapshot, SignalOutcome
from trade_news_analysis.services.evaluation import EVALUATION_VERSION
from trade_news_analysis.services.evidence import EVIDENCE_RULE_VERSION
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
        components={"research_score": score, "decision_score": score,
                    "evidence_rule_version": EVIDENCE_RULE_VERSION},
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
            components={"research_score": score, "decision_score": score,
                    "evidence_rule_version": EVIDENCE_RULE_VERSION},
        )
        session.add(snapshot)
        session.flush()
        session.add(
            SignalOutcome(
                evaluation_version=EVALUATION_VERSION,
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


def test_build_metrics_excludes_unranked_and_deduplicates_entry_session(
    session: Session,
) -> None:
    securities = session.scalars(
        select(Security).where(Security.market == "US").order_by(Security.id).limit(2)
    ).all()
    assert len(securities) == 2
    as_of = datetime(2026, 1, 5, 12, tzinfo=UTC)
    baseline = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    for offset in (0, 1):
        snapshot = SecuritySignalSnapshot(
            security_id=securities[0].id,
            as_of=as_of + timedelta(hours=offset),
            horizon=5,
            score=20 + offset,
            direction="bullish",
            confidence=0.8,
            rank=1,
            components={"research_score": 20 + offset, "decision_score": 20 + offset,
                        "evidence_rule_version": EVIDENCE_RULE_VERSION},
        )
        session.add(snapshot)
        session.flush()
        session.add(
            SignalOutcome(
                evaluation_version=EVALUATION_VERSION,
                snapshot_id=snapshot.id,
                baseline_at=baseline,
                observed_at=baseline + timedelta(days=7),
                entry_price=100,
                exit_price=102,
                benchmark_entry=100,
                benchmark_exit=101,
                return_pct=2,
                benchmark_return_pct=1,
                excess_return_pct=1,
                predicted_direction="bullish",
                actual_direction="bullish",
                correct=True,
            )
        )
    unranked = SecuritySignalSnapshot(
        security_id=securities[1].id,
        as_of=as_of,
        horizon=5,
        score=-20,
        direction="bearish",
        confidence=0.8,
        rank=None,
        components={"research_score": -20, "decision_score": -20,
                    "evidence_rule_version": EVIDENCE_RULE_VERSION},
    )
    session.add(unranked)
    session.flush()
    session.add(
        SignalOutcome(
            evaluation_version=EVALUATION_VERSION,
            snapshot_id=unranked.id,
            baseline_at=baseline,
            observed_at=baseline + timedelta(days=7),
            entry_price=100,
            exit_price=98,
            benchmark_entry=100,
            benchmark_exit=101,
            return_pct=-2,
            benchmark_return_pct=1,
            excess_return_pct=-3,
            predicted_direction="bearish",
            actual_direction="bearish",
            correct=True,
        )
    )
    session.commit()

    result = build_metrics(session, market="US", horizon=5, top_k=10)

    assert result["sample_size"] == 1
    assert result["decision_periods"] == 1


@pytest.mark.parametrize("include_strict", [False, True])
def test_metrics_keep_evaluation_versions_separate(
    session: Session, include_strict: bool,
) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    baseline = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    versions = [("legacy-v1", -5.0)]
    if include_strict:
        versions.insert(0, (EVALUATION_VERSION, 5.0))
    for offset, (version, excess_return) in enumerate(versions):
        snapshot = SecuritySignalSnapshot(
            security_id=security.id, as_of=baseline - timedelta(hours=2 - offset),
            horizon=5, score=70, direction="bullish", rank=1, confidence=0.8,
            components={"decision_score": 70, "evidence_rule_version": (
                EVIDENCE_RULE_VERSION if version == EVALUATION_VERSION else "legacy-v1"
            )},
        )
        session.add(snapshot)
        session.flush()
        session.add(SignalOutcome(
            snapshot_id=snapshot.id, evaluation_version=version,
            baseline_at=baseline, observed_at=baseline + timedelta(days=7),
            entry_price=100, exit_price=100 + excess_return,
            benchmark_entry=100, benchmark_exit=100, return_pct=excess_return,
            benchmark_return_pct=0, excess_return_pct=excess_return,
            predicted_direction="bullish", actual_direction="bullish" if offset == 0 else "bearish",
            correct=excess_return > 0,
        ))
    session.commit()

    result = build_metrics(session, security_id=security.id)

    assert result["evaluation_version"] == EVALUATION_VERSION
    assert result["sample_size"] == int(include_strict)
    assert result["average_excess_return_pct"] == (5.0 if include_strict else None)
    versions_report = result["by_evaluation_version"]
    assert versions_report["legacy-v1"]["sample_size"] == 1
    assert versions_report["legacy-v1"]["average_excess_return_pct"] == -5
    assert versions_report[EVALUATION_VERSION]["sample_size"] == int(include_strict)


def test_same_validation_version_never_mixes_evidence_rules(session: Session) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    baseline = datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    rules = [None, EVIDENCE_RULE_VERSION, "mixed:legacy-v1+original-sources-v2"]
    for offset, rule in enumerate(rules):
        components: dict[str, Any] = {"decision_score": 70}
        if rule is not None:
            components["evidence_rule_version"] = rule
        snapshot = SecuritySignalSnapshot(
            security_id=security.id, as_of=baseline - timedelta(hours=3 - offset),
            horizon=1, score=70, direction="bullish", rank=1, components=components,
        )
        session.add(snapshot)
        session.flush()
        session.add(SignalOutcome(
            snapshot_id=snapshot.id, evaluation_version=EVALUATION_VERSION,
            baseline_at=baseline, observed_at=baseline + timedelta(hours=7),
            entry_price=100, exit_price=101 + offset, benchmark_entry=100, benchmark_exit=100,
            return_pct=1 + offset, benchmark_return_pct=0, excess_return_pct=1 + offset,
            predicted_direction="bullish", actual_direction="bullish", correct=True,
        ))
    session.commit()
    report = build_metrics(session)
    assert report["sample_size"] == 1
    assert report["average_excess_return_pct"] == 2
    assert report["evidence_rule_version"] == EVIDENCE_RULE_VERSION
    by_rule = report["by_evidence_rule_version"]
    assert {key: group["sample_size"] for key, group in by_rule.items()} == {
        "legacy-v1": 1, EVIDENCE_RULE_VERSION: 1, "mixed:legacy-v1+original-sources-v2": 1,
    }
    assert by_rule["legacy-v1"]["average_excess_return_pct"] == 1
