from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security, SecuritySignalSnapshot, SignalOutcome
from trade_news_analysis.services.evaluation import OutcomeEvaluator, first_tradable_date


def frame(open_start: float, close_step: float) -> pd.DataFrame:
    index = pd.bdate_range("2026-01-05", periods=30)
    opens = [open_start + index for index in range(30)]
    closes = [value + close_step for value in opens]
    return pd.DataFrame({"Open": opens, "Close": closes}, index=index)


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


def add_snapshot(session: Session, as_of: datetime) -> SecuritySignalSnapshot:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
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
    assert ("US", "AAPL") in provider.calls
    assert ("US", "benchmark") in provider.calls


def test_evaluator_does_not_write_without_market_benchmark(session: Session) -> None:
    add_snapshot(session, datetime(2026, 1, 4, 12, tzinfo=UTC))
    evaluator = OutcomeEvaluator(provider=FakeProvider(with_benchmark=False))
    assert evaluator.evaluate(session, now=datetime(2026, 3, 1, tzinfo=UTC)) == 0
    assert session.scalar(select(func.count()).select_from(SignalOutcome)) == 0
