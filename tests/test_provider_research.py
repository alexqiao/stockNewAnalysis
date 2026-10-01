from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import SecretStr
from pytest import MonkeyPatch

from trade_news_analysis.services import providers

from .conftest import IsolatedSettings


def test_tushare_preserves_raw_prices_and_normalizes_a_share_units() -> None:
    class Client:
        def query(self, name: str, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
            if name == "adj_factor":
                return [
                    {"trade_date": "20260914", "adj_factor": 1},
                    {"trade_date": "20260915", "adj_factor": 2},
                ]
            assert "vol,amount" in fields
            return [
                {
                    "trade_date": "20260914",
                    "open": 20,
                    "high": 20,
                    "low": 20,
                    "close": 20,
                    "vol": 10,
                    "amount": 20,
                },
                {
                    "trade_date": "20260915",
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "vol": 20,
                    "amount": 20,
                },
            ]

    provider = providers.TushareProvider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    provider.client = Client()  # type: ignore[assignment]
    frame = provider.history("A", "000001.SZ")
    assert frame["Close"].tolist() == [20, 10]
    assert frame["Adj Close"].tolist() == [10, 10]
    assert frame["Volume"].tolist() == [1000, 2000]
    assert frame["Amount"].tolist() == [20_000, 20_000]
    assert frame.attrs["analysis_price_basis"] == "total_return_adjusted"


def test_yahoo_fetches_explicit_raw_and_adjusted_prices(monkeypatch: MonkeyPatch) -> None:
    calls = []

    class Stock:
        def __init__(self, symbol: str):
            pass

        def history(self, *, period: str, interval: str, adjusted: bool) -> pd.DataFrame:
            calls.append(adjusted)
            price = 50 if adjusted else 100
            return pd.DataFrame(
                {
                    "Open": [price],
                    "High": [price],
                    "Low": [price],
                    "Close": [price],
                    "Volume": [1000],
                },
                index=pd.to_datetime(["2026-09-15"]),
            )

    monkeypatch.setattr(providers, "Stock", Stock)
    frame = providers.YahooMarketDataProvider().history("US", "AAPL")
    assert calls == [False, True]
    assert frame.iloc[0]["Close"] == 100
    assert frame.iloc[0]["Adj Close"] == 50
    assert frame.attrs["adjustment_status"] == "verified"


def test_missing_adjustment_permission_leaves_basis_unknown() -> None:
    class Client:
        def query(self, name: str, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
            if name == "adj_factor":
                raise RuntimeError("not authorized")
            return [
                {
                    "trade_date": "20260915",
                    "open": 10,
                    "high": 10,
                    "low": 10,
                    "close": 10,
                    "vol": 1,
                    "amount": 1,
                }
            ]

    provider = providers.TushareProvider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    provider.client = Client()  # type: ignore[assignment]
    frame = provider.history("A", "000001.SZ")
    assert frame.attrs["adjustment_status"] == "unavailable"
    assert frame.attrs["analysis_price_basis"] == "unknown"


def hk_frame(price: float, *, verified: bool) -> pd.DataFrame:
    frame = pd.DataFrame(
        {"Open": [price], "High": [price], "Low": [price], "Close": [price], "Volume": [1000]},
        index=pd.to_datetime(["2026-09-16"]),
    )
    if verified:
        for field in ("Open", "High", "Low", "Close"):
            frame[f"Adj {field}"] = frame[field] * 0.9
    frame.attrs.update(
        source="yfinance" if verified else "tushare",
        price_basis="raw", currency="HKD", volume_unit="shares",
        analysis_price_basis="total_return_adjusted" if verified else "unknown",
        adjustment_status="verified" if verified else "unavailable",
    )
    return frame


def test_default_provider_replaces_entire_unadjusted_hk_history_with_yahoo(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = []
    primary = hk_frame(19, verified=False)
    primary["Amount"] = 19000
    monkeypatch.setattr(providers.TushareProvider, "history", lambda *_args: primary)

    class Stock:
        def __init__(self, symbol: str):
            assert symbol == "1810.HK"

        def history(self, *, period: str, interval: str, adjusted: bool) -> pd.DataFrame:
            calls.append((period, adjusted))
            return hk_frame(90 if adjusted else 100, verified=False)

    monkeypatch.setattr(providers, "Stock", Stock)
    provider = providers.build_market_data_provider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    frame = provider.history("HK", "01810.HK", "1y")
    assert calls == [("1y", False), ("1y", True)]
    assert frame["Close"].tolist() == [100]
    assert frame["Adj Close"].tolist() == [90]
    assert "Amount" not in frame
    assert frame.attrs["source"] == "yfinance"
    assert frame.attrs["adjustment_fallback"] == {
        "from": "tushare", "to": "yfinance",
        "reason": "primary_adjustment_unverified_or_incomplete", "status": "selected",
    }
    assert "adjustment_fallback" not in primary.attrs


def test_adjustment_fallback_failure_preserves_primary_raw_and_blocked_state(
    monkeypatch: MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from trade_news_analysis.models import Security
    from trade_news_analysis.services.market_research import analyze_market_frame

    primary = hk_frame(19, verified=False)
    monkeypatch.setattr(providers.TushareProvider, "history", lambda *_args: primary)

    def unavailable(*_args: Any) -> pd.DataFrame:
        raise RuntimeError("fixture_sensitive_text")

    monkeypatch.setattr(providers.YahooMarketDataProvider, "history", unavailable)
    provider = providers.build_market_data_provider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    frame = provider.history("HK", "01810.HK")
    assert frame["Close"].tolist() == [19]
    assert frame.attrs["source"] == "tushare"
    assert frame.attrs["adjustment_fallback"]["failure"] == "RuntimeError"
    assert "fixture_sensitive_text" not in str(frame.attrs)
    security = Security(
        id=999, market="HK", symbol="01810.HK", name="Fixture",
        currency="HKD", timezone="Asia/Hong_Kong",
    )
    result = analyze_market_frame(security, frame, now=datetime(2026, 9, 16, 22, tzinfo=UTC))
    assert result["status"] == "blocked"
    assert result["quote"]["source"] == "tushare"
    assert result["horizons"]["1"]["absolute_return_pct"] is None


def test_adjustment_fallback_rejects_partial_or_wrong_currency_data(
    monkeypatch: MonkeyPatch,
) -> None:
    primary = hk_frame(19, verified=False)
    monkeypatch.setattr(providers.TushareProvider, "history", lambda *_args: primary)
    provider = providers.build_market_data_provider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    for invalid in (
        "missing_column", "unknown_basis", "wrong_currency", "invalid_ohlc", "missing_latest_close"
    ):
        candidate = hk_frame(100, verified=True)
        if invalid == "missing_column":
            candidate = candidate.drop(columns="Adj Open")
        elif invalid == "unknown_basis":
            candidate.attrs["analysis_price_basis"] = "unknown"
        elif invalid == "wrong_currency":
            candidate.attrs["currency"] = "USD"
        elif invalid == "missing_latest_close":
            candidate["Close"] = float("nan")
            for field in ("Open", "High", "Low", "Close"):
                candidate[f"Adj {field}"] = float("nan")
        else:
            candidate["Adj Low"] = 999
        monkeypatch.setattr(
            providers.YahooMarketDataProvider, "history", lambda *_args, result=candidate: result
        )
        frame = provider.history("HK", "01810.HK")
        assert frame["Close"].tolist() == [19]
        assert frame.attrs["adjustment_fallback"]["status"] == "unavailable"


def test_verified_primary_does_not_fetch_adjustment_fallback(monkeypatch: MonkeyPatch) -> None:
    primary = hk_frame(19, verified=True)
    primary.attrs["source"] = "tushare"
    monkeypatch.setattr(providers.TushareProvider, "history", lambda *_args: primary)

    def unexpected(*_args: Any) -> pd.DataFrame:
        raise AssertionError("verified primary should be used")

    monkeypatch.setattr(providers.YahooMarketDataProvider, "history", unexpected)
    provider = providers.build_market_data_provider(
        IsolatedSettings(tushare_token=SecretStr("fixture"))
    )
    frame = provider.history("HK", "01810.HK")
    assert frame is primary
    assert "adjustment_fallback" not in frame.attrs
