"""Capability-based multi-market data providers with explicit degradation."""

from __future__ import annotations

import importlib.util
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.request import Request, urlopen

import pandas as pd
from investormate import Stock

from ..config import Settings

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


class TushareClient:
    endpoint = "https://api.tushare.pro"

    def __init__(self, token: str, timeout: float):
        self.token = token
        self.timeout = timeout

    def query(
        self, api_name: str, params: dict[str, Any] | None = None, fields: str = ""
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
            raise RuntimeError(str(body.get("msg") or "Tushare request failed"))
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

    def fetch_securities(self) -> list[SecurityRecord]:
        result: list[SecurityRecord] = []
        specs = (
            ("A", "stock_basic", "ts_code,symbol,name,industry,exchange"),
            ("HK", "hk_basic", "ts_code,name,enname,list_status"),
            ("US", "us_basic", "ts_code,name,enname,classify,list_status"),
        )
        for market, api_name, fields in specs:
            for row in self.client.query(api_name, {"list_status": "L"}, fields):
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
        return result

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        del period
        api_name = {"A": "daily", "HK": "hk_daily", "US": "us_daily"}[market]
        rows = self.client.query(
            api_name, {"ts_code": symbol}, "trade_date,open,high,low,close"
        )
        return _price_frame(rows)

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        del period
        api_name, symbol = TUSHARE_BENCHMARKS[BENCHMARKS[market]]
        rows = self.client.query(
            api_name, {"ts_code": symbol}, "trade_date,open,high,low,close"
        )
        return _price_frame(rows)


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

    @staticmethod
    def _symbol(market: str, symbol: str) -> str:
        return yahoo_symbol(market, symbol)

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        result = Stock(self._symbol(market, symbol)).history(
            period=period, interval="1d", adjusted=False
        )
        return result.data if hasattr(result, "data") else result

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        symbol = {"A": "000300.SS", "HK": "^HSI", "US": "SPY"}[market]
        return self.history(market, symbol, period)


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


def _price_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame.index = pd.to_datetime(frame.pop("trade_date"))
    frame = frame.rename(columns={name: name.title() for name in ("open", "high", "low", "close")})
    return frame.sort_index()


def build_security_master_provider(settings: Settings) -> SecurityMasterProvider | None:
    if settings.tushare_configured:
        return TushareProvider(settings)
    if settings.akshare_enabled and importlib.util.find_spec("akshare") is not None:
        return AkShareSecurityMasterProvider()
    return None


def build_market_data_provider(settings: Settings) -> MarketDataProvider:
    return TushareProvider(settings) if settings.tushare_configured else YahooMarketDataProvider()


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
