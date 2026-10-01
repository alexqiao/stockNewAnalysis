from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security
from trade_news_analysis.risk_models import ActionDecisionSnapshot, ActionEvaluationResult
from trade_news_analysis.services.action_evaluation import (
    _digest,
    calibration_report,
    evaluate_action_snapshots,
    record_action_snapshot,
)
from trade_news_analysis.services.decision_policy import build_decision_layers

from .test_market_research import FixtureProvider, apple, market_frame

RECORDED = datetime(2026, 9, 14, 22, tzinfo=UTC)
MATURE = datetime(2026, 10, 15, 22, tzinfo=UTC)


def record(
    session: Session,
    *,
    horizon: int = 5,
    now: datetime = RECORDED,
    ready: bool = False,
    evidence_id: int = 1,
    costs: bool = True,
    direction: str = "positive",
    quote_price: float = 100,
) -> dict[str, Any]:
    return record_action_snapshot(
        session,
        apple(session).id,
        horizon,
        {
            "action": {"code": "review", "policy_version": "baseline"},
            "strategy": {
                "policy_version": "candidate-v1",
                "candidate": {"code": direction},
                "execution": {"ready": ready},
            },
        },
        signal={"evidence_event_ids": [evidence_id], "confidence": 0.7},
        market_research={
            "quote": {"price": quote_price},
            "benchmark": {
                "market": "US",
                "symbol": "XLK",
                "currency": "USD",
            },
        },
        risk_plan={
            "costs": {"fee_bps": 10 if costs else None, "slippage_bps": 5 if costs else None}
        },
        now=now,
    )


def provider() -> FixtureProvider:
    frame = market_frame(start="2026-09-01", end="2026-10-15")
    frame["Close"] = frame["Adj Close"] = 102
    frame["High"] = frame["Adj High"] = 103
    benchmark = market_frame(start="2026-09-01", end="2026-10-15")
    benchmark["Close"] = benchmark["Adj Close"] = 101
    return FixtureProvider({"AAPL": frame, "XLK": benchmark})


def test_snapshots_are_deduplicated_and_versioned_inputs_cannot_be_mutated(
    session: Session,
) -> None:
    first = record(session)
    duplicate = record(session, now=RECORDED + timedelta(minutes=10))
    changed = record(session, ready=True)
    assert first["created"] is True
    assert duplicate["created"] is False
    assert duplicate["id"] == first["id"]
    assert changed["id"] != first["id"]
    snapshot = session.get(ActionDecisionSnapshot, first["id"])
    assert snapshot is not None
    assert snapshot.policy_version == "candidate-v1"
    snapshot.direction = "bearish"
    with pytest.raises(ValueError, match="不可修改"):
        session.flush()
    session.rollback()


def test_forward_windows_use_actual_sessions_costs_and_industry_comparison(
    session: Session,
    settings: Settings,
) -> None:
    for horizon in (1, 5, 20):
        record(session, horizon=horizon, ready=True)
    partial = evaluate_action_snapshots(
        session, settings, now=datetime(2026, 9, 16, 22, tzinfo=UTC), provider=provider()
    )
    assert partial["completed"] == 1
    assert partial["pending"] == 2
    finished = evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    assert finished["completed"] == 2
    outcomes = list(session.scalars(select(ActionEvaluationResult)))
    assert len(outcomes) == 3
    five = next(item.payload for item in outcomes if item.payload["horizon"] == 5)
    assert five["entry_date"] == "2026-09-15"
    assert five["exit_date"] == "2026-09-21"
    assert five["absolute_return_pct"] == pytest.approx(2)
    assert five["industry_excess_return_pct"] == pytest.approx(1)
    assert five["net_long_return_pct"] < five["absolute_return_pct"]
    assert five["max_adverse_excursion_pct"] == pytest.approx(-2)
    assert (
        evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())["completed"]
        == 0
    )


def test_action_evaluation_allows_other_writers_during_every_price_request(
    session: Session, session_factory: SessionFactory, settings: Settings,
) -> None:
    securities = session.scalars(
        select(Security).where(Security.symbol.in_(("AAPL", "MSFT"))).order_by(Security.symbol)
    ).all()
    for security, benchmark in zip(securities, ("XLK", "SPY"), strict=True):
        record_action_snapshot(
            session, security.id, 1,
            {"action": {"code": "hold", "policy_version": "test"}},
            signal={"direction": "bullish"},
            market_research={"benchmark": {"market": "US", "symbol": benchmark}},
            risk_plan={"costs": {"fee_bps": 10, "slippage_bps": 5}}, now=RECORDED,
        )
    session.commit()
    session.expunge_all()
    writes: list[str] = []
    data = market_frame(start="2026-09-01", end="2026-10-15")

    class ConcurrentWriterProvider(FixtureProvider):
        def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
            with session_factory() as writer:
                writer.execute(text("PRAGMA busy_timeout=100"))
                writer.execute(update(Security).where(Security.symbol == "AAPL").values(
                    business_summary=f"Concurrent write during {symbol}"
                ))
                writer.commit()
            writes.append(symbol)
            return super().history(market, symbol, period)

    result = evaluate_action_snapshots(
        session, settings, now=MATURE,
        provider=ConcurrentWriterProvider(dict.fromkeys(("AAPL", "XLK", "MSFT", "SPY"), data)),
    )
    assert writes == ["AAPL", "XLK", "MSFT", "SPY"]
    assert result["completed"] == 2
    assert result["pending"] == 0
    assert len(session.scalars(select(ActionEvaluationResult)).all()) == 2
    session.rollback()
    with session_factory() as reader:
        assert list(reader.scalars(select(ActionEvaluationResult))) == []


def test_cost_unknown_is_preserved_and_negative_event_direction_uses_exit_counterfactual(
    session: Session,
    settings: Settings,
) -> None:
    record(session, costs=False)
    negative = record(session, direction="negative")
    evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    rows = list(session.scalars(select(ActionEvaluationResult)))
    unknown = next(item.payload for item in rows if item.snapshot_id != negative["id"])
    assert unknown["costs_known"] is False
    assert unknown["net_long_return_pct"] is None
    short = next(item.payload for item in rows if item.snapshot_id == negative["id"])
    assert short["directional_benefit_pct"] == pytest.approx(-2.15)
    assert "非做空收益" in short["interpretation"]


def test_missing_initial_history_never_moves_the_entry_to_a_later_year(
    session: Session,
    settings: Settings,
) -> None:
    record(session, now=datetime(2025, 9, 14, 22, tzinfo=UTC))
    result = evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    assert result["completed"] == 0
    assert result["pending"] == 1


def test_zero_volume_cannot_be_an_entry_or_count_towards_the_horizon(
    session: Session,
    settings: Settings,
) -> None:
    record(session, now=datetime(2026, 9, 11, 22, tzinfo=UTC))
    data_provider = provider()
    frame = data_provider.frames["AAPL"]
    assert not isinstance(frame, Exception)
    frame.loc["2026-09-14", "Volume"] = 0
    pending = evaluate_action_snapshots(
        session, settings, now=datetime(2026, 9, 18, 22, tzinfo=UTC), provider=data_provider
    )
    assert pending["completed"] == 0
    assert pending["pending"] == 1
    complete = evaluate_action_snapshots(session, settings, now=MATURE, provider=data_provider)
    assert complete["completed"] == 1
    outcome = session.scalar(select(ActionEvaluationResult))
    assert outcome is not None
    assert outcome.payload["entry_date"] == "2026-09-15"
    assert outcome.payload["exit_date"] == "2026-09-21"


def test_unverified_adjustments_never_enter_calibration(
    session: Session, settings: Settings
) -> None:
    record(session)
    data_provider = provider()
    frame = data_provider.frames["AAPL"]
    assert not isinstance(frame, Exception)
    frame.attrs["adjustment_status"] = "unavailable"
    result = evaluate_action_snapshots(session, settings, now=MATURE, provider=data_provider)
    assert result["completed"] == 0
    assert result["pending"] == 1


def test_calibration_separates_readiness_versions_and_repeated_event_samples(
    session: Session,
    settings: Settings,
) -> None:
    record(session, evidence_id=1)
    record(session, evidence_id=1, quote_price=101)
    record(session, evidence_id=2)
    record(session, evidence_id=3, ready=True)
    record(session, horizon=20, now=MATURE)
    evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    report = calibration_report(session, min_samples=2)

    assert report["total_snapshot_count"] == 5
    assert report["completed_count"] == 4
    assert report["pending_count"] == 1
    observations = next(group for group in report["groups"] if not group["execution_ready"])
    assert observations["independent_sample_count"] == 2
    assert observations["duplicates_excluded"] == 1
    assert observations["direction_accuracy"] == 1
    assert observations["industry_excess_sample_count"] == 2
    assert observations["industry_excess_return_percentiles"]["0.5"] == pytest.approx(1)
    execution = next(group for group in report["groups"] if group["execution_ready"])
    assert execution["status"] == "insufficient_samples"
    assert execution["direction_accuracy"] is None
    assert execution["calibrated_probability"] is None


def test_action_calibration_keeps_evidence_rules_separate(
    session: Session, settings: Settings,
) -> None:
    saved_ids = []
    for components in ({}, {"evidence_rule_version": "original-sources-v2"}):
        result = record_action_snapshot(
            session, apple(session).id, 1,
            {"action": {"code": "buy_candidate", "policy_version": "same-policy"}},
            signal={"direction": "bullish", "confidence": .8, "evidence_event_ids": [1],
                    "components": components},
            now=RECORDED,
        )
        saved_ids.append(result["id"])
    assert saved_ids[0] != saved_ids[1]
    evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    groups = calibration_report(session)["groups"]
    assert {row["evidence_rule_version"] for row in groups} == {
        "legacy-v1", "original-sources-v2",
    }
    assert all(row["independent_sample_count"] == 1 for row in groups)


@pytest.mark.parametrize("risk", [
    {"status": "stop_triggered"},
    {"status": "ready", "required_reduce_quantity": 100, "max_buy_quantity": 0},
])
def test_snapshot_records_execution_reduction_without_changing_event_direction(
    session: Session, settings: Settings, risk: dict[str, Any],
) -> None:
    signal = {
        "as_of": RECORDED, "direction": "bullish", "confidence": 0.8,
        "conflict": 0.1, "decision_score": 40, "evidence_event_ids": [1],
    }
    strategy = build_decision_layers(
        signal, {}, [{"event_id": 1, "fact_time_verified": True,
                      "first_disclosed_at": RECORDED}],
        {"status": "ready"}, risk, holding_status="long", horizon=5, now=RECORDED,
    )
    assert strategy["execution"]["action_code"] == "reduce"
    result = record_action_snapshot(
        session, apple(session).id, 5,
        {"action": {"code": "hold"}, "strategy": strategy}, signal=signal,
        risk_plan={"costs": {"fee_bps": 10, "slippage_bps": 5}}, now=RECORDED,
    )
    saved = session.get(ActionDecisionSnapshot, result["id"])
    assert saved is not None
    assert saved.action_code == "reduce"
    assert saved.direction == "bullish"
    assert saved.inputs["judgment"]["action"]["code"] == "hold"
    assert saved.inputs["recording_version"] == "execution-actions-v2"
    evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    outcome = session.scalar(select(ActionEvaluationResult).where(
        ActionEvaluationResult.snapshot_id == saved.id,
    ))
    assert outcome is not None
    assert outcome.payload["direction_correct_gross"] is True
    assert outcome.payload["directional_benefit_pct"] > 0
    assert "不代表" in outcome.payload["interpretation"]


def test_execution_review_overrides_old_hold_and_controls_deduplication(session: Session) -> None:
    judgment: dict[str, Any] = {
        "action": {"code": "hold"},
        "strategy": {
            "policy_version": "same-policy",
            "candidate": {"code": "positive"},
            "execution": {"action_code": "review", "ready": False},
        },
    }
    identifier = apple(session).id
    first = record_action_snapshot(session, identifier, 5, judgment, now=RECORDED)
    reduced = deepcopy(judgment)
    reduced["strategy"]["execution"]["action_code"] = "reduce"
    second = record_action_snapshot(session, identifier, 5, reduced, now=RECORDED)
    assert second["created"] is True
    assert second["id"] != first["id"]
    changed_legacy = deepcopy(judgment)
    changed_legacy["action"]["code"] = "sell_candidate"
    repeated = record_action_snapshot(session, identifier, 5, changed_legacy, now=RECORDED)
    assert repeated["id"] == first["id"]
    assert repeated["created"] is False
    saved = session.get(ActionDecisionSnapshot, first["id"])
    assert saved is not None
    assert saved.action_code == "review"
    assert saved.inputs["judgment"] == judgment


@pytest.mark.parametrize("strategy", [{}, {"execution": {"ready": True}}])
def test_incomplete_strategy_does_not_fall_back_to_old_hold(
    session: Session, strategy: dict[str, Any],
) -> None:
    result = record_action_snapshot(
        session, apple(session).id, 5, {"action": {"code": "hold"}, "strategy": strategy},
        signal={"direction": "bullish"}, now=RECORDED,
    )
    saved = session.get(ActionDecisionSnapshot, result["id"])
    assert saved is not None
    assert saved.action_code == "review"
    assert saved.direction == "neutral"


def test_recording_without_strategy_retains_legacy_action_compatibility(session: Session) -> None:
    result = record_action_snapshot(
        session, apple(session).id, 5,
        {"action": {"code": "hold", "policy_version": "old-rule"}},
        signal={"direction": "bullish"}, now=RECORDED,
    )
    saved = session.get(ActionDecisionSnapshot, result["id"])
    assert saved is not None
    assert saved.action_code == "hold"
    assert saved.policy_version == "old-rule"
    assert saved.direction == "bullish"
    assert saved.inputs["execution_ready"] is True
    assert saved.inputs["recording_version"] == "execution-actions-v2"


def test_new_record_does_not_reuse_matching_legacy_dedupe_key(session: Session) -> None:
    identifier = apple(session).id
    # Freeze the previous identity format: even identical actions need a new recording version.
    legacy_key = _digest({
        "security_id": identifier, "horizon": 5, "policy_version": "candidate-v1",
        "action_code": "review", "candidate": {"code": "positive"}, "execution_ready": False,
        "signal": {"evidence_event_ids": [1], "confidence": 0.7}, "quote": {"price": 100},
        "horizons": None, "benchmark": {"market": "US", "symbol": "XLK", "currency": "USD"},
        "risk_inputs": None, "risk_costs": {"fee_bps": 10, "slippage_bps": 5},
        "valuation": None, "pricing": None,
    })
    original_inputs = {"judgment": {"action": {"code": "review"}}, "legacy_note": "保留原文"}
    legacy = ActionDecisionSnapshot(
        security_id=identifier, horizon=5, as_of=RECORDED, policy_version="candidate-v1",
        market="US", action_code="review", direction="bullish", confidence=0.7,
        dedupe_key=legacy_key, inputs=deepcopy(original_inputs),
    )
    session.add(legacy)
    session.flush()
    created = record(session)
    assert created["created"] is True
    assert created["id"] != legacy.id
    assert created["dedupe_key"] != legacy_key
    assert record(session)["id"] == created["id"]
    session.expire(legacy)
    assert legacy.inputs == original_inputs


def test_calibration_separates_legacy_recording_without_rewriting_historical_inputs(
    session: Session, settings: Settings,
) -> None:
    originals = []
    for evidence_id in (1, 2):
        result = record(session, evidence_id=evidence_id)
        current = session.get(ActionDecisionSnapshot, result["id"])
        assert current is not None
        old_inputs = deepcopy(current.inputs)
        old_inputs.pop("recording_version")
        old = ActionDecisionSnapshot(
            security_id=current.security_id, horizon=current.horizon, as_of=current.as_of,
            policy_version=current.policy_version, market=current.market, action_code="hold",
            direction=current.direction, confidence=current.confidence,
            dedupe_key=f"legacy-{evidence_id}", inputs=old_inputs,
        )
        session.add(old)
        session.flush()
        originals.append((old.id, deepcopy(old_inputs)))
    evaluate_action_snapshots(session, settings, now=MATURE, provider=provider())
    report = calibration_report(session, min_samples=2)
    assert report["completed_count"] == 4
    assert len(report["groups"]) == 2
    assert {group["recording_version"] for group in report["groups"]} == {
        "legacy-v1", "execution-actions-v2",
    }
    for group in report["groups"]:
        assert group["independent_sample_count"] == 2
        assert group["duplicates_excluded"] == 0
    for identifier, inputs in originals:
        historical = session.get(ActionDecisionSnapshot, identifier)
        assert historical is not None
        assert historical.inputs == inputs
        assert historical.action_code == "hold"
    assert record(session)["created"] is False
    assert "不代表减仓或退出动作的胜率" in report["note"]
