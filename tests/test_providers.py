from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import SecretStr
from pytest import MonkeyPatch

from trade_news_analysis.config import Settings
from trade_news_analysis.services import providers


class StubMarketDataProvider:
    def __init__(
        self,
        name: str,
        history_results: dict[tuple[str, str], pd.DataFrame | Exception],
        benchmark_results: dict[str, pd.DataFrame | Exception],
    ):
        self.name = name
        self.history_results = history_results
        self.benchmark_results = benchmark_results
        self.history_calls: list[tuple[str, str, str]] = []
        self.benchmark_calls: list[tuple[str, str]] = []

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        self.history_calls.append((market, symbol, period))
        result = self.history_results[(market, symbol)]
        if isinstance(result, Exception):
            raise result
        return result

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        self.benchmark_calls.append((market, period))
        result = self.benchmark_results[market]
        if isinstance(result, Exception):
            raise result
        return result


def price_frame(value: float) -> pd.DataFrame:
    return pd.DataFrame({"Open": [value], "Close": [value]})


def test_lookup_security_record_normalizes_hk_symbol(monkeypatch: MonkeyPatch) -> None:
    requested: list[str] = []

    class FakeStock:
        def __init__(self, symbol: str):
            requested.append(symbol)
            self.info = {
                "longName": "Tencent Holdings Limited",
                "industry": "Internet Content & Information",
                "marketCap": 1_000_000,
                "currency": "HKD",
                "exchangeTimezoneName": "Asia/Hong_Kong",
            }

    monkeypatch.setattr(providers, "Stock", FakeStock)

    record = providers.lookup_security_record("HK", "700")

    assert record is not None
    assert requested == ["0700.HK"]
    assert record.symbol == "00700.HK"
    assert record.exchange == "HK"
    assert record.name == "Tencent Holdings Limited"


def test_lookup_security_record_rejects_company_name_without_network(
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_if_called(_symbol: str) -> None:
        raise AssertionError("Stock should not be called for a company name")

    monkeypatch.setattr(providers, "Stock", fail_if_called)

    assert providers.lookup_security_record("HK", "腾讯控股") is None


def test_yahoo_symbol_normalizes_all_supported_markets() -> None:
    assert providers.yahoo_symbol("US", "AAPL") == "AAPL"
    assert providers.yahoo_symbol("A", "600519.SH") == "600519.SS"
    assert providers.yahoo_symbol("A", "000001.SZ") == "000001.SZ"
    assert providers.yahoo_symbol("HK", "00700.HK") == "0700.HK"
    assert providers.yahoo_symbol(
        "HK", "00700.HK", {"yahoo_symbol": "CUSTOM.HK"}
    ) == "CUSTOM.HK"


def test_market_data_provider_falls_back_for_us_and_hk() -> None:
    primary = StubMarketDataProvider(
        "primary",
        {
            ("US", "AAPL"): RuntimeError("not authorized"),
            ("HK", "00700.HK"): pd.DataFrame(),
        },
        {"US": RuntimeError("not authorized")},
    )
    fallback = StubMarketDataProvider(
        "fallback",
        {
            ("US", "AAPL"): price_frame(100),
            ("US", "MSFT"): price_frame(200),
            ("HK", "00700.HK"): price_frame(300),
        },
        {"US": price_frame(500)},
    )
    provider = providers.FallbackMarketDataProvider(
        primary,
        fallback,
        fallback_markets=frozenset({"HK", "US"}),
    )

    assert provider.history("US", "AAPL").equals(price_frame(100))
    assert provider.history("US", "MSFT").equals(price_frame(200))
    assert provider.history("HK", "00700.HK").equals(price_frame(300))
    assert provider.benchmark_history("US").equals(price_frame(500))
    assert primary.history_calls == [
        ("US", "AAPL", "6mo"),
        ("HK", "00700.HK", "6mo"),
    ]
    assert primary.benchmark_calls == []
    assert fallback.history_calls == [
        ("US", "AAPL", "6mo"),
        ("US", "MSFT", "6mo"),
        ("HK", "00700.HK", "6mo"),
    ]
    assert fallback.benchmark_calls == [("US", "6mo")]


def test_market_data_provider_does_not_fall_back_for_a_shares() -> None:
    primary = StubMarketDataProvider(
        "primary",
        {("A", "600519.SH"): pd.DataFrame()},
        {"A": pd.DataFrame()},
    )
    fallback = StubMarketDataProvider("fallback", {}, {})
    provider = providers.FallbackMarketDataProvider(
        primary,
        fallback,
        fallback_markets=frozenset({"HK", "US"}),
    )

    assert provider.history("A", "600519.SH").empty
    assert provider.benchmark_history("A").empty
    assert fallback.history_calls == []
    assert fallback.benchmark_calls == []


def test_build_market_data_provider_combines_tushare_and_yahoo() -> None:
    provider = providers.build_market_data_provider(
        Settings(tushare_token=SecretStr("configured"))
    )

    assert isinstance(provider, providers.FallbackMarketDataProvider)
    assert isinstance(provider.primary, providers.TushareProvider)
    assert isinstance(provider.fallback, providers.YahooMarketDataProvider)


def test_tushare_security_master_keeps_markets_that_are_authorized() -> None:
    class FakeClient:
        def query(
            self, api_name: str, _params: dict[str, Any], _fields: str
        ) -> list[dict[str, Any]]:
            if api_name == "hk_basic":
                raise RuntimeError("permission denied")
            if api_name == "stock_basic":
                return [
                    {
                        "ts_code": "000001.SZ",
                        "name": "平安银行",
                        "industry": "银行",
                    }
                ]
            return [{"ts_code": "AAPL", "name": "Apple", "classify": "Technology"}]

    provider = providers.TushareProvider(
        Settings(tushare_token=SecretStr("configured"))
    )
    provider.client = FakeClient()  # type: ignore[assignment]

    records = provider.fetch_securities()

    assert {item.market for item in records} == {"A", "US"}
    assert provider.market_errors == {"HK": "RuntimeError: permission denied"}


def test_fundamental_provider_uses_latest_complete_year_and_fallbacks(
    monkeypatch: MonkeyPatch,
) -> None:
    requested: list[str] = []

    class FakeStock:
        def __init__(self, symbol: str):
            requested.append(symbol)
            self.info: dict[str, Any] = {}
            self.price = 42.5
            self.market_cap = 10_000
            self.income_statement = {
                "2025-12-31": {"Total Revenue": 1_500},
                "2024-12-31": {
                    "Operating Revenue": 1_200,
                    "Net Income": 120,
                    "Diluted Average Shares": 60,
                },
            }

    monkeypatch.setattr(providers, "Stock", FakeStock)

    snapshot = providers.InvestorMateFundamentalProvider().fetch(
        "HK", "00700.HK"
    )

    assert requested == ["0700.HK"]
    assert snapshot.fiscal_year == 2024
    assert snapshot.price == 42.5
    assert snapshot.market_cap == 10_000
    assert snapshot.revenue == 1_200
    assert snapshot.net_income == 120
    assert snapshot.shares_outstanding == 60
