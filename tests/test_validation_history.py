from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.services.action_evaluation import evaluate_action_snapshots
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.history_repository import HistoryRepository
from trade_news_analysis.services.market_research import calendar_for_market
from trade_news_analysis.services.validation_history import read_validation_history

from .test_action_evaluation import MATURE, record
from .test_evaluation import add_snapshot
from .test_market_research import FixtureProvider, apple, market_frame


class RangeProvider(FixtureProvider):
    def __init__(self, frame: pd.DataFrame):
        super().__init__({})
        self.frame = frame
        self.ranges: list[tuple[str, date, date]] = []

    def history_range(
        self, market: str, symbol: str, start: date, end: date, provider_data: Any = None,
    ) -> pd.DataFrame:
        self.ranges.append((symbol, start, end))
        return self.frame

    def benchmark_history_range(self, market: str, start: date, end: date) -> pd.DataFrame:
        self.ranges.append(("benchmark", start, end))
        return self.frame


@pytest.mark.parametrize("kind", ["action", "signal"])
@pytest.mark.parametrize("suspended", [False, True])
def test_old_one_day_validation_only_reads_target_sessions(
    session: Session, settings: Settings, kind: str, suspended: bool,
) -> None:
    as_of = datetime(2024, 1, 1, 22, tzinfo=UTC)
    if kind == "action":
        record(session, horizon=1, now=as_of)
    else:
        snapshot = add_snapshot(session, as_of)
        snapshot.horizon = 1
        session.commit()
    prices = market_frame(start="2024-01-02", end="2024-02-15")
    if suspended:
        prices.loc["2024-01-02", "Volume"] = 0
    provider = RangeProvider(prices)
    if kind == "action":
        result = evaluate_action_snapshots(session, settings, now=MATURE, provider=provider)
        assert result["completed"] == 1
    else:
        assert OutcomeEvaluator(provider=provider).evaluate(session, now=MATURE) == 1
    assert provider.ranges[0] == ("AAPL", date(2024, 1, 2), date(2024, 1, 3))
    if suspended:
        assert len(provider.ranges) == 3
        assert provider.ranges[1][2] == date(2024, 2, 1)
        assert provider.ranges[2][2] == date(2024, 1, 4)
    else:
        assert len(provider.ranges) == 2
        assert provider.ranges[1][2] == date(2024, 1, 3)


@pytest.mark.parametrize("missing", [False, True])
def test_missing_data_does_not_extend_and_long_suspensions_are_bounded(
    session: Session, missing: bool,
) -> None:
    frame = market_frame(start="2024-01-02", end="2024-12-31")
    frame["Volume"] = 0
    if missing:
        frame = frame.drop(pd.Timestamp("2024-01-02"))
    provider = RangeProvider(frame)
    repository = HistoryRepository(session, provider, allow_fetch=True)
    schedule = calendar_for_market("US").schedule.loc["2024-01-02":"2024-12-31"]
    _, end, problem = read_validation_history(repository, apple(session), schedule, 1)
    if missing:
        assert len(provider.ranges) == 1
        assert problem is None
        assert end == date(2024, 1, 2)
    else:
        assert len(provider.ranges) == 6
        assert problem == "suspension_limit"
        assert end == schedule.index[100].date()
