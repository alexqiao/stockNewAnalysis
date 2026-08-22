"""Leakage-resistant forward validation against SPY."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from investormate import Stock
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ImpactAnalysis, Outcome
from .normalization import ensure_aware

HORIZONS = (1, 5, 20)
DIRECTION_BAND_PCT = 0.5


def default_price_loader(symbol: str) -> pd.DataFrame:
    result = Stock(symbol).history(period="6mo", interval="1d", adjusted=False)
    return result.data if hasattr(result, "data") else result


def _normalized_frame(frame: Any) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index)
    if result.index.tz is not None:
        result.index = result.index.tz_convert("America/New_York").tz_localize(None)
    result.index = result.index.normalize()
    return result.sort_index()


def first_tradable_date(created_at: datetime, trading_dates: list[date]) -> date | None:
    local = ensure_aware(created_at).astimezone(ZoneInfo("America/New_York"))
    candidate = local.date() if local.time() < time(9, 30) else local.date() + timedelta(days=1)
    return next((day for day in trading_dates if day >= candidate), None)


def actual_direction(excess_return_pct: float) -> str:
    if excess_return_pct > DIRECTION_BAND_PCT:
        return "bullish"
    if excess_return_pct < -DIRECTION_BAND_PCT:
        return "bearish"
    return "neutral"


class OutcomeEvaluator:
    def __init__(self, price_loader: Callable[[str], pd.DataFrame] = default_price_loader):
        self.price_loader = price_loader

    def evaluate(self, session: Session, now: datetime | None = None) -> int:
        current = ensure_aware(now or datetime.now(UTC))
        analyses = session.scalars(
            select(ImpactAnalysis).where(
                ImpactAnalysis.status == "complete", ImpactAnalysis.is_current.is_(True)
            )
        ).all()
        try:
            benchmark = _normalized_frame(self.price_loader("SPY"))
        except Exception:
            return 0
        if benchmark.empty:
            return 0
        created = 0
        price_cache: dict[str, pd.DataFrame] = {}
        for analysis in analyses:
            relation = analysis.article_symbol
            symbol = relation.symbol
            if symbol not in price_cache:
                try:
                    price_cache[symbol] = _normalized_frame(self.price_loader(symbol))
                except Exception:
                    price_cache[symbol] = pd.DataFrame()
            prices = price_cache[symbol]
            if prices.empty or "Open" not in prices or "Close" not in prices:
                continue
            dates = [stamp.date() for stamp in prices.index]
            entry_date = first_tradable_date(analysis.created_at, dates)
            if entry_date is None:
                continue
            entry_position = dates.index(entry_date)
            entry_stamp = prices.index[entry_position]
            if entry_stamp not in benchmark.index:
                continue
            for horizon in HORIZONS:
                if session.scalar(
                    select(Outcome.id).where(
                        Outcome.analysis_id == analysis.id, Outcome.horizon == horizon
                    )
                ):
                    continue
                exit_position = entry_position + horizon - 1
                if exit_position >= len(prices):
                    continue
                exit_stamp = prices.index[exit_position]
                exit_local = datetime.combine(
                    exit_stamp.date(), time(16, 15), ZoneInfo("America/New_York")
                )
                if current < exit_local.astimezone(UTC) or exit_stamp not in benchmark.index:
                    continue
                entry_price = float(prices.iloc[entry_position]["Open"])
                exit_price = float(prices.iloc[exit_position]["Close"])
                benchmark_entry = float(benchmark.loc[entry_stamp]["Open"])
                benchmark_exit = float(benchmark.loc[exit_stamp]["Close"])
                if min(entry_price, exit_price, benchmark_entry, benchmark_exit) <= 0:
                    continue
                stock_return = (exit_price / entry_price - 1) * 100
                benchmark_return = (benchmark_exit / benchmark_entry - 1) * 100
                excess = stock_return - benchmark_return
                predicted = str(analysis.impacts[str(horizon)]["direction"])
                actual = actual_direction(excess)
                session.add(
                    Outcome(
                        analysis_id=analysis.id,
                        horizon=horizon,
                        baseline_at=entry_stamp.to_pydatetime().replace(tzinfo=UTC),
                        observed_at=exit_stamp.to_pydatetime().replace(tzinfo=UTC),
                        entry_price=entry_price,
                        exit_price=exit_price,
                        benchmark_entry=benchmark_entry,
                        benchmark_exit=benchmark_exit,
                        return_pct=stock_return,
                        benchmark_return_pct=benchmark_return,
                        excess_return_pct=excess,
                        predicted_direction=predicted,
                        actual_direction=actual,
                        correct=predicted == actual,
                    )
                )
                created += 1
        session.commit()
        return created
