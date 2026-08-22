"""Leakage-resistant multi-market validation of persisted security signals."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import SecuritySignalSnapshot, SignalOutcome
from .normalization import ensure_aware
from .providers import MarketDataProvider, build_market_data_provider

DIRECTION_BAND_PCT = 0.5


def _normalized_frame(frame: Any) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index)
    if result.index.tz is not None:
        result.index = result.index.tz_convert("UTC").tz_localize(None)
    result.index = result.index.normalize()
    return result.sort_index()


def first_tradable_date(
    created_at: datetime,
    trading_dates: list[date],
    timezone: str = "America/New_York",
) -> date | None:
    local = ensure_aware(created_at).astimezone(ZoneInfo(timezone))
    candidate = local.date() if local.time() < time(9, 30) else local.date() + timedelta(days=1)
    return next((day for day in trading_dates if day >= candidate), None)


def actual_direction(excess_return_pct: float) -> str:
    if excess_return_pct > DIRECTION_BAND_PCT:
        return "bullish"
    if excess_return_pct < -DIRECTION_BAND_PCT:
        return "bearish"
    return "neutral"


class OutcomeEvaluator:
    def __init__(
        self,
        settings: Settings | None = None,
        provider: MarketDataProvider | None = None,
    ):
        self.provider = provider or build_market_data_provider(settings or Settings())

    def evaluate(self, session: Session, now: datetime | None = None) -> int:
        current = ensure_aware(now or datetime.now(UTC))
        snapshots = session.scalars(
            select(SecuritySignalSnapshot)
            .outerjoin(SignalOutcome)
            .where(SignalOutcome.id.is_(None))
            .options(selectinload(SecuritySignalSnapshot.security))
        ).all()
        created = 0
        price_cache: dict[tuple[str, str], pd.DataFrame] = {}
        benchmark_cache: dict[str, pd.DataFrame] = {}
        for snapshot in snapshots:
            security = snapshot.security
            key = (security.market, security.symbol)
            if key not in price_cache:
                try:
                    price_cache[key] = _normalized_frame(
                        self.provider.history(security.market, security.symbol)
                    )
                except Exception:
                    price_cache[key] = pd.DataFrame()
            if security.market not in benchmark_cache:
                try:
                    benchmark_cache[security.market] = _normalized_frame(
                        self.provider.benchmark_history(security.market)
                    )
                except Exception:
                    benchmark_cache[security.market] = pd.DataFrame()
            prices = price_cache[key]
            benchmark = benchmark_cache[security.market]
            if (
                prices.empty
                or benchmark.empty
                or "Open" not in prices
                or "Close" not in prices
            ):
                continue
            dates = [stamp.date() for stamp in prices.index]
            entry_date = first_tradable_date(snapshot.as_of, dates, security.timezone)
            if entry_date is None:
                continue
            entry_position = dates.index(entry_date)
            exit_position = entry_position + snapshot.horizon - 1
            if exit_position >= len(prices):
                continue
            entry_stamp = prices.index[entry_position]
            exit_stamp = prices.index[exit_position]
            if entry_stamp not in benchmark.index or exit_stamp not in benchmark.index:
                continue
            exit_local = datetime.combine(
                exit_stamp.date(), time(16, 15), ZoneInfo(security.timezone)
            )
            if current < exit_local.astimezone(UTC):
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
            predicted = snapshot.direction
            limit_up_hit = None
            if security.market == "A" and "UpLimit" in prices and "High" in prices:
                observed = prices.iloc[entry_position : exit_position + 1]
                limit_up_hit = bool((observed["High"] >= observed["UpLimit"]).any())
            session.add(
                SignalOutcome(
                    snapshot_id=snapshot.id,
                    baseline_at=datetime.combine(
                        entry_stamp.date(), time(9, 30), ZoneInfo(security.timezone)
                    ).astimezone(UTC),
                    observed_at=exit_local.astimezone(UTC),
                    entry_price=entry_price,
                    exit_price=exit_price,
                    benchmark_entry=benchmark_entry,
                    benchmark_exit=benchmark_exit,
                    return_pct=stock_return,
                    benchmark_return_pct=benchmark_return,
                    excess_return_pct=excess,
                    predicted_direction=predicted,
                    actual_direction=actual_direction(excess),
                    correct=predicted == actual_direction(excess),
                    limit_up_hit=limit_up_hit,
                )
            )
            created += 1
        session.commit()
        return created
