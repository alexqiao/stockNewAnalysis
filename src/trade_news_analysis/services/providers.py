"""Capability-based multi-market data providers with explicit degradation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import re
import threading
from _thread import LockType
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, timedelta
from time import monotonic
from typing import Any, NoReturn, Protocol
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pandas as pd
from investormate import Stock

from ..config import Settings

logger = logging.getLogger(__name__)

BENCHMARKS = {"A": "CN_CSI300", "HK": "HK_HSI", "US": "US_SPY"}
TUSHARE_BENCHMARKS = {
    "CN_CSI300": ("index_daily", "000300.SH"),
    "HK_HSI": ("hk_daily", "HSI.HK"),
    "US_SPY": ("us_daily", "SPY"),
}


@dataclass(slots=True)
class SecurityRecord:
    market: str
    exchange: str
    symbol: str
    name: str
    aliases: list[str]
    industry: str = ""
    business_summary: str = ""
    market_cap: float | None = None
    currency: str = "USD"
    timezone: str = "America/New_York"
    calendar: str = "US"
    provider_data: dict[str, Any] | None = None


@dataclass(slots=True)
class FundamentalSnapshot:
    fiscal_year: int | None
    price: float | None
    market_cap: float | None
    shares_outstanding: float | None
    revenue: float | None
    net_income: float | None
    source: str = "investormate/yfinance"


class SecurityMasterProvider(Protocol):
    name: str
    markets: tuple[str, ...]

    def fetch_securities(self) -> list[SecurityRecord]: ...


class MarketDataProvider(Protocol):
    name: str

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame: ...

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame: ...


class FundamentalDataProvider(Protocol):
    name: str

    def fetch(
        self,
        market: str,
        symbol: str,
        provider_data: Mapping[str, Any] | None = None,
    ) -> FundamentalSnapshot: ...


class CalendarProvider(Protocol):
    def trading_dates(self, market: str, start: str, end: str) -> list[str]: ...


class TushareRateLimitError(RuntimeError):
    def __init__(self, api_name: str, retry_after_seconds: float):
        self.api_name = api_name
        self.retry_after_seconds = max(1, math.ceil(retry_after_seconds))
        super().__init__(
            f"Tushare {api_name} 限频冷却中，约 {self.retry_after_seconds} 秒后可重试"
        )


@dataclass
class _TushareRequestGate:
    lock: LockType = dataclass_field(default_factory=threading.Lock)
    retry_at: float = 0.0


# Provider instances are rebuilt by different workflows, but their API quota is shared.
_tushare_gates: dict[tuple[str, str, str], _TushareRequestGate] = {}
_tushare_gates_lock = threading.Lock()


def _tushare_rate_limit_seconds(message: str) -> float | None:
    normalized = message.casefold()
    if not any(marker in normalized for marker in (
        "频率超限", "频次超限", "访问次数超限", "超过访问频率", "每分钟最多", "每小时最多",
        "每天最多", "rate limit", "too many requests", "frequency limit",
    )):
        return None
    units = {
        "秒": 1, "分钟": 60, "小时": 3600, "天": 86400,
        "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    }
    windows = re.findall(
        r"(?:/\s*|每\s*|per\s+)(?:(\d+)\s*)?"
        r"(小时|分钟|天|秒|second|minute|hour|day)s?", normalized,
    )
    return float(max((int(count or 1) * units[unit] for count, unit in windows), default=60))


class TushareClient:
    endpoint = "https://api.tushare.pro"

    def __init__(self, token: str, timeout: float):
        self.token = token
        self.timeout = timeout
        self._quota_identity = hashlib.sha256(token.encode()).hexdigest()

    def query(
        self, api_name: str, params: dict[str, Any] | None = None, fields: str = ""
    ) -> list[dict[str, Any]]:
        with _tushare_gates_lock:
            gate = _tushare_gates.setdefault(
                (self.endpoint, self._quota_identity, api_name), _TushareRequestGate()
            )
        with gate.lock:
            if gate.retry_at > monotonic():
                raise TushareRateLimitError(api_name, gate.retry_at - monotonic())
            try:
                return self._request(api_name, params, fields, gate)
            except HTTPError as exc:
                if exc.code != 429:
                    raise
                try:
                    wait = float(exc.headers.get("Retry-After", "60"))
                except (ValueError, TypeError, AttributeError):
                    wait = 60.0
                self._pause(api_name, gate, wait if math.isfinite(wait) and wait > 0 else 60)

    @staticmethod
    def _pause(api_name: str, gate: _TushareRequestGate, seconds: float) -> NoReturn:
        gate.retry_at = max(gate.retry_at, monotonic() + seconds + 1)
        error = TushareRateLimitError(api_name, gate.retry_at - monotonic())
        logger.warning(
            "Tushare %s 触发限频，暂停该接口请求约 %s 秒；同凭据的其他标的和基准共享冷却",
            api_name, error.retry_after_seconds,
        )
        raise error from None

    def _request(
        self, api_name: str, params: dict[str, Any] | None, fields: str,
        gate: _TushareRequestGate,
    ) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "api_name": api_name,
                "token": self.token,
                "params": params or {},
                "fields": fields,
            }
        ).encode()
        request = Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "tradeNewsAnalysis/0.2"},
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed endpoint
            body = json.loads(response.read())
        if body.get("code") not in (None, 0):
            message = str(body.get("msg") or "Tushare request failed")
            wait = _tushare_rate_limit_seconds(message)
            if wait is not None:
                self._pause(api_name, gate, wait)
            raise RuntimeError(message)
        data = body.get("data") or {}
        columns = data.get("fields") or []
        return [dict(zip(columns, item, strict=False)) for item in data.get("items") or []]


class TushareProvider:
    name: str = "tushare"
    markets: tuple[str, ...] = ("A", "HK", "US")

    def __init__(self, settings: Settings):
        if not settings.tushare_token:
            raise RuntimeError("TUSHARE_TOKEN 未配置")
        self.client = TushareClient(
            settings.tushare_token.get_secret_value(), settings.request_timeout_seconds
        )
        self.market_errors: dict[str, str] = {}

    def fetch_securities(self) -> list[SecurityRecord]:
        result: list[SecurityRecord] = []
        specs = (
            ("A", "stock_basic", "ts_code,symbol,name,industry,exchange"),
            ("HK", "hk_basic", "ts_code,name,enname,list_status"),
            ("US", "us_basic", "ts_code,name,enname,classify,list_status"),
        )
        for market, api_name, fields in specs:
            try:
                rows = self.client.query(api_name, {"list_status": "L"}, fields)
            except Exception as exc:
                self.market_errors[market] = f"{type(exc).__name__}: {exc}"
                continue
            for row in rows:
                symbol = str(row.get("ts_code") or row.get("symbol") or "").upper()
                if not symbol:
                    continue
                exchange = symbol.rsplit(".", 1)[-1] if "." in symbol else market
                currency, timezone, calendar = {
                    "A": ("CNY", "Asia/Shanghai", "CN"),
                    "HK": ("HKD", "Asia/Hong_Kong", "HK"),
                    "US": ("USD", "America/New_York", "US"),
                }[market]
                aliases = [str(row["enname"])] if row.get("enname") else []
                result.append(
                    SecurityRecord(
                        market=market,
                        exchange=exchange,
                        symbol=symbol,
                        name=str(row.get("name") or symbol),
                        aliases=aliases,
                        industry=str(row.get("industry") or row.get("classify") or ""),
                        currency=currency,
                        timezone=timezone,
                        calendar=calendar,
                        provider_data=row,
                    )
                )
        if not result and self.market_errors:
            details = "; ".join(
                f"{market}: {error}" for market, error in self.market_errors.items()
            )
            raise RuntimeError(details)
        return result

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        del period
        return self._history(market, symbol, {})

    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        return self._history(market, symbol, {
            "start_date": start.strftime("%Y%m%d"),
            "end_date": (end - timedelta(days=1)).strftime("%Y%m%d"),
        })

    def _history(self, market: str, symbol: str, bounds: dict[str, str]) -> pd.DataFrame:
        api_name = {"A": "daily", "HK": "hk_daily", "US": "us_daily"}[market]
        rows = self.client.query(
            api_name, {"ts_code": symbol, **bounds}, "trade_date,open,high,low,close,vol,amount"
        )
        frame = _price_frame(rows, market=market)
        if market == "A" and not frame.empty:
            try:
                factors = self.client.query(
                    "adj_factor", {"ts_code": symbol, **bounds}, "trade_date,adj_factor"
                )
                factor_frame = pd.DataFrame(factors)
                factor_frame.index = pd.to_datetime(factor_frame.pop("trade_date"))
                factor = pd.to_numeric(factor_frame["adj_factor"], errors="coerce")
                aligned = factor.reindex(frame.index)
                if aligned.notna().all() and (aligned > 0).all():
                    for field in ("Open", "High", "Low", "Close"):
                        frame[f"Adj {field}"] = frame[field] * aligned / aligned.iloc[-1]
                    frame.attrs["analysis_price_basis"] = "total_return_adjusted"
                    frame.attrs["adjustment_status"] = "verified"
            except Exception as exc:
                frame.attrs["adjustment_error"] = type(exc).__name__
        return frame

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        del period
        return self._benchmark_history(market, {})

    def benchmark_history_range(self, market: str, start: date, end: date) -> pd.DataFrame:
        return self._benchmark_history(market, {
            "start_date": start.strftime("%Y%m%d"),
            "end_date": (end - timedelta(days=1)).strftime("%Y%m%d"),
        })

    def _benchmark_history(self, market: str, bounds: dict[str, str]) -> pd.DataFrame:
        api_name, symbol = TUSHARE_BENCHMARKS[BENCHMARKS[market]]
        rows = self.client.query(
            api_name, {"ts_code": symbol, **bounds}, "trade_date,open,high,low,close,vol,amount"
        )
        frame = _price_frame(rows, market=market)
        if api_name == "index_daily":
            for field in ("Open", "High", "Low", "Close"):
                if field in frame:
                    frame[f"Adj {field}"] = frame[field]
            frame.attrs.update(
                analysis_price_basis="index_price_return", adjustment_status="verified"
            )
        return frame


class AkShareSecurityMasterProvider:
    name: str = "akshare"
    markets: tuple[str, ...] = ("A",)

    def fetch_securities(self) -> list[SecurityRecord]:
        import akshare as ak

        frame = ak.stock_info_a_code_name()
        result = []
        for row in frame.to_dict("records"):
            code = str(row.get("code") or row.get("股票代码") or "")
            if not code:
                continue
            if code.startswith(("5", "6", "9")):
                exchange = "SH"
            elif code.startswith(("4", "8")):
                exchange = "BJ"
            else:
                exchange = "SZ"
            result.append(
                SecurityRecord(
                    market="A",
                    exchange=exchange,
                    symbol=f"{code}.{exchange}",
                    name=str(row.get("name") or row.get("股票简称") or code),
                    aliases=[],
                    currency="CNY",
                    timezone="Asia/Shanghai",
                    calendar="CN",
                    provider_data=row,
                )
            )
        return result


def yahoo_symbol(
    market: str,
    symbol: str,
    provider_data: Mapping[str, Any] | None = None,
) -> str:
    configured = str((provider_data or {}).get("yahoo_symbol") or "").strip()
    if configured:
        return configured
    normalized = symbol.strip().upper()
    if market == "HK":
        match = re.fullmatch(r"(\d{1,5})(?:\.HK)?", normalized)
        if match:
            return f"{int(match.group(1)):04d}.HK"
    if market == "A" and normalized.endswith(".SH"):
        return f"{normalized.removesuffix('.SH')}.SS"
    return normalized


class YahooMarketDataProvider:
    name = "yfinance"
    range_markets = frozenset({"US", "HK"})

    @staticmethod
    def _symbol(market: str, symbol: str) -> str:
        return yahoo_symbol(market, symbol)

    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        """Fetch inclusive start/exclusive end without InvestorMate discarding fields."""
        import yfinance as yf

        if market not in {"US", "HK"}:
            raise ValueError("日 K 线暂仅支持美股和港股")
        ticker_symbol = yahoo_symbol(market, symbol, provider_data)
        if market == "US" and not (provider_data or {}).get("yahoo_symbol"):
            ticker_symbol = ticker_symbol.replace(".", "-")
        ticker = yf.Ticker(ticker_symbol)
        frame = ticker.history(
            start=start.isoformat(), end=end.isoformat(), interval="1d",
            auto_adjust=False, actions=True, repair=False, raise_errors=True,
            keepna=True, timeout=30,
        )
        metadata = ticker.get_history_metadata()
        currency = metadata.get("currency")
        timezone = metadata.get("exchangeTimezoneName")
        if currency != {"US": "USD", "HK": "HKD"}[market]:
            raise ValueError("Yahoo 行情币种缺失或与交易市场不匹配")
        allowed_zones = {
            "US": {"America/New_York", "US/Eastern"}, "HK": {"Asia/Hong_Kong"},
        }
        if timezone not in allowed_zones[market]:
            raise ValueError("Yahoo 交易所时区缺失或与交易市场不匹配")
        if not isinstance(frame, pd.DataFrame):
            raise ValueError("Yahoo 未返回日线表格")
        frame = frame.copy()
        frame.attrs.update(
            source=self.name, currency=currency, timezone=timezone,
            price_basis="split_adjusted", volume_unit="shares",
        )
        return frame

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        stock = Stock(self._symbol(market, symbol))
        result = stock.history(
            period=period, interval="1d", adjusted=False
        )
        raw = result.data if hasattr(result, "data") else result
        frame = raw.copy() if isinstance(raw, pd.DataFrame) else pd.DataFrame()
        frame.attrs.update(
            source=self.name, price_basis="raw", analysis_price_basis="unknown",
            adjustment_status="unavailable", volume_unit="shares",
            amount_unit={"A": "CNY", "HK": "HKD", "US": "USD"}.get(market, "unknown"),
            currency={"A": "CNY", "HK": "HKD", "US": "USD"}.get(market, "unknown"),
            amount_method="provider_reported" if "Amount" in frame else "unavailable",
        )
        try:
            adjusted_result = stock.history(period=period, interval="1d", adjusted=True)
            adjusted = (
                adjusted_result.data if hasattr(adjusted_result, "data") else adjusted_result
            )
            if isinstance(adjusted, pd.DataFrame) and not frame.empty:
                aligned = adjusted.reindex(frame.index)
                if all(field in aligned for field in ("Open", "High", "Low", "Close")):
                    for field in ("Open", "High", "Low", "Close"):
                        frame[f"Adj {field}"] = pd.to_numeric(aligned[field], errors="coerce")
                    adjusted_columns = [
                        f"Adj {field}" for field in ("Open", "High", "Low", "Close")
                    ]
                    if frame[adjusted_columns].notna().all().all():
                        frame.attrs.update(
                            analysis_price_basis="total_return_adjusted",
                            adjustment_status="verified",
                        )
        except Exception as exc:
            frame.attrs["adjustment_error"] = type(exc).__name__
        return frame

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        symbol = {"A": "000300.SS", "HK": "^HSI", "US": "SPY"}[market]
        return self.history(market, symbol, period)

    def benchmark_history_range(self, market: str, start: date, end: date) -> pd.DataFrame:
        symbol = {"A": "000300.SS", "HK": "^HSI", "US": "SPY"}[market]
        return self.history_range(market, symbol, start, end)


class FallbackMarketDataProvider:
    """Replace whole histories when selected markets fail or lack required price basis."""

    def __init__(
        self,
        primary: MarketDataProvider,
        fallback: MarketDataProvider,
        fallback_markets: frozenset[str],
        *,
        prefer_verified_adjustment: bool = False,
    ):
        self.primary = primary
        self.fallback = fallback
        self.fallback_markets = fallback_markets
        self.prefer_verified_adjustment = prefer_verified_adjustment
        self._unavailable_primary_markets: set[str] = set()
        self.name = f"{primary.name}+{fallback.name}"

    @staticmethod
    def _has_verified_adjustment(frame: pd.DataFrame, market: str) -> bool:
        if (
            frame.empty
            or frame.index.has_duplicates
            or frame.attrs.get("price_basis") not in {"raw", "split_adjusted"}
            or frame.attrs.get("adjustment_status") != "verified"
            or frame.attrs.get("analysis_price_basis")
            not in {"total_return_adjusted", "split_adjusted", "index_price_return"}
            or frame.attrs.get("currency") != {"A": "CNY", "HK": "HKD", "US": "USD"}.get(market)
        ):
            return False
        columns = [f"{prefix}{field}" for prefix in ("", "Adj ") for field in (
            "Open", "High", "Low", "Close"
        )]
        if any(column not in frame for column in columns):
            return False
        prices = frame[columns].apply(pd.to_numeric, errors="coerce")
        if not ((prices > 0) & (prices < float("inf"))).all().all():
            return False
        for prefix in ("", "Adj "):
            open_close = prices[[f"{prefix}Open", f"{prefix}Close"]]
            if not (
                (prices[f"{prefix}Low"] <= open_close.min(axis=1))
                & (prices[f"{prefix}High"] >= open_close.max(axis=1))
            ).all():
                return False
        return True

    def _prefer_adjusted_history(
        self, primary_frame: pd.DataFrame, market: str, symbol: str | None, period: str
    ) -> pd.DataFrame:
        if not self.prefer_verified_adjustment or self._has_verified_adjustment(
            primary_frame, market
        ):
            return primary_frame
        attempt = {
            "from": self.primary.name,
            "to": self.fallback.name,
            "reason": "primary_adjustment_unverified_or_incomplete",
            "status": "unavailable",
        }
        try:
            fallback_frame = (
                self.fallback.history(market, symbol, period)
                if symbol is not None
                else self.fallback.benchmark_history(market, period)
            )
            if isinstance(fallback_frame, pd.DataFrame) and self._has_verified_adjustment(
                fallback_frame, market
            ):
                result = fallback_frame.copy()
                result.attrs["adjustment_fallback"] = {**attempt, "status": "selected"}
                return result
            attempt["failure"] = "fallback_adjustment_unverified_or_incomplete"
        except Exception as exc:
            attempt["failure"] = type(exc).__name__
        result = primary_frame.copy()
        result.attrs["adjustment_fallback"] = attempt
        return result

    @staticmethod
    def _is_market_level_failure(reason: str) -> bool:
        normalized = reason.casefold()
        return any(
            marker in normalized
            for marker in ("没有接口", "无权限", "permission", "not authorized")
        )

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        if market not in self.fallback_markets:
            return self.primary.history(market, symbol, period)
        if market in self._unavailable_primary_markets:
            return self.fallback.history(market, symbol, period)
        try:
            result = self.primary.history(market, symbol, period)
            if isinstance(result, pd.DataFrame) and not result.empty:
                return self._prefer_adjusted_history(result, market, symbol, period)
            reason = "empty response"
        except TushareRateLimitError as exc:
            return self._rate_limit_fallback(market, symbol, period, exc)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if self._is_market_level_failure(reason):
                self._unavailable_primary_markets.add(market)
        logger.warning(
            "%s history unavailable for %s:%s; falling back to %s: %s",
            self.primary.name,
            market,
            symbol,
            self.fallback.name,
            reason,
        )
        return self.fallback.history(market, symbol, period)

    def _rate_limit_fallback(
        self, market: str, symbol: str | None, period: str, error: TushareRateLimitError,
    ) -> pd.DataFrame:
        frame = (
            self.fallback.history(market, symbol, period)
            if symbol is not None else self.fallback.benchmark_history(market, period)
        ).copy()
        frame.attrs["provider_fallback"] = {
            "from": self.primary.name, "to": self.fallback.name, "reason": "rate_limited",
            "api": error.api_name, "retry_after_seconds": error.retry_after_seconds,
        }
        return frame

    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        return self._range_history(market, symbol, start, end, provider_data)

    def benchmark_history_range(self, market: str, start: date, end: date) -> pd.DataFrame:
        return self._range_history(market, None, start, end, None)

    def _range_history(
        self, market: str, symbol: str | None, start: date, end: date,
        provider_data: Mapping[str, Any] | None,
    ) -> pd.DataFrame:
        from ..models import Security
        from .history_repository import provider_history

        security = Security(market=market, symbol=symbol or "benchmark",
                            timezone={"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong",
                                      "US": "America/New_York"}[market],
                            provider_data=dict(provider_data or {}))

        def fetch(provider: MarketDataProvider) -> pd.DataFrame:
            return provider_history(provider, security, start, end - timedelta(days=1),
                                    benchmark=symbol is None)

        if market not in self.fallback_markets:
            return fetch(self.primary)
        if market in self._unavailable_primary_markets:
            return fetch(self.fallback)
        primary_frame = None
        try:
            primary_frame = fetch(self.primary)
            if not primary_frame.empty and (not self.prefer_verified_adjustment or
                    self._has_verified_adjustment(primary_frame, market)):
                return primary_frame
        except Exception as exc:
            if self._is_market_level_failure(str(exc)):
                self._unavailable_primary_markets.add(market)
        try:
            fallback_frame = fetch(self.fallback)
            if not fallback_frame.empty:
                return fallback_frame
        except Exception:
            if primary_frame is None or primary_frame.empty:
                raise
        return primary_frame if primary_frame is not None else pd.DataFrame()

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        if market not in self.fallback_markets:
            return self.primary.benchmark_history(market, period)
        if market in self._unavailable_primary_markets:
            return self.fallback.benchmark_history(market, period)
        try:
            result = self.primary.benchmark_history(market, period)
            if isinstance(result, pd.DataFrame) and not result.empty:
                return self._prefer_adjusted_history(result, market, None, period)
            reason = "empty response"
        except TushareRateLimitError as exc:
            return self._rate_limit_fallback(market, None, period, exc)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if self._is_market_level_failure(reason):
                self._unavailable_primary_markets.add(market)
        logger.warning(
            "%s benchmark unavailable for %s; falling back to %s: %s",
            self.primary.name,
            market,
            self.fallback.name,
            reason,
        )
        return self.fallback.benchmark_history(market, period)


class InvestorMateFundamentalProvider:
    name = "investormate/yfinance"

    def fetch(
        self,
        market: str,
        symbol: str,
        provider_data: Mapping[str, Any] | None = None,
    ) -> FundamentalSnapshot:
        stock = Stock(yahoo_symbol(market, symbol, provider_data))
        info = stock.info or {}
        statement = stock.income_statement or {}
        fiscal_year, statement_row = _latest_complete_income_statement(statement)
        shares = _as_float(info.get("sharesOutstanding"))
        if shares is None and statement_row:
            shares = _first_number(
                statement_row,
                "Diluted Average Shares",
                "Basic Average Shares",
            )
        return FundamentalSnapshot(
            fiscal_year=fiscal_year,
            price=_as_float(stock.price),
            market_cap=_as_float(stock.market_cap) or _as_float(info.get("marketCap")),
            shares_outstanding=shares,
            revenue=_first_number(statement_row, "Total Revenue", "Operating Revenue"),
            net_income=_first_number(
                statement_row,
                "Net Income Common Stockholders",
                "Net Income",
            ),
            source=self.name,
        )


def _latest_complete_income_statement(
    statement: Mapping[str, Any],
) -> tuple[int | None, Mapping[str, Any]]:
    for period in sorted(statement, reverse=True):
        row = statement.get(period)
        if not isinstance(row, Mapping):
            continue
        revenue = _first_number(row, "Total Revenue", "Operating Revenue")
        net_income = _first_number(row, "Net Income Common Stockholders", "Net Income")
        match = re.match(r"(\d{4})", str(period))
        if match and revenue is not None and net_income is not None:
            return int(match.group(1)), row
    return None, {}


def _first_number(values: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(values.get(key))
        if value is not None:
            return value
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _price_frame(rows: list[dict[str, Any]], *, market: str | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame.index = pd.to_datetime(frame.pop("trade_date"))
    frame = frame.rename(columns={name: name.title() for name in ("open", "high", "low", "close")})
    frame = frame.rename(columns={"vol": "Volume", "amount": "Amount"})
    for field in ("Open", "High", "Low", "Close", "Volume", "Amount"):
        if field in frame:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
    if market == "A":
        if "Volume" in frame:
            frame["Volume"] *= 100
        if "Amount" in frame:
            frame["Amount"] *= 1000
    frame.attrs.update(
        source="tushare", price_basis="raw", analysis_price_basis="unknown",
        adjustment_status="unavailable",
        volume_unit="shares" if market == "A" else "provider_native_unknown",
        amount_unit="CNY" if market == "A" else "provider_native_unknown",
        currency={"A": "CNY", "HK": "HKD", "US": "USD"}.get(market or "", "unknown"),
        amount_method="provider_reported",
    )
    return frame.sort_index()


def build_security_master_provider(settings: Settings) -> SecurityMasterProvider | None:
    if settings.tushare_configured:
        return TushareProvider(settings)
    if settings.akshare_enabled and importlib.util.find_spec("akshare") is not None:
        return AkShareSecurityMasterProvider()
    return None


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    yahoo = YahooMarketDataProvider()
    if not settings.tushare_configured:
        return yahoo
    return FallbackMarketDataProvider(
        TushareProvider(settings),
        yahoo,
        fallback_markets=frozenset({"HK", "US"}),
        prefer_verified_adjustment=True,
    )


def lookup_security_record(market: str, value: str) -> SecurityRecord | None:
    """Verify one explicitly entered ticker through the existing Yahoo adapter."""
    raw_symbol = value.strip().upper()
    if market == "HK":
        match = re.fullmatch(r"(\d{1,5})(?:\.HK)?", raw_symbol)
        if match is None:
            return None
        number = int(match.group(1))
        symbol = f"{number:05d}.HK"
        yahoo_symbol = f"{number:04d}.HK"
        exchange = "HK"
    elif market == "A":
        match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ|SS))?", raw_symbol)
        if match is None:
            return None
        code, suffix = match.groups()
        inferred_exchange = (
            "SH"
            if code.startswith(("5", "6", "9"))
            else "BJ"
            if code.startswith(("4", "8"))
            else "SZ"
        )
        exchange = "SH" if suffix in {"SH", "SS"} else suffix or inferred_exchange
        symbol = f"{code}.{exchange}"
        yahoo_symbol = f"{code}.SS" if exchange == "SH" else f"{code}.{exchange}"
    else:
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", raw_symbol) is None:
            return None
        symbol = raw_symbol
        yahoo_symbol = raw_symbol
        exchange = "US"

    stock = Stock(yahoo_symbol)
    info = stock.info
    name = str(info.get("longName") or info.get("shortName") or "").strip()
    if not name:
        return None
    currency, timezone, calendar = {
        "A": ("CNY", "Asia/Shanghai", "CN"),
        "HK": ("HKD", "Asia/Hong_Kong", "HK"),
        "US": ("USD", "America/New_York", "US"),
    }[market]
    if market == "US":
        exchange = str(info.get("exchange") or exchange)[:20]
    market_cap = info.get("marketCap")
    return SecurityRecord(
        market=market,
        exchange=exchange,
        symbol=symbol,
        name=name,
        aliases=[],
        industry=str(info.get("industry") or ""),
        business_summary=str(info.get("longBusinessSummary") or ""),
        market_cap=float(market_cap) if isinstance(market_cap, (int, float)) else None,
        currency=str(info.get("currency") or currency),
        timezone=str(info.get("exchangeTimezoneName") or timezone),
        calendar=calendar,
        provider_data={"source": "yfinance", "yahoo_symbol": yahoo_symbol},
    )
