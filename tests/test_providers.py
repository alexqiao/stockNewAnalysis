from __future__ import annotations

from typing import Any

from pytest import MonkeyPatch

from trade_news_analysis.services import providers


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
