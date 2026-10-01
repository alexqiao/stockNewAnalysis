from __future__ import annotations

import logging
from datetime import UTC, datetime

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security, SecuritySignalSnapshot, SignalOutcome
from trade_news_analysis.services.evaluation import (
    EVALUATION_VERSION,
    OutcomeEvaluator,
    first_tradable_date,
)
from trade_news_analysis.services.normalization import ensure_aware


def frame(open_start: float, close_step: float) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-05", periods=30)
    opens = [open_start + index for index in range(30)]
    closes = [value + close_step for value in opens]
    return verified(pd.DataFrame({"Open": opens, "Close": closes}, index=index))


def verified(result: pd.DataFrame, currency: str = "USD") -> pd.DataFrame:
    result = result.copy()
    for field in ("Open", "Close"):
        result[f"Adj {field}"] = result[field]
    result.attrs.update(adjustment_status="verified", currency=currency,
                        analysis_price_basis="total_return_adjusted", source="fixture")
    return result


class FakeProvider:
    name = "fake"

    def __init__(self, with_benchmark: bool = True):
        self.with_benchmark = with_benchmark
        self.calls: list[tuple[str, str]] = []

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        self.calls.append((market, symbol))
        return frame(100, 3)

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        self.calls.append((market, "benchmark"))
        return frame(100, 0) if self.with_benchmark else pd.DataFrame()


def add_snapshot(
    session: Session, as_of: datetime, symbol: str = "AAPL"
) -> SecuritySignalSnapshot:
    security = session.scalar(select(Security).where(Security.symbol == symbol))
    assert security is not None
    snapshot = SecuritySignalSnapshot(
        security_id=security.id,
        as_of=as_of,
        horizon=5,
        score=70,
        direction="bullish",
        confidence=0.8,
        conflict=0,
        rank=1,
    )
    session.add(snapshot)
    session.commit()
    return snapshot


def test_first_tradable_date_uses_security_timezone() -> None:
    dates = [stamp.date() for stamp in pd.bdate_range("2026-01-05", periods=4)]
    before_open = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # 08:00 Shanghai
    after_open = datetime(2026, 1, 5, 4, 0, tzinfo=UTC)  # 12:00 Shanghai
    assert first_tradable_date(before_open, dates, "Asia/Shanghai") == dates[0]
    assert first_tradable_date(after_open, dates, "Asia/Shanghai") == dates[1]


def test_evaluator_creates_one_idempotent_snapshot_outcome(session: Session) -> None:
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    provider = FakeProvider()
    evaluator = OutcomeEvaluator(provider=provider)
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert evaluator.evaluate(session, now=now) == 1
    assert evaluator.evaluate(session, now=now) == 0
    assert session.scalar(select(func.count()).select_from(SignalOutcome)) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    assert outcome.predicted_direction == "bullish"
    assert outcome.actual_direction == "bullish"
    assert outcome.correct is True
    assert outcome.evaluation_version == EVALUATION_VERSION
    assert ("US", "AAPL") in provider.calls
    assert ("US", "benchmark") in provider.calls


def test_evaluator_does_not_write_without_market_benchmark(session: Session) -> None:
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FakeProvider(with_benchmark=False))
    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0
    assert session.scalar(select(func.count()).select_from(SignalOutcome)) == 0


def test_evaluator_does_not_write_non_finite_prices(session: Session) -> None:
    class NonFiniteProvider(FakeProvider):
        def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
            result = frame(100, 3)
            result.loc[result.index[4], "Close"] = float("nan")
            return result

    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=NonFiniteProvider())

    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0
    assert session.scalar(select(func.count()).select_from(SignalOutcome)) == 0


def test_evaluator_batches_past_an_ineligible_snapshot(session: Session) -> None:
    class SelectiveProvider(FakeProvider):
        def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
            self.calls.append((market, symbol))
            return pd.DataFrame() if symbol == "AAPL" else frame(100, 3)

    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC), "AAPL")
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC), "MSFT")
    evaluator = OutcomeEvaluator(provider=SelectiveProvider(), batch_size=1)

    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    assert outcome.snapshot.security.symbol == "MSFT"


class FrameProvider(FakeProvider):
    def __init__(self, prices: pd.DataFrame, benchmark: pd.DataFrame):
        super().__init__()
        self.prices = prices if prices.attrs else verified(prices)
        self.benchmark = benchmark if benchmark.attrs else verified(benchmark)

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        return self.prices

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        return self.benchmark


def test_recent_history_cannot_be_used_as_last_year_entry(session: Session) -> None:
    add_snapshot(session, datetime(2025, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FakeProvider())

    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0
    assert session.scalar(select(func.count(SignalOutcome.id))) == 0


def test_first_tradable_date_does_not_skip_missing_entry_history() -> None:
    assert first_tradable_date(
        datetime(2025, 1, 4, 12, tzinfo=UTC), [datetime(2026, 1, 5).date()],
    ) is None


@pytest.mark.parametrize("target", ["security", "benchmark"])
@pytest.mark.parametrize("position", [0, 2])
def test_missing_expected_session_keeps_outcome_pending(
    session: Session, target: str, position: int,
) -> None:
    prices, benchmark = frame(100, 3), frame(100, 0)
    if target == "security":
        prices = prices.drop(prices.index[position])
    else:
        benchmark = benchmark.drop(benchmark.index[position])
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FrameProvider(prices, benchmark))

    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0


def test_observed_zero_volume_session_is_skipped_as_suspension(session: Session) -> None:
    prices, benchmark = frame(100, 3), frame(100, 0)
    prices["Volume"] = 1000
    prices.loc[prices.index[0], "Volume"] = 0
    prices.loc[prices.index[0], "Open"] = float("nan")
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FrameProvider(prices, benchmark))

    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    assert ensure_aware(outcome.baseline_at) == datetime(2026, 1, 6, 14, 30, tzinfo=UTC)
    assert ensure_aware(outcome.observed_at) == datetime(2026, 1, 12, 21, tzinfo=UTC)


def test_holiday_and_half_day_follow_exchange_open_and_close(session: Session) -> None:
    prices = pd.DataFrame(
        {"Open": [999, 100], "Close": [999, 103]},
        index=pd.to_datetime(["2026-11-26", "2026-11-27"]),
    )
    benchmark = prices.copy()
    benchmark["Close"] = benchmark["Open"]
    snapshot = add_snapshot(session, datetime(2026, 11, 25, 20, tzinfo=UTC))
    snapshot.horizon = 1
    session.commit()
    evaluator = OutcomeEvaluator(provider=FrameProvider(prices, benchmark))

    assert evaluator.evaluate(session, now=datetime(2026, 11, 27, 17, 59, tzinfo=UTC)) == 0
    assert evaluator.evaluate(session, now=datetime(2026, 11, 27, 18, tzinfo=UTC)) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    assert outcome.entry_price == 100
    assert ensure_aware(outcome.baseline_at) == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert ensure_aware(outcome.observed_at) == datetime(2026, 11, 27, 18, tzinfo=UTC)


@pytest.mark.parametrize("market,timezone,close_hour", [
    ("A", "Asia/Shanghai", 7), ("HK", "Asia/Hong_Kong", 8),
])
def test_asian_sessions_preserve_local_dates_and_close_time(
    session: Session, market: str, timezone: str, close_hour: int,
) -> None:
    snapshot = add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    snapshot.security.market = market
    snapshot.security.timezone = timezone
    snapshot.security.currency = {"A": "CNY", "HK": "HKD"}[market]
    snapshot.horizon = 1
    session.commit()
    prices, benchmark = frame(100, 3), frame(100, 0)
    prices.attrs["currency"] = benchmark.attrs["currency"] = snapshot.security.currency
    prices.index = prices.index.tz_localize(timezone)
    benchmark.index = benchmark.index.tz_localize(timezone)
    evaluator = OutcomeEvaluator(provider=FrameProvider(prices, benchmark))

    assert evaluator.evaluate(session, now=datetime(2026, 1, 5, close_hour, tzinfo=UTC)) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    assert ensure_aware(outcome.baseline_at) == datetime(2026, 1, 5, 1, 30, tzinfo=UTC)
    assert ensure_aware(outcome.observed_at) == datetime(2026, 1, 5, close_hour, tzinfo=UTC)


def test_existing_legacy_outcome_remains_unchanged(session: Session) -> None:
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FakeProvider())
    now = datetime(2026, 3, 1, tzinfo=UTC)
    assert evaluator.evaluate(session, now=now) == 1
    outcome = session.scalar(select(SignalOutcome))
    assert outcome is not None
    outcome.evaluation_version = "legacy-v1"
    outcome.entry_price = 999
    session.commit()

    assert evaluator.evaluate(session, now=now) == 0
    session.refresh(outcome)
    assert outcome.evaluation_version == "legacy-v1"
    assert outcome.entry_price == 999


def test_a_share_adjusted_stock_and_price_index_remain_diagnostic_gap(
    session: Session, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("trade_news_analysis.services.evaluation"),
                        "disabled", False)
    snapshot = add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    snapshot.security.market = "A"
    snapshot.security.timezone = "Asia/Shanghai"
    snapshot.security.currency = "CNY"
    snapshot.horizon = 1
    session.commit()
    prices, benchmark = frame(100, 3), frame(100, 0)
    prices.attrs["currency"] = benchmark.attrs["currency"] = "CNY"
    benchmark.attrs["analysis_price_basis"] = "index_price_return"
    evaluator = OutcomeEvaluator(provider=FrameProvider(prices, benchmark))
    with caplog.at_level("INFO", logger="trade_news_analysis.services.evaluation"):
        assert evaluator.evaluate(session, now=datetime(2026, 1, 5, 7, tzinfo=UTC)) == 0
    assert evaluator.last_pending_by_reason == {"price_basis_mismatch": 1}
    assert "price_basis_mismatch" in caplog.text
    assert session.scalar(select(func.count()).select_from(SignalOutcome)) == 0
