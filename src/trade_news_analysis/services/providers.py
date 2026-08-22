"""Capability-based multi-market data providers with explicit degradation."""

from __future__ import annotations

import importlib.util
import json
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


class SecurityMasterProvider(Protocol):
    name: str
    markets: tuple[str, ...]

    def fetch_securities(self) -> list[SecurityRecord]: ...


class MarketDataProvider(Protocol):
    name: str

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame: ...

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame: ...


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


class YahooMarketDataProvider:
    name = "yfinance"

    @staticmethod
    def _symbol(market: str, symbol: str) -> str:
        if market == "A" and symbol.endswith(".SH"):
            return f"{symbol.removesuffix('.SH')}.SS"
        return symbol

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        result = Stock(self._symbol(market, symbol)).history(
            period=period, interval="1d", adjusted=False
        )
        return result.data if hasattr(result, "data") else result

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        symbol = {"A": "000300.SS", "HK": "^HSI", "US": "SPY"}[market]
        return self.history(market, symbol, period)


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
