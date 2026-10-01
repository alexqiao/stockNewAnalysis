from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.risk_models import (
    ActionDecisionSnapshot,
    ActionEvaluationResult,
    ActionValidationState,
    SignalEvaluationAudit,
)
from trade_news_analysis.services.action_evaluation import (
    ACTION_EVALUATION_VERSION,
    calibration_report,
    evaluate_action_snapshots,
    retry_action_validation,
)
from trade_news_analysis.services.evaluation import OutcomeEvaluator

from .test_action_evaluation import MATURE, RECORDED, provider, record
from .test_evaluation import FakeProvider, add_snapshot
from .test_market_research import FixtureProvider, apple, market_frame


@pytest.mark.parametrize("problem", [
    "history_missing", "volume_unknown", "adjustment_unverified", "currency_mismatch",
    "source_failed",
])
def test_pending_reasons_are_distinct_and_backoff_avoids_repeated_requests(
    session: Session, settings: Settings, problem: str,
) -> None:
    saved = record(session)
    data = provider()
    prices = data.frames["AAPL"]
    assert isinstance(prices, pd.DataFrame)
    if problem == "history_missing":
        data.frames["AAPL"] = prices.drop(pd.Timestamp("2026-09-15"))
    elif problem == "volume_unknown":
        prices.loc["2026-09-15", "Volume"] = None
    elif problem == "adjustment_unverified":
        prices.attrs["adjustment_status"] = "unknown"
    elif problem == "currency_mismatch":
        prices.attrs["currency"] = "HKD"
    else:
        data.frames["AAPL"] = ValueError("provider unavailable")
    result = evaluate_action_snapshots(session, settings, now=MATURE, provider=data)
    assert result["completed"] == 0
    assert result["pending_details"][0]["status"] == problem
    state = session.get(ActionValidationState, saved["id"])
    assert state is not None
    assert state.attempts == 1
    if problem in {"history_missing", "volume_unknown"}:
        assert state.missing_dates == ["2026-09-15"]
    call_count = len(data.calls)
    assert evaluate_action_snapshots(session, settings, now=MATURE, provider=data)["processed"] == 0
    assert len(data.calls) == call_count
    retry_action_validation(session, saved["id"], MATURE)
    evaluate_action_snapshots(session, settings, now=MATURE, provider=data)
    assert state.attempts == 2


def test_immature_windows_schedule_at_real_close_without_network(
    session: Session, settings: Settings,
) -> None:
    saved = record(session, horizon=5)
    data = provider()
    result = evaluate_action_snapshots(session, settings, now=RECORDED, provider=data)
    assert result["pending_details"][0]["status"] == "not_mature"
    assert data.calls == []
    state = session.get(ActionValidationState, saved["id"])
    assert state is not None
    assert state.window_start == date(2026, 9, 15)
    assert state.window_end == date(2026, 9, 21)
    assert state.due_at == datetime(2026, 9, 21, 20)
    assert state.next_attempt_at == state.due_at


def test_validation_queue_is_bounded_and_new_rows_are_not_starved(
    session: Session, settings: Settings,
) -> None:
    identifier = apple(session).id
    for index in range(501):
        session.add(ActionDecisionSnapshot(
            security_id=identifier, horizon=20, as_of=MATURE, policy_version="queue-test",
            market="US", action_code="review", direction="neutral", dedupe_key=f"queued-{index}",
            inputs={},
        ))
    session.commit()
    data = provider()
    first = evaluate_action_snapshots(session, settings, now=MATURE, provider=data)
    assert first["processed"] == 500
    report = calibration_report(session)
    counts = {row["status"]: row["count"] for row in report["pending_by_reason"]}
    assert counts == {"not_mature": 500, "unassessed": 1}
    assert len(report["pending_samples"]) == 50
    second = evaluate_action_snapshots(session, settings, now=MATURE, provider=data)
    assert second["processed"] == 1
    assert data.calls == []


def test_old_action_requests_entry_year_and_persists_exact_window_and_metadata(
    session: Session, settings: Settings,
) -> None:
    saved = record(session, now=datetime(2024, 1, 1, 22, tzinfo=UTC))
    calls = []

    class HistoricalProvider(FixtureProvider):
        def history_range(self, market, symbol, start, end, provider_data=None):
            calls.append((symbol, start, end))
            return market_frame(start="2024-01-02", end="2024-01-08")

    result = evaluate_action_snapshots(
        session, settings, now=MATURE, provider=HistoricalProvider({}),
    )
    assert result["completed"] == 1
    assert calls[0][1] == date(2024, 1, 2)
    assert all(end == date(2024, 1, 9) for _, _, end in calls)
    outcome = session.scalar(select(ActionEvaluationResult).where(
        ActionEvaluationResult.snapshot_id == saved["id"],
    ))
    assert outcome is not None
    assert outcome.payload["entry_date"] == "2024-01-02"
    assert outcome.payload["exit_date"] == "2024-01-08"
    assert len(outcome.payload["window_bars"]) == 5
    benchmark = outcome.payload["benchmark_window_bars"]
    assert len(benchmark) == 5
    benchmark_return = (benchmark[-1]["adj_close"] / benchmark[0]["adj_open"] - 1) * 100
    assert outcome.payload["industry_excess_return_pct"] == pytest.approx(
        outcome.payload["absolute_return_pct"] - benchmark_return
    )
    assert outcome.payload["market_metadata"]["source_version"]
    assert outcome.payload["evaluation_version"] == ACTION_EVALUATION_VERSION
    with pytest.raises(ValueError, match="不可变"):
        retry_action_validation(session, saved["id"])


def test_old_signal_requests_exact_year_and_preserves_input_audit(session: Session) -> None:
    snapshot = add_snapshot(session, datetime(2024, 1, 1, 22, tzinfo=UTC))
    calls = []

    class HistoricalProvider(FakeProvider):
        def history_range(self, market, symbol, start, end, provider_data=None):
            calls.append((symbol, start, end))
            return market_frame(start="2024-01-02", end="2024-01-08")

        def benchmark_history_range(self, market, start, end):
            calls.append(("benchmark", start, end))
            return market_frame(start="2024-01-02", end="2024-01-08")

    assert OutcomeEvaluator(provider=HistoricalProvider()).evaluate(session, now=MATURE) == 1
    assert calls[0][1] == date(2024, 1, 2)
    assert all(end == date(2024, 1, 9) for _, _, end in calls)
    audit = session.get(SignalEvaluationAudit, snapshot.id)
    assert audit is not None
    assert audit.payload["window_dates"] == ["2024-01-02", "2024-01-03", "2024-01-04",
                                              "2024-01-05", "2024-01-08"]
    assert audit.payload["stock_metadata"]["source_version"]
    assert audit.evaluation_version == "strict-sessions-v3"
    assert session.scalar(select(func.count()).select_from(SignalEvaluationAudit)) == 1


def test_longstanding_missing_history_backs_off_to_weekly(session: Session, settings: Settings):
    saved = record(session)
    bad = FixtureProvider({"AAPL": RuntimeError("unavailable")})
    for index in range(5):
        when = MATURE + timedelta(days=index * 8)
        evaluate_action_snapshots(session, settings, now=when, provider=bad)
    state = session.get(ActionValidationState, saved["id"])
    assert state is not None
    assert state.next_attempt_at - state.last_attempt_at == timedelta(days=7)
