"""Cached, source-labelled market observations on actual provider trading sessions."""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import SessionFactory
from ..models import Security, Watchlist
from ..risk_models import MarketResearchSnapshot, SecurityRiskProfile
from .providers import MarketDataProvider, build_market_data_provider
from .risk_defaults import BENCHMARK_FIELDS, benchmark_inputs

if TYPE_CHECKING:
    from .history_repository import HistoryRepository

HORIZONS = (1, 5, 20)
MARKET_RESEARCH_VERSION = "market-observations-v2"
CALENDAR_NAMES = {"A": "XSHG", "HK": "XHKG", "US": "XNYS"}


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return utc(value) if isinstance(value, datetime) else None


def calendar_for_market(market: str) -> Any:
    if market not in CALENDAR_NAMES:
        raise ValueError("市场交易日历未配置")
    return xcals.get_calendar(CALENDAR_NAMES[market])


def quote_session_age(market: str, observed_at: datetime, now: datetime) -> int | None:
    """Count exchange sessions completed after the observed close, including suspensions."""
    try:
        calendar = calendar_for_market(market)
        start = pd.Timestamp(utc(observed_at).astimezone(calendar.tz).date())
        end = pd.Timestamp(utc(now).astimezone(calendar.tz).date())
        if start < calendar.first_session or end > calendar.last_session or observed_at > now:
            return None
        schedule = calendar.schedule.loc[start:end]
        closes = schedule["close"]
        return int(
            ((closes > pd.Timestamp(utc(observed_at))) & (closes <= pd.Timestamp(utc(now)))).sum()
        )
    except (ValueError, KeyError, TypeError):
        return None


def closed_bars(
    frame: pd.DataFrame, market: str, timezone: str, now: datetime
) -> list[dict[str, Any]]:
    """Keep actual observed sessions after exchange close, including half days."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    zone = ZoneInfo(timezone)
    calendar = calendar_for_market(market)
    current_date = pd.Timestamp(utc(now).astimezone(calendar.tz).date())
    if current_date < calendar.first_session or current_date > calendar.last_session:
        raise ValueError("交易日历超出覆盖范围，不能推定工作日为交易日")
    bars: dict[str, dict[str, Any]] = {}
    for stamp, row in frame.sort_index().iterrows():
        timestamp = pd.Timestamp(stamp)
        if pd.isna(timestamp):
            continue
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert(zone)
        date = timestamp.date()
        if date > utc(now).astimezone(zone).date():
            continue
        if not calendar.is_session(pd.Timestamp(date)):
            continue
        observed_at = calendar.session_close(pd.Timestamp(date)).to_pydatetime()
        if observed_at > utc(now):
            continue
        bar: dict[str, Any] = {"date": date.isoformat(), "observed_at": observed_at.isoformat()}
        for field in ("Open", "High", "Low", "Close", "Volume", "Amount"):
            bar[field.lower()] = number(row.get(field))
        for field in ("Open", "High", "Low", "Close"):
            bar[f"adj_{field.lower()}"] = number(row.get(f"Adj {field}"))
        bar["valid"] = (
            all(bar[key] is not None and bar[key] > 0 for key in ("open", "high", "low", "close"))
            and bar["low"] <= min(bar["open"], bar["close"])
            and bar["high"] >= max(bar["open"], bar["close"])
        )
        bars[date.isoformat()] = bar
    return list(bars.values())


def adjusted_window_valid(bars: list[dict[str, Any]], metadata: dict[str, Any]) -> bool:
    return bool(
        bars
        and metadata.get("adjustment_status") == "verified"
        and metadata.get("analysis_price_basis")
        in {"total_return_adjusted", "split_adjusted", "index_price_return"}
        and all(
            bar.get("valid")
            and all(
                bar.get(f"adj_{field}") is not None and bar[f"adj_{field}"] > 0
                for field in ("open", "high", "low", "close")
            )
            for bar in bars
        )
    )


def _series_result(
    bars: list[dict[str, Any]],
    metadata: dict[str, Any],
    horizon: int,
    benchmark_bars: list[dict[str, Any]],
    benchmark_metadata: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "horizon": horizon,
        "absolute_return_pct": None,
        "industry_return_pct": None,
        "industry_excess_return_pct": None,
        "start_date": None,
        "end_date": None,
        "status": "insufficient_history",
        "blockers": [],
    }
    if len(bars) < horizon + 1:
        result["blockers"].append(f"需要 {horizon + 1} 个完整交易日观察值")
        return result
    window = bars[-horizon - 1 :]
    result.update(start_date=window[0]["date"], end_date=window[-1]["date"])
    if not adjusted_window_valid(window, metadata):
        result["status"] = "blocked"
        result["blockers"].append("复权或 OHLC 口径未核实，避免公司行动污染收益")
        return result
    stock_return = (window[-1]["adj_close"] / window[0]["adj_close"] - 1) * 100
    result.update(status="ready", absolute_return_pct=stock_return)
    benchmark_by_date = {bar["date"]: bar for bar in benchmark_bars}
    matched = [benchmark_by_date[bar["date"]] for bar in window if bar["date"] in benchmark_by_date]
    if len(matched) != len(window):
        result["blockers"].append("行业基准未配置或交易日不完全对齐，行业超额收益未知")
    elif metadata.get("analysis_price_basis") != benchmark_metadata.get("analysis_price_basis"):
        result["blockers"].append("证券与行业基准收益口径不同，不能比较")
    elif metadata.get("currency") != benchmark_metadata.get("currency"):
        result["blockers"].append("证券与行业基准币种不同，尚未处理汇率影响")
    elif adjusted_window_valid(matched, benchmark_metadata):
        benchmark_return = (matched[-1]["adj_close"] / matched[0]["adj_close"] - 1) * 100
        result.update(
            industry_return_pct=benchmark_return,
            industry_excess_return_pct=stock_return - benchmark_return,
        )
    else:
        result["blockers"].append("行业基准复权口径未核实")
    return result


def _liquidity(bars: list[dict[str, Any]], metadata: dict[str, Any]) -> dict[str, Any]:
    volumes = [bar.get("volume") for bar in bars[-20:]]
    comparable = metadata.get("volume_unit") == "shares"
    ratio_changes = []
    for bar in bars[-21:]:
        if bar.get("close") and bar.get("adj_close"):
            ratio_changes.append(bar["adj_close"] / bar["close"])
    company_action = bool(ratio_changes and max(ratio_changes) / min(ratio_changes) - 1 > 0.001)
    enough = len(volumes) == 20 and all(value is not None and value > 0 for value in volumes)
    mean_volume = (
        sum(float(value) for value in volumes if value is not None) / len(volumes)
        if comparable and enough
        else None
    )
    previous = [bar.get("volume") for bar in bars[-21:-1]]
    volume_ratio = None
    if (
        comparable
        and len(previous) == 20
        and not company_action
        and all(value is not None and value > 0 for value in previous)
        and bars[-1].get("volume") is not None
    ):
        volume_ratio = bars[-1]["volume"] / (
            sum(float(value) for value in previous if value is not None) / 20
        )
    amounts = [bar.get("amount") for bar in bars[-20:]]
    mean_amount = None
    if len(amounts) == 20 and all(value is not None and value > 0 for value in amounts):
        if metadata.get("amount_unit") == metadata.get("currency"):
            mean_amount = sum(float(value) for value in amounts if value is not None) / 20
    return {
        "average_volume_20": mean_volume,
        "volume_unit": metadata.get("volume_unit", "unknown"),
        "volume_ratio_20": volume_ratio,
        "average_amount_20": mean_amount,
        "amount_unit": metadata.get("amount_unit", "unknown"),
        "amount_method": metadata.get("amount_method", "unknown"),
        "company_action_in_volume_window": company_action,
        "sizing_volume_ready": bool(mean_volume and not company_action),
        "note": "成交量按原始股数；检测到复权因子变化时暂停量比和基于量能的数量测算。",
    }


def analyze_market_frame(
    security: Security,
    frame: pd.DataFrame,
    *,
    now: datetime,
    benchmark_frame: pd.DataFrame | None = None,
    benchmark: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(frame.attrs) if isinstance(frame, pd.DataFrame) else {}
    bars = [
        bar
        for bar in closed_bars(frame, security.market, security.timezone, now)
        if bar.get("volume") != 0
    ]
    benchmark_metadata = dict(benchmark_frame.attrs) if benchmark_frame is not None else {}
    benchmark_bars = (
        closed_bars(benchmark_frame, security.market, security.timezone, now)
        if benchmark_frame is not None
        else []
    )
    blockers = []
    quote: dict[str, Any] = {}
    if bars:
        last = bars[-1]
        quote_basis = metadata.get("price_basis", "unknown")
        if quote_basis == "split_adjusted":
            schedule = calendar_for_market(security.market).schedule
            completed = schedule[schedule["close"] <= pd.Timestamp(utc(now))]
            latest = completed.index[-1].date().isoformat() if not completed.empty else None
            # Only the current final close has no later split to undo. Historical chart
            # prices retain their split-adjusted label and cannot become execution quotes.
            if last["date"] == latest and (
                not metadata.get("latest_available_date")
                or metadata["latest_available_date"] <= last["date"]
            ):
                quote_basis = "raw"
        quote = {
            "price": last["close"],
            "as_of": last["observed_at"],
            "currency": metadata.get("currency", "unknown"),
            "price_basis": quote_basis,
            "history_price_basis": metadata.get("price_basis", "unknown"),
            "source": metadata.get("source", "unknown"),
            "source_version": metadata.get("source_version"),
            "kind": "completed_daily_close",
            "valid": bool(last["valid"]),
        }
        quote_time = parse_time(last["observed_at"])
        age = quote_session_age(security.market, quote_time, utc(now)) if quote_time else None
        if age is None:
            blockers.append("交易所日历不可用或超出覆盖范围，报价时效未知")
        elif age > 1:
            blockers.append("最近完整日线已缺失超过 1 个交易所交易日，需核对停牌或刷新行情")
        quote["age_sessions"] = age
        if not last["valid"]:
            blockers.append("最新 OHLC 数据无效")
    else:
        blockers.append("没有已收盘的有效日线行情")
    if not adjusted_window_valid(bars[-21:], metadata):
        blockers.append("复权口径未核实或行情不完整，暂停收益与风险测算")
    if metadata.get("currency") != security.currency:
        blockers.append("行情币种与证券币种不一致或未知")
    horizons = {
        str(h): _series_result(bars, metadata, h, benchmark_bars, benchmark_metadata)
        for h in HORIZONS
    }
    volatility = None
    if len(bars) >= 21 and adjusted_window_valid(bars[-21:], metadata):
        changes = [
            math.log(later["adj_close"] / earlier["adj_close"])
            for earlier, later in zip(bars[-21:-1], bars[-20:], strict=True)
        ]
        volatility = float(pd.Series(changes).std(ddof=1)) * math.sqrt(252) * 100
    return {
        "version": MARKET_RESEARCH_VERSION,
        "security_id": security.id,
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "as_of": utc(now).isoformat(),
        "quote": quote,
        "metadata": metadata,
        "source_version": metadata.get("source_version"),
        "horizons": horizons,
        "annualized_volatility_20_pct": volatility,
        "liquidity": _liquidity(bars, metadata),
        "benchmark": benchmark,
        "bars": bars,
        "benchmark_bars": benchmark_bars,
        "benchmark_metadata": benchmark_metadata,
        "pricing_status": "unverified",
        "pricing_note": "量价反应是观察事实，不能单凭上涨断言消息已充分计价。",
        "calendar": {"name": CALENDAR_NAMES.get(security.market), "source": "exchange_calendars"},
        "calendar_note": (
            "周期使用来源实际日线；交易所日历核验节假日、半日及收盘时间，"
            "个股停牌不会延长报价有效期。"
        ),
    }


class MarketResearchService:
    def __init__(self, settings: Settings, provider: MarketDataProvider | None = None):
        self.provider = provider or build_market_data_provider(settings)

    def refresh(
        self,
        session: Session,
        security_ids: list[int] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Keep caller transaction ownership; background work uses refresh_isolated."""
        from .history_repository import HistoryRepository

        timestamp = utc(now or datetime.now(UTC))
        inputs = self._inputs(session, security_ids)
        repository = HistoryRepository(session, self.provider, allow_fetch=True)
        payloads = list(self._payloads(inputs, timestamp, repository))
        versions = repository.persist(session, timestamp)
        for payload in payloads:
            self._label_versions(payload, versions)
        results = [
            self._save(session, timestamp, payload) for payload in payloads
        ]
        return {"count": len(results), "results": results}

    def refresh_isolated(
        self,
        session_factory: SessionFactory,
        security_ids: list[int] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch each observation outside write transactions, preserving earlier commits."""
        from .history_repository import HistoryRepository

        timestamp = utc(now or datetime.now(UTC))
        with session_factory() as session:
            inputs = self._inputs(session, security_ids)
        results = []
        repository = HistoryRepository(session_factory, self.provider, allow_fetch=True)
        for payload in self._payloads(inputs, timestamp, repository):
            with session_factory() as session:
                versions = repository.persist(session, timestamp)
                self._label_versions(payload, versions)
                result = self._save(session, timestamp, payload)
                session.commit()
                results.append(result)
        return {"count": len(results), "results": results}

    def refresh_cached(
        self, session_factory: SessionFactory, security_ids: list[int],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Rebuild deterministic observations after chart updates, without source requests."""
        from .history_repository import HistoryRepository

        timestamp = utc(now or datetime.now(UTC))
        with session_factory() as session:
            inputs = self._inputs(session, security_ids)
        repository = HistoryRepository(session_factory, self.provider, allow_fetch=False)
        by_id = {security.id: security for security, _benchmark in inputs}
        results = []
        updated = []
        skipped = []
        for payload in self._payloads(inputs, timestamp, repository):
            identifier = payload["security_id"]
            security = by_id[identifier]
            try:
                calendar = calendar_for_market(security.market)
                closed = calendar.schedule[calendar.schedule["close"] <= pd.Timestamp(timestamp)]
                required = {stamp.date().isoformat() for stamp in closed.index[-21:]}
                observed = {bar["date"]: bar for bar in payload.get("bars") or []}
                complete = bool(
                    payload.get("status") == "ready" and len(required) == 21
                    and required.issubset(observed)
                    and all(observed[day].get("valid")
                            and observed[day].get("volume") is not None
                            and observed[day]["volume"] > 0 for day in required)
                )
            except (ValueError, KeyError, TypeError):
                complete = False
            if not complete:
                skipped.append(identifier)
                continue
            with session_factory() as session:
                results.append(self._save(session, timestamp, payload))
                session.commit()
            updated.append(identifier)
        return {"count": len(results), "results": results, "updated_security_ids": updated,
                "skipped": len(skipped), "skipped_ids": skipped}

    @staticmethod
    def _label_versions(
        payload: dict[str, Any], versions: dict[str, str],
    ) -> None:
        version = versions.get(str((payload.get("metadata") or {}).get("source_version") or ""))
        if version:
            payload["source_version"] = payload["metadata"]["source_version"] = version
            if payload.get("quote"):
                payload["quote"]["source_version"] = version
        version = versions.get(str(
            (payload.get("benchmark_metadata") or {}).get("source_version") or ""
        ))
        if version:
            payload["benchmark_metadata"]["source_version"] = version

    @staticmethod
    def _inputs(
        session: Session, security_ids: list[int] | None
    ) -> list[tuple[Security, dict[str, Any]]]:
        query = select(Security).where(Security.active.is_(True))
        if security_ids is not None:
            query = query.where(Security.id.in_(security_ids))
        else:
            query = query.join(Watchlist).where(Watchlist.active.is_(True))
        inputs = []
        for security in session.scalars(query):
            profile = session.scalar(
                select(SecurityRiskProfile).where(SecurityRiskProfile.security_id == security.id)
            )
            values = benchmark_inputs(security, {
                key: getattr(profile, key) if profile else None for key in BENCHMARK_FIELDS
            })
            inputs.append((security, values))
        return inputs

    def _payloads(
        self, inputs: list[tuple[Security, dict[str, Any]]], timestamp: datetime,
        repository: HistoryRepository,
    ) -> Iterator[dict[str, Any]]:
        history_cache: dict[tuple[str, str], pd.DataFrame | Exception] = {}

        def history(market: str, symbol: str) -> pd.DataFrame:
            key = (market, symbol)
            if key not in history_cache:
                try:
                    calendar = calendar_for_market(market)
                    completed = calendar.schedule[calendar.schedule["close"] <= pd.Timestamp(
                        timestamp
                    )]
                    end = completed.index[-1].date()
                    history_cache[key] = repository.history(
                        repository.security(market, symbol), end - timedelta(days=180), end,
                    )
                except Exception as exc:
                    history_cache[key] = exc
            result = history_cache[key]
            if isinstance(result, Exception):
                raise result
            return result

        for security, benchmark_values in inputs:
            benchmark = None
            benchmark_frame = None
            try:
                frame = history(security.market, security.symbol)
                if benchmark_values["benchmark_symbol"] and benchmark_values["benchmark_market"]:
                    benchmark = {
                        key.removeprefix("benchmark_"): value
                        for key, value in benchmark_values.items()
                    }
                    key = (benchmark["market"], benchmark["symbol"])
                    if benchmark["market"] == security.market:
                        try:
                            benchmark_frame = history(*key)
                        except Exception:
                            benchmark_frame = None
                payload = analyze_market_frame(
                    security,
                    frame,
                    now=timestamp,
                    benchmark_frame=benchmark_frame,
                    benchmark=benchmark,
                )
            except Exception as exc:
                payload = {
                    "security_id": security.id,
                    "status": "error",
                    "blockers": [f"行情刷新失败（{type(exc).__name__}），请核对来源覆盖与权限"],
                    "as_of": timestamp.isoformat(),
                    "quote": {},
                    "horizons": {},
                }
            yield payload

    @staticmethod
    def _save(session: Session, timestamp: datetime, payload: dict[str, Any]) -> dict[str, Any]:
        snapshot = MarketResearchSnapshot(
            security_id=payload["security_id"], as_of=timestamp,
            status=payload["status"], payload=payload,
        )
        session.add(snapshot)
        session.flush()
        return {"snapshot_id": snapshot.id, **payload}


def get_market_research(
    session: Session, security_id: int, now: datetime | None = None
) -> dict[str, Any]:
    snapshot = session.scalar(
        select(MarketResearchSnapshot)
        .where(MarketResearchSnapshot.security_id == security_id)
        .order_by(MarketResearchSnapshot.as_of.desc(), MarketResearchSnapshot.id.desc())
        .limit(1)
    )
    security = session.get(Security, security_id) if snapshot is not None else None
    return market_research_payload(security_id, security, snapshot, now)


def market_research_payload(
    security_id: int,
    security: Security | None,
    snapshot: MarketResearchSnapshot | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render an already loaded observation without issuing database queries."""
    if snapshot is None:
        return {
            "status": "missing",
            "security_id": security_id,
            "quote": {},
            "horizons": {},
            "blockers": ["尚未刷新量价与复权行情"],
        }
    payload = {**snapshot.payload, "snapshot_id": snapshot.id}
    timestamp = parse_time((payload.get("quote") or {}).get("as_of"))
    age = (
        quote_session_age(security.market, timestamp, utc(now or datetime.now(UTC)))
        if security and timestamp
        else None
    )
    if timestamp is not None and (age is None or age > 1):
        payload["status"] = "stale"
        payload["blockers"] = list(
            dict.fromkeys(
                [
                    *(payload.get("blockers") or []),
                    "交易所日历覆盖未知"
                    if age is None
                    else "行情已过期，请重新刷新后评估数量与收益",
                ]
            )
        )
    return payload
