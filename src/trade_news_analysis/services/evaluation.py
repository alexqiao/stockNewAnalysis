"""Leakage-resistant multi-market validation of persisted security signals."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import SecuritySignalSnapshot, SignalOutcome
from ..risk_models import SignalEvaluationAudit
from .history_repository import HistoryRepository
from .market_research import calendar_for_market, number
from .normalization import ensure_aware
from .providers import MarketDataProvider, build_market_data_provider
from .validation_history import read_validation_history

DIRECTION_BAND_PCT = 0.5
EVALUATION_VERSION = "strict-sessions-v3"
logger = logging.getLogger(__name__)


def _normalized_frame(frame: Any, timezone: str = "America/New_York") -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    result = frame.copy()
    result.index = pd.to_datetime(result.index)
    if result.index.tz is not None:
        result.index = result.index.tz_convert(timezone).tz_localize(None)
    result.index = result.index.normalize()
    if result.index.has_duplicates or result.index.isna().any():
        return pd.DataFrame()
    return result.sort_index()


def _future_sessions(created_at: datetime, market: str) -> pd.DataFrame:
    calendar = calendar_for_market(market)
    observed = pd.Timestamp(ensure_aware(created_at))
    day = pd.Timestamp(observed.tz_convert(calendar.tz).date())
    if day < calendar.first_session or day > calendar.last_session:
        return pd.DataFrame()
    schedule = calendar.schedule
    return schedule[schedule["open"] > observed]


def first_tradable_date(
    created_at: datetime,
    trading_dates: list[date],
    timezone: str = "America/New_York",
) -> date | None:
    market = {
        "America/New_York": "US", "Asia/Shanghai": "A", "Asia/Hong_Kong": "HK",
    }.get(timezone)
    if market is None:
        return None
    schedule = _future_sessions(created_at, market)
    if schedule.empty:
        return None
    expected = schedule.index[0].date()
    return expected if expected in trading_dates else None


def _validation_window(
    snapshot: SecuritySignalSnapshot,
    prices: pd.DataFrame,
    benchmark: pd.DataFrame,
    now: datetime,
) -> tuple[list[pd.Timestamp], datetime, datetime] | None:
    try:
        schedule = _future_sessions(snapshot.as_of, snapshot.security.market)
    except (ValueError, KeyError):
        return None
    if schedule.empty or snapshot.horizon < 1:
        return None
    closed = schedule[schedule["close"] <= pd.Timestamp(now)]
    window: list[pd.Timestamp] = []
    for stamp in closed.index:
        if stamp not in prices.index:
            # A missing bar is unknown history, never evidence of a suspension.
            return None
        row = prices.loc[stamp]
        if "Volume" in prices:
            volume = number(row["Volume"])
            if volume is None or volume < 0:
                return None
            if volume == 0:
                continue
        if stamp not in benchmark.index:
            return None
        values = [number(table.loc[stamp, field])
                  for table in (prices, benchmark) for field in ("Open", "Close")]
        if any(value is None or value <= 0 for value in values):
            return None
        window.append(stamp)
        if len(window) == snapshot.horizon:
            return (
                window,
                schedule.loc[window[0], "open"].to_pydatetime(),
                schedule.loc[window[-1], "close"].to_pydatetime(),
            )
    return None


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
        batch_size: int = 500,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.provider = provider or build_market_data_provider(settings or Settings())
        self.batch_size = min(batch_size, 500)
        self.last_pending_by_reason: dict[str, int] = {}

    def evaluate(self, session: Session, now: datetime | None = None) -> int:
        current = ensure_aware(now or datetime.now(UTC))
        created = 0
        pending: Counter[str] = Counter()
        self.last_pending_by_reason = {}
        last_snapshot_id = 0
        upper_snapshot_id = session.scalar(select(func.max(SecuritySignalSnapshot.id))) or 0
        while last_snapshot_id < upper_snapshot_id:
            snapshots = session.scalars(
                select(SecuritySignalSnapshot)
                .outerjoin(SignalOutcome)
                .where(
                    SignalOutcome.id.is_(None),
                    SecuritySignalSnapshot.id > last_snapshot_id,
                    SecuritySignalSnapshot.id <= upper_snapshot_id,
                )
                .order_by(SecuritySignalSnapshot.id)
                .limit(self.batch_size)
                .options(selectinload(SecuritySignalSnapshot.security))
            ).all()
            if not snapshots:
                break
            last_snapshot_id = snapshots[-1].id
            repository = HistoryRepository(session, self.provider, allow_fetch=True)
            results = []
            audits = []
            for snapshot in snapshots:
                security = snapshot.security
                try:
                    schedule = _future_sessions(snapshot.as_of, security.market)
                    closed = schedule[schedule["close"] <= pd.Timestamp(current)]
                    if len(closed) < snapshot.horizon:
                        pending["not_mature"] += 1
                        continue
                    frame, end, limit_reason = read_validation_history(
                        repository, security, closed, snapshot.horizon,
                    )
                    if limit_reason:
                        pending[limit_reason] += 1
                        continue
                    start = schedule.index[0].date()
                    prices = _normalized_frame(frame, security.timezone)
                    benchmark_security = repository.security(security.market, {
                        "A": "000300.SH", "HK": "^HSI", "US": "SPY",
                    }[security.market])
                    benchmark = _normalized_frame(repository.history(
                        benchmark_security, start, end, benchmark=True,
                    ), security.timezone)
                except Exception:
                    pending["source_failed"] += 1
                    continue
                if (
                    prices.empty
                    or benchmark.empty
                    or "Open" not in prices
                    or "Close" not in prices
                    or "Open" not in benchmark
                    or "Close" not in benchmark
                ):
                    pending["history_missing"] += 1
                    continue
                if any(table.attrs.get("currency") != security.currency
                       for table in (prices, benchmark)):
                    pending["currency_mismatch"] += 1
                    continue
                if any(table.attrs.get("adjustment_status") != "verified"
                       for table in (prices, benchmark)):
                    pending["adjustment_unverified"] += 1
                    continue
                if prices.attrs.get("analysis_price_basis") != benchmark.attrs.get(
                    "analysis_price_basis"
                ):
                    pending["price_basis_mismatch"] += 1
                    continue
                # New outcomes consistently use verified adjusted prices; old results stay intact.
                if any(f"Adj {field}" not in table
                       for table in (prices, benchmark) for field in ("Open", "Close")):
                    pending["adjustment_unverified"] += 1
                    continue
                if _validation_window(snapshot, prices, benchmark, current) is None:
                    required = closed.index[closed.index.date <= end]
                    if any(stamp not in prices.index or stamp not in benchmark.index
                           for stamp in required):
                        pending["history_missing"] += 1
                    elif "Volume" in prices and any(
                        number(value) is None or float(value) < 0 for value in prices["Volume"]
                    ):
                        pending["volume_unknown"] += 1
                    elif "Volume" in prices and sum(prices["Volume"] > 0) < snapshot.horizon:
                        pending["not_mature"] += 1
                    else:
                        pending["price_invalid"] += 1
                    continue
                original_prices, original_benchmark = prices.copy(), benchmark.copy()
                for table in (prices, benchmark):
                    for field in ("Open", "Close"):
                        table[field] = table[f"Adj {field}"]
                window = _validation_window(snapshot, prices, benchmark, current)
                if window is None:
                    pending["price_invalid"] += 1
                    continue
                stamps, baseline_at, observed_at = window
                entry_stamp, exit_stamp = stamps[0], stamps[-1]
                entry_price = float(prices.loc[entry_stamp]["Open"])
                exit_price = float(prices.loc[exit_stamp]["Close"])
                benchmark_entry = float(benchmark.loc[entry_stamp]["Open"])
                benchmark_exit = float(benchmark.loc[exit_stamp]["Close"])
                stock_return = (exit_price / entry_price - 1) * 100
                benchmark_return = (benchmark_exit / benchmark_entry - 1) * 100
                excess = stock_return - benchmark_return
                predicted = snapshot.direction
                limit_up_hit = None
                if security.market == "A" and "UpLimit" in prices and "High" in prices:
                    observed = prices.loc[stamps]
                    limit_up_hit = bool((observed["High"] >= observed["UpLimit"]).any())
                results.append(
                    SignalOutcome(
                        snapshot_id=snapshot.id,
                        baseline_at=baseline_at,
                        observed_at=observed_at,
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
                        evaluation_version=EVALUATION_VERSION,
                    )
                )
                audits.append(SignalEvaluationAudit(
                    snapshot_id=snapshot.id, evaluation_version=EVALUATION_VERSION,
                    payload={
                        "window_dates": [stamp.date().isoformat() for stamp in stamps],
                        "stock_metadata": dict(original_prices.attrs),
                        "benchmark_metadata": dict(original_benchmark.attrs),
                        "stock_window": json.loads(original_prices.loc[stamps].to_json(
                            orient="split", date_format="iso",
                        )),
                        "benchmark_window": json.loads(original_benchmark.loc[stamps].to_json(
                            orient="split", date_format="iso",
                        )),
                    },
                ))
                created += 1
            versions = repository.persist(session, current)
            for audit in audits:
                version = versions.get(audit.payload["stock_metadata"].get("source_version"))
                if version:
                    audit.payload["stock_metadata"]["source_version"] = version
                version = versions.get(audit.payload["benchmark_metadata"].get("source_version"))
                if version:
                    audit.payload["benchmark_metadata"]["source_version"] = version
            session.add_all([*results, *audits])
            session.commit()
        self.last_pending_by_reason = dict(sorted(pending.items()))
        if pending:
            # Aggregate reason codes only: provider exception text may include credentials.
            logger.info("signal_validation_pending reasons=%s", self.last_pending_by_reason)
        return created
