"""Closed daily candles with short database transactions and repairable history."""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..daily_bar_models import DailyBar, DailyBarSyncState
from ..db import SessionFactory
from ..holding_models import HoldingPosition
from ..models import Security, Watchlist
from .holdings import latest_snapshot
from .market_research import calendar_for_market, number, utc
from .providers import YahooMarketDataProvider

SOURCE = "yfinance"
FULL_REFRESH_INTERVAL = timedelta(days=7)
_locks: dict[tuple[str, int], Any] = {}
_locks_guard = threading.Lock()


class DailyHistoryProvider(Protocol):
    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame: ...


class DailyBarValidationError(ValueError):
    """Safe, local validation diagnostics that contain no provider credentials."""


def target_trade_date(market: str, now: datetime | None = None) -> date:
    """The latest session whose actual close was at least one hour ago."""
    if market not in {"US", "HK"}:
        raise ValueError("日 K 线暂仅支持美股和港股")
    current = utc(now or datetime.now(UTC))
    calendar = calendar_for_market(market)
    today = pd.Timestamp(current.astimezone(calendar.tz).date())
    if today < calendar.first_session or today > calendar.last_session:
        raise DailyBarValidationError("交易所日历超出覆盖范围")
    schedule = calendar.schedule.loc[today - pd.Timedelta(days=40):today]
    completed = schedule[schedule["close"] + pd.Timedelta(hours=1) <= pd.Timestamp(current)]
    if completed.empty:
        raise DailyBarValidationError("未找到已收盘的交易日")
    return completed.index[-1].date()


def _cooldown(session: Session) -> datetime | None:
    value = session.scalar(select(func.max(DailyBarSyncState.next_retry_at)).where(
        DailyBarSyncState.error_kind == "rate_limited"
    ))
    return utc(value) if value else None


def refresh_due(session: Session, security: Security, now: datetime | None = None) -> bool:
    if not security.active or security.market not in {"US", "HK"}:
        return False
    current = utc(now or datetime.now(UTC))
    cooling = _cooldown(session)
    if cooling and cooling > current:
        return False
    state = session.get(DailyBarSyncState, security.id)
    if state is None:
        return True
    if state.next_retry_at and utc(state.next_retry_at) > current:
        return False
    if state.status in {"pending", "failed", "partial", "running"}:
        return True
    target = target_trade_date(security.market, current)
    return bool(
        state.checked_through is None or state.checked_through < target
        or state.last_full_refresh_at is None
        or current - utc(state.last_full_refresh_at) >= FULL_REFRESH_INTERVAL
    )


def get_daily_bars(
    session: Session, security: Security, start: date | None = None,
    end: date | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Read local bars only; requested end dates are inclusive."""
    if start and end and start > end:
        raise ValueError("起始日期不能晚于结束日期")
    query = select(DailyBar).where(
        DailyBar.security_id == security.id, DailyBar.source == SOURCE,
    )
    if start:
        query = query.where(DailyBar.trade_date >= start)
    if end:
        query = query.where(DailyBar.trade_date <= end)
    rows = session.scalars(query.order_by(DailyBar.trade_date)).all()
    state = session.get(DailyBarSyncState, security.id)
    bars = []
    for row in rows:
        factor = row.adj_close / row.close if row.adj_close else None
        bars.append({
            "date": row.trade_date.isoformat(), "open": row.open, "high": row.high,
            "low": row.low, "close": row.close, "volume": row.volume, "amount": row.amount,
            "adj_open": row.open * factor if factor else None,
            "adj_high": row.high * factor if factor else None,
            "adj_low": row.low * factor if factor else None, "adj_close": row.adj_close,
        })
    current = utc(now or datetime.now(UTC))
    target = target_trade_date(security.market, current)
    cooling = _cooldown(session)
    retry = utc(state.next_retry_at) if state and state.next_retry_at else None
    retry = max(item for item in (retry, cooling) if item) if retry or cooling else None
    return {
        "security_id": security.id, "market": security.market, "symbol": security.symbol,
        "source": SOURCE, "currency": (state.currency if state else None) or security.currency,
        "source_version": state.source_version if state else None,
        "price_basis": "split_adjusted",
        "analysis_price_basis": "total_return_adjusted" if rows and all(
            row.adj_close for row in rows
        ) else "unknown",
        "timezone": (state.timezone if state else None) or security.timezone,
        "volume_unit": "shares",
        "available_adjustments": ["split_adjusted"] + (
            ["total_return_adjusted"] if rows and all(row.adj_close for row in rows) else []
        ),
        "bars": bars,
        "coverage": {
            "start": state.coverage_start if state else None,
            "end": state.coverage_end if state else None,
        },
        "latest_trade_date": state.coverage_end if state else None,
        "last_success_at": utc(state.last_success_at) if state and state.last_success_at else None,
        "last_attempt_at": utc(state.last_attempt_at) if state and state.last_attempt_at else None,
        "next_retry_at": retry,
        "sync_status": state.status if state else "pending",
        "error": state.error if state else None,
        "needs_refresh": refresh_due(session, security, current),
        "stale": bool(
            not state or not state.coverage_end or state.coverage_end < target
            or state.status == "failed"
        ),
    }


def _five_year_start(target: date) -> date:
    try:
        return target.replace(year=target.year - 5)
    except ValueError:
        return target.replace(year=target.year - 5, day=28)


def _overlap_start(market: str, latest: date) -> date:
    calendar = calendar_for_market(market)
    sessions = calendar.sessions_in_range(
        pd.Timestamp(latest - timedelta(days=40)), pd.Timestamp(latest)
    )
    return sessions[-min(10, len(sessions))].date()


def _nonnegative(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value is None or pd.isna(value):
        if optional:
            return None
        raise DailyBarValidationError(f"日线 {field} 缺失")
    parsed = number(value)
    if parsed is None or parsed < 0:
        raise DailyBarValidationError(f"日线 {field} 必须是有限非负数")
    return parsed


def _normalize(
    frame: pd.DataFrame, security: Security, start: date, target: date,
) -> list[dict[str, Any]]:
    if not isinstance(frame, pd.DataFrame):
        raise DailyBarValidationError("行情来源未返回日线表格")
    if frame.attrs.get("currency") != security.currency:
        raise DailyBarValidationError("行情币种与证券币种不匹配")
    timezone = frame.attrs.get("timezone")
    allowed = {"US": {"America/New_York", "US/Eastern"}, "HK": {"Asia/Hong_Kong"}}
    if timezone not in allowed[security.market] or frame.attrs.get("volume_unit") != "shares":
        raise DailyBarValidationError("行情时区或成交量单位未核实")
    if frame.attrs.get("price_basis") != "split_adjusted":
        raise DailyBarValidationError("行情基础价格口径未核实")
    zone = ZoneInfo(timezone)
    calendar = calendar_for_market(security.market)
    bars: dict[date, dict[str, Any]] = {}
    for stamp, row in frame.iterrows():
        stamp = pd.Timestamp(stamp)
        if pd.isna(stamp):
            raise DailyBarValidationError("日线交易日期无效")
        day = stamp.tz_convert(zone).date() if stamp.tzinfo else stamp.date()
        if day < start or day > target:
            continue
        if not calendar.is_session(pd.Timestamp(day)):
            continue
        values: dict[str, float] = {}
        for key in ("Open", "High", "Low", "Close"):
            value = number(row.get(key))
            if value is None or value <= 0:
                raise DailyBarValidationError("日线 OHLC 价格缺失、非正数或非有限数")
            values[key.lower()] = value
        if (
            values["low"] > min(values["open"], values["close"])
            or values["high"] < max(values["open"], values["close"])
        ):
            raise DailyBarValidationError("日线最高价、最低价与开收盘价矛盾")
        adjusted = row.get("Adj Close")
        adj_close = None if adjusted is None or pd.isna(adjusted) else number(adjusted)
        if adjusted is not None and not pd.isna(adjusted) and (
            adj_close is None or adj_close <= 0
        ):
            raise DailyBarValidationError("复权收盘价无效")
        item = {
            "trade_date": day, **values, "adj_close": adj_close,
            "volume": _nonnegative(row.get("Volume"), "成交量", optional=True),
            "amount": _nonnegative(row.get("Amount"), "成交额", optional=True),
            "dividends": _nonnegative(row.get("Dividends", 0), "分红"),
            "stock_splits": _nonnegative(row.get("Stock Splits", 0), "拆股比例"),
        }
        if day in bars and bars[day] != item:
            raise DailyBarValidationError("同一交易日返回矛盾的重复行情")
        bars[day] = item
    return [bars[day] for day in sorted(bars)]


def _different(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left != right
    return not math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-8)


def _requires_full_history(existing: Sequence[DailyBar], bars: list[dict[str, Any]]) -> bool:
    by_date = {row.trade_date: row for row in existing}
    for bar in bars:
        old = by_date.get(bar["trade_date"])
        if old is None:
            if bar["dividends"] or bar["stock_splits"]:
                return True
            continue
        if any(_different(getattr(old, field), bar[field]) for field in (
            "open", "high", "low", "close", "dividends", "stock_splits",
        )):
            return True
        old_factor = old.adj_close / old.close if old.adj_close else None
        factor = bar["adj_close"] / bar["close"] if bar["adj_close"] else None
        if _different(old_factor, factor):
            return True
    return False


class DailyBarService:
    def __init__(self, settings: Settings, provider: DailyHistoryProvider | None = None):
        self.settings = settings
        self.provider = provider or YahooMarketDataProvider()

    get_daily_bars = staticmethod(get_daily_bars)
    refresh_due = staticmethod(refresh_due)

    @staticmethod
    def _security_ids(session: Session, explicit: Sequence[int] | None) -> list[int]:
        query = select(Security.id).where(
            Security.active.is_(True), Security.market.in_(("US", "HK"))
        )
        if explicit is not None:
            return list(session.scalars(
                query.where(Security.id.in_(set(explicit))).order_by(Security.id)
            ))
        selected = set(session.scalars(
            select(Watchlist.security_id).where(Watchlist.active.is_(True))
        ))
        snapshot = latest_snapshot(session)
        if snapshot:
            selected.update(session.scalars(select(HoldingPosition.security_id).where(
                HoldingPosition.snapshot_id == snapshot.id, HoldingPosition.quantity != 0,
                HoldingPosition.security_id.is_not(None),
                HoldingPosition.unsupported_reason.is_(None),
            )))
        return list(session.scalars(query.where(Security.id.in_(selected)).order_by(Security.id)))

    def refresh_isolated(
        self, session_factory: SessionFactory, security_ids: Sequence[int] | None = None,
        *, now: datetime | None = None, force: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"updated": 0, "skipped": 0, "failed": 0, "errors": []}
        with session_factory() as session:
            ids = self._security_ids(session, security_ids)
        for security_id in ids:
            with _locks_guard:
                lock = _locks.setdefault(
                    (self.settings.database_url, security_id), threading.Lock()
                )
            if not lock.acquire(blocking=False):
                result["skipped"] += 1
                continue
            try:
                status, error = self._refresh_one(
                    session_factory, security_id, utc(now or datetime.now(UTC)), force
                )
                result[status] += 1
                if error:
                    result["errors"].append(f"证券 {security_id}: {error}")
            finally:
                lock.release()
        return result

    def _refresh_one(
        self, factory: SessionFactory, security_id: int, now: datetime, force: bool,
    ) -> tuple[str, str | None]:
        try:
            with factory() as session:
                security = session.get(Security, security_id)
                if security is None:
                    return "skipped", None
                cooling = _cooldown(session)
                if cooling and cooling > now:
                    return "skipped", None
                state = session.get(DailyBarSyncState, security_id)
                # Manual refresh never bypasses provider cooldown or failure backoff.
                if state and state.next_retry_at and utc(state.next_retry_at) > now:
                    return "skipped", None
                if not force and not refresh_due(session, security, now):
                    return "skipped", None
                target = target_trade_date(security.market, now)
                existing = list(session.scalars(select(DailyBar).where(
                    DailyBar.security_id == security_id, DailyBar.source == SOURCE,
                ).order_by(DailyBar.trade_date)))
                full_start = (
                    min(_five_year_start(target), existing[0].trade_date)
                    if existing else _five_year_start(target)
                )
                full = bool(
                    not existing or not state or not state.last_full_refresh_at
                    or now - utc(state.last_full_refresh_at) >= FULL_REFRESH_INTERVAL
                )
                start = (
                    full_start if full else _overlap_start(security.market, existing[-1].trade_date)
                )
                if state is None:
                    state = DailyBarSyncState(security_id=security_id)
                    session.add(state)
                state.status, state.last_attempt_at = "running", now
                session.commit()
                session.expunge_all()

            # No SQLite transaction or pooled connection spans either network request.
            frame = self.provider.history_range(
                security.market, security.symbol, start, target + timedelta(days=1),
                provider_data=security.provider_data,
            )
            bars = _normalize(frame, security, start, target)
            if not full and _requires_full_history(existing, bars):
                full = True
                frame = self.provider.history_range(
                    security.market, security.symbol, full_start, target + timedelta(days=1),
                    provider_data=security.provider_data,
                )
                bars = _normalize(frame, security, full_start, target)
            if full:
                if not bars:
                    raise DailyBarValidationError("未返回可用的已收盘历史日线")
                if not {row.trade_date for row in existing}.issubset(
                    {bar["trade_date"] for bar in bars}
                ):
                    raise DailyBarValidationError("完整历史响应缺少已保存的交易日，保留原行情")
            elif not {row.trade_date for row in existing if row.trade_date >= start}.issubset(
                {bar["trade_date"] for bar in bars}
            ):
                raise DailyBarValidationError("增量响应缺少重叠交易日，保留原行情")

            with factory() as session:
                from .history_repository import save_history_revision

                state = session.get(DailyBarSyncState, security_id)
                assert state is not None
                save_history_revision(session, security, bars, dict(frame.attrs), now)
                session.flush()
                state.coverage_start, state.coverage_end = session.execute(select(
                    func.min(DailyBar.trade_date), func.max(DailyBar.trade_date),
                ).where(DailyBar.security_id == security_id, DailyBar.source == SOURCE)).one()
                state.currency = frame.attrs["currency"]
                state.timezone = frame.attrs["timezone"]
                state.checked_through, state.last_success_at = target, now
                if full:
                    state.last_full_refresh_at = now
                if state.coverage_end is None or state.coverage_end < target:
                    state.failure_count += 1
                    state.status, state.error_kind = "partial", "incomplete"
                    state.error = "最新交易日尚无行情，可能停牌或来源延迟，将自动复核"
                    state.next_retry_at = now + timedelta(
                        hours=1 if state.failure_count == 1 else 6
                    )
                else:
                    state.status, state.failure_count = "success", 0
                    state.error, state.error_kind, state.next_retry_at = None, None, None
                session.commit()
            return "updated", None
        except Exception as exc:
            rate_limited = any(marker in f"{type(exc).__name__} {exc}".casefold() for marker in (
                "ratelimit", "rate limit", "too many requests", "429",
            ))
            error = (
                "Yahoo 请求限流，稍后自动重试" if rate_limited else str(exc)
                if isinstance(exc, DailyBarValidationError)
                else f"日线同步失败（{type(exc).__name__}），稍后自动重试"
            )
            with factory() as session:
                state = session.get(DailyBarSyncState, security_id)
                if state is None:
                    state = DailyBarSyncState(security_id=security_id, failure_count=0)
                    session.add(state)
                state.failure_count += 1
                delay = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))[
                    min(state.failure_count - 1, 2)
                ]
                state.status, state.error = "failed", error
                state.error_kind = "rate_limited" if rate_limited else "fetch_failed"
                state.last_attempt_at, state.next_retry_at = now, now + delay
                session.commit()
            return "failed", error
