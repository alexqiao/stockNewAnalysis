"""Date-bounded histories shared by charts, research and forward validation.

Reads never fetch by default. Background callers explicitly allow fetching and
flush staged cache updates only after their network work has finished.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..daily_bar_models import DailyBar, DailyBarRevision, DailyBarSyncState
from ..db import SessionFactory
from ..models import Security
from .market_research import calendar_for_market, utc
from .providers import MarketDataProvider

BAR_FIELDS = (
    "open", "high", "low", "close", "volume", "amount", "adj_close", "dividends", "stock_splits",
)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def bounded_frame(frame: pd.DataFrame, start: date, end: date, timezone: str) -> pd.DataFrame:
    result = frame.copy()
    if result.empty:
        return result
    index = pd.to_datetime(result.index)
    if index.tz is not None:
        index = index.tz_convert(timezone).tz_localize(None)
    result.index = index.normalize()
    return result.loc[(result.index.date >= start) & (result.index.date <= end)].sort_index()


def label_history(frame: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """Use values and stable metadata, never a fresh read timestamp, as the version."""
    frame = frame.copy()
    attrs = dict(frame.attrs)
    if not attrs.get("source_version"):
        attrs["source_version"] = digest({
            "metadata": attrs, "data": frame.to_json(orient="split", date_format="iso"),
        })
    attrs.update(requested_start=start.isoformat(), requested_end=end.isoformat())
    frame.attrs = attrs
    return frame


def provider_history(
    provider: MarketDataProvider, security: Security, start: date, end: date,
    *, benchmark: bool = False,
) -> pd.DataFrame:
    """Range methods use an exclusive end; legacy adapters remain supported."""
    method = getattr(provider, "benchmark_history_range" if benchmark else "history_range", None)
    supports_range = security.market in getattr(provider, "range_markets", {"US", "HK", "A"})
    if callable(method) and supports_range:
        frame = (
            method(security.market, start, end + timedelta(days=1)) if benchmark else
            method(security.market, security.symbol, start, end + timedelta(days=1),
                   provider_data=security.provider_data)
        )
    else:
        # 'max' avoids losing old entry dates in legacy providers that only accept periods.
        frame = (provider.benchmark_history(security.market, period="max") if benchmark else
                 provider.history(security.market, security.symbol, period="max"))
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("行情来源未返回日线表格")
    frame = bounded_frame(frame, start, end, security.timezone)
    if frame.attrs.get("price_basis") == "split_adjusted" and "Adj Close" in frame:
        if "Close" in frame:
            ratio = pd.to_numeric(frame["Adj Close"], errors="coerce") / frame["Close"]
            for field in ("Open", "High", "Low"):
                if field in frame:
                    frame[f"Adj {field}"] = frame[field] * ratio
            verified = bool(not frame.empty and ratio.notna().all() and (ratio > 0).all())
            frame.attrs.update(
                adjustment_status="verified" if verified else "unavailable",
                analysis_price_basis="total_return_adjusted" if verified else "unknown",
            )
    return label_history(frame, start, end)


def _bar_values(row: DailyBar) -> dict[str, Any]:
    return {"trade_date": row.trade_date.isoformat(),
            **{field: getattr(row, field) for field in BAR_FIELDS}}


def save_history_revision(
    session: Session, security: Security, bars: list[dict[str, Any]],
    metadata: dict[str, Any], now: datetime,
) -> str | None:
    """Upsert a validated Yahoo range and retain every changed value in an audit row."""
    if not bars:
        return None
    start, end = bars[0]["trade_date"], bars[-1]["trade_date"]
    existing = {row.trade_date: row for row in session.scalars(select(DailyBar).where(
        DailyBar.security_id == security.id, DailyBar.source == "yfinance",
    ).execution_options(populate_existing=True))}
    from .daily_bars import _requires_full_history

    if _requires_full_history(list(existing.values()), bars) and not set(existing).issubset(
        {bar["trade_date"] for bar in bars}
    ):
        raise ValueError("抓取期间行情口径变化，需重试完整保存区间，未覆盖原行情")
    state = session.get(DailyBarSyncState, security.id, populate_existing=True)
    if state is None:
        state = DailyBarSyncState(security_id=security.id, source="yfinance")
        session.add(state)
    changes = []
    for bar in bars:
        old = existing.get(bar["trade_date"])
        after = {**bar, "trade_date": bar["trade_date"].isoformat()}
        before = _bar_values(old) if old is not None else None
        if before != after or not state.source_version:
            changes.append({"date": after["trade_date"], "before": before, "after": after})
        if old is None:
            session.add(DailyBar(security_id=security.id, source="yfinance", fetched_at=now, **bar))
        else:
            for field in BAR_FIELDS:
                setattr(old, field, bar[field])
            old.fetched_at = now
    stable_metadata = {key: metadata.get(key) for key in (
        "source", "currency", "timezone", "price_basis", "analysis_price_basis", "volume_unit",
    )}
    stable_metadata["source"] = "yfinance"
    stable_metadata["analysis_price_basis"] = (
        "total_return_adjusted" if all(bar["adj_close"] for bar in bars) else "unknown"
    )
    previous = session.scalar(select(DailyBarRevision).where(
        DailyBarRevision.security_id == security.id,
        DailyBarRevision.source_version == state.source_version,
    )) if state.source_version else None
    previous_metadata = {
        key: value for key, value in (previous.metadata_json if previous else {}).items()
        if key != "input_source_version"
    }
    if changes or not state.source_version or previous_metadata != stable_metadata:
        version = digest({"previous": state.source_version, "changes": changes,
                          "metadata": stable_metadata})
        session.add(DailyBarRevision(
            security_id=security.id, source="yfinance", source_version=version,
            previous_version=state.source_version, observed_at=now,
            start_date=start, end_date=end,
            metadata_json={
                **stable_metadata, "input_source_version": metadata.get("source_version"),
            },
            changes=changes,
        ))
        state.source_version = version
    state.currency = metadata.get("currency")
    state.timezone = metadata.get("timezone")
    state.coverage_start = min(state.coverage_start or start, start)
    state.coverage_end = max(state.coverage_end or end, end)
    state.last_success_at = now
    return state.source_version


def cached_history(session: Session, security: Security, start: date, end: date) -> pd.DataFrame:
    """Read only. Incomplete rows remain visible so callers can diagnose exact gaps."""
    if security.market not in {"US", "HK"} or security.id is None:
        return pd.DataFrame()
    state = session.get(DailyBarSyncState, security.id)
    rows = list(session.scalars(select(DailyBar).where(
        DailyBar.security_id == security.id, DailyBar.source == "yfinance",
        DailyBar.trade_date >= start, DailyBar.trade_date <= end,
    ).order_by(DailyBar.trade_date)))
    if not rows or state is None:
        return pd.DataFrame()
    frame = pd.DataFrame([{
        "Open": row.open, "High": row.high, "Low": row.low, "Close": row.close,
        "Volume": row.volume, "Amount": row.amount, "Adj Close": row.adj_close,
        "Dividends": row.dividends, "Stock Splits": row.stock_splits,
    } for row in rows], index=pd.to_datetime([row.trade_date for row in rows]))
    ratio = frame["Adj Close"] / frame["Close"]
    for field in ("Open", "High", "Low"):
        frame[f"Adj {field}"] = frame[field] * ratio
    verified = bool(ratio.notna().all() and (ratio > 0).all())
    frame.attrs.update(
        source="yfinance", source_version=state.source_version,
        currency=state.currency, timezone=state.timezone,
        price_basis="split_adjusted", analysis_price_basis="total_return_adjusted",
        adjustment_status="verified" if verified else "unavailable", volume_unit="shares",
        amount_unit=state.currency, amount_method="provider_reported", cache="local",
        latest_available_date=state.coverage_end.isoformat() if state.coverage_end else None,
    )
    return label_history(frame, start, end)


def cache_usable(frame: pd.DataFrame, security: Security, start: date, end: date) -> bool:
    if frame.empty or frame.index.has_duplicates:
        return False
    if (frame.attrs.get("currency") != security.currency
            or frame.attrs.get("adjustment_status") != "verified"):
        return False
    if frame.attrs.get("timezone") not in {
        security.timezone, "US/Eastern" if security.market == "US" else security.timezone,
    }:
        return False
    expected = calendar_for_market(security.market).sessions_in_range(start, end)
    if not set(expected.date).issubset(set(frame.index.date)):
        return False
    columns = [f"{prefix}{field}" for prefix in ("", "Adj ")
               for field in ("Open", "High", "Low", "Close")]
    if any(field not in frame for field in columns):
        return False
    values = frame[columns].apply(pd.to_numeric, errors="coerce")
    volume = pd.to_numeric(frame.get("Volume"), errors="coerce")
    if not bool(((values > 0) & (values < float("inf"))).all().all()):
        return False
    for prefix in ("", "Adj "):
        if not bool(((values[f"{prefix}Low"] <= values[[f"{prefix}Open", f"{prefix}Close"]].min(
            axis=1
        )) & (values[f"{prefix}High"] >= values[[f"{prefix}Open", f"{prefix}Close"]].max(
            axis=1
        ))).all()):
            return False
    return bool(volume.notna().all() and ((volume >= 0) & (volume < float("inf"))).all())


class HistoryRepository:
    def __init__(
        self, sessions: Session | SessionFactory, provider: MarketDataProvider,
        *, allow_fetch: bool = False,
    ):
        self.sessions = sessions
        self.provider = provider
        self.allow_fetch = allow_fetch
        self._frames: dict[tuple[str, str, date, date, bool], pd.DataFrame | Exception] = {}
        self._pending: list[tuple[Security, list[dict[str, Any]], dict[str, Any]]] = []

    def _session(self) -> Any:
        return nullcontext(self.sessions) if isinstance(self.sessions, Session) else self.sessions()

    def security(self, market: str, symbol: str) -> Security:
        with self._session() as session:
            security = session.scalar(select(Security).where(
                Security.market == market, Security.symbol == symbol,
            ))
            if security is not None:
                return security
        return Security(market=market, symbol=symbol, currency={"A": "CNY", "HK": "HKD",
                        "US": "USD"}[market], timezone={"A": "Asia/Shanghai",
                        "HK": "Asia/Hong_Kong", "US": "America/New_York"}[market],
                        provider_data={})

    def history(
        self, security: Security, start: date, end: date, *, benchmark: bool = False,
    ) -> pd.DataFrame:
        if start > end:
            raise ValueError("起始日期不能晚于结束日期")
        key = security.market, security.symbol, start, end, benchmark
        if key not in self._frames:
            try:
                self._frames[key] = self._load(security, start, end, benchmark)
            except Exception as exc:
                self._frames[key] = exc
        result = self._frames[key]
        if isinstance(result, Exception):
            raise result
        return result.copy()

    def _load(self, security: Security, start: date, end: date, benchmark: bool) -> pd.DataFrame:
        with self._session() as session:
            cached = cached_history(session, security, start, end)
        if cache_usable(cached, security, start, end) or not self.allow_fetch:
            return cached
        frame = provider_history(self.provider, security, start, end, benchmark=benchmark)
        if (security.id is not None and security.market in {"US", "HK"}
                and frame.attrs.get("source") == "yfinance"
                and frame.attrs.get("price_basis") == "split_adjusted"):
            from .daily_bars import _normalize, _requires_full_history

            bars = _normalize(frame, security, start, end)
            with self._session() as session:
                existing = list(session.scalars(select(DailyBar).where(
                    DailyBar.security_id == security.id, DailyBar.source == "yfinance",
                ).order_by(DailyBar.trade_date)))
            combined = {row.trade_date: row for row in existing}
            for pending_security, pending_bars, _metadata in self._pending:
                if pending_security.id == security.id:
                    combined.update({bar["trade_date"]: DailyBar(
                        security_id=security.id, source="yfinance", **bar,
                    ) for bar in pending_bars})
            existing = sorted(combined.values(), key=lambda row: row.trade_date)
            # A changed adjustment factor must replace the whole saved basis, not a splice.
            if existing and _requires_full_history(existing, bars):
                full_start, full_end = min(start, existing[0].trade_date), max(
                    end, existing[-1].trade_date,
                )
                if (full_start, full_end) != (start, end):
                    frame = provider_history(self.provider, security, full_start, full_end,
                                             benchmark=benchmark)
                    bars = _normalize(frame, security, full_start, full_end)
                if not {row.trade_date for row in existing}.issubset(
                    {bar["trade_date"] for bar in bars}
                ):
                    raise ValueError("完整修订缺少既有交易日，保留原行情")
            self._pending.append((security, bars, dict(frame.attrs)))
        return bounded_frame(frame, start, end, security.timezone)

    def persist(
        self, session: Session, now: datetime | None = None,
    ) -> dict[str, str]:
        """Only call after network fetching; the caller commits the short write transaction."""
        versions = {}
        for security, bars, metadata in self._pending:
            version = save_history_revision(
                session, security, bars, metadata, utc(now or datetime.now(UTC)),
            )
            if version and metadata.get("source_version"):
                versions[str(metadata["source_version"])] = version
        for frame in self._frames.values():
            if not isinstance(frame, Exception) and frame.attrs.get("source_version") in versions:
                frame.attrs["source_version"] = versions[frame.attrs["source_version"]]
        self._pending.clear()
        return versions
