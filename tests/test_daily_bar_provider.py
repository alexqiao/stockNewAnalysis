from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import Mock

import pandas as pd
import pytest
import yfinance as yf

from trade_news_analysis.services.providers import YahooMarketDataProvider


@pytest.mark.parametrize(
    ("market", "symbol", "provider_data", "expected", "currency", "timezone"),
    [
        ("US", "AAPL", None, "AAPL", "USD", "America/New_York"),
        ("US", "BRK.B", None, "BRK-B", "USD", "US/Eastern"),
        ("US", "BRK.B", {"yahoo_symbol": "BRK-B"}, "BRK-B", "USD", "America/New_York"),
        ("HK", "00700.HK", None, "0700.HK", "HKD", "Asia/Hong_Kong"),
    ],
)
def test_daily_history_range_preserves_prices_actions_and_explicit_request_options(
    monkeypatch: pytest.MonkeyPatch, market: str, symbol: str,
    provider_data: dict[str, str] | None, expected: str, currency: str, timezone: str,
) -> None:
    source = pd.DataFrame({
        "Open": [100.0, 101.0], "High": [102.0, 103.0], "Low": [99.0, 100.0],
        "Close": [101.0, 102.0], "Adj Close": [90.9, 102.0], "Volume": [1000, 2000],
        "Dividends": [0.0, 1.0], "Stock Splits": [0.0, 2.0],
    }, index=pd.DatetimeIndex(["2026-09-14", "2026-09-15"], tz=timezone))
    ticker = Mock()
    ticker.history.return_value = source
    ticker.get_history_metadata.return_value = {
        "currency": currency, "exchangeTimezoneName": timezone,
    }
    factory = Mock(return_value=ticker)
    monkeypatch.setattr(yf, "Ticker", factory)

    result = YahooMarketDataProvider().history_range(
        market, symbol, date(2026, 9, 14), date(2026, 9, 16), provider_data,
    )

    factory.assert_called_once_with(expected)
    ticker.history.assert_called_once_with(
        start="2026-09-14", end="2026-09-16", interval="1d",
        auto_adjust=False, actions=True, repair=False, raise_errors=True,
        keepna=True, timeout=30,
    )
    ticker.get_history_metadata.assert_called_once_with()
    pd.testing.assert_frame_equal(result, source)
    assert result is not source
    assert source.attrs == {}
    assert result.attrs == {
        "source": "yfinance", "currency": currency, "timezone": timezone,
        "price_basis": "split_adjusted", "volume_unit": "shares",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"currency": "HKD", "exchangeTimezoneName": "America/New_York"},
        {"exchangeTimezoneName": "America/New_York"},
        {"currency": "USD", "exchangeTimezoneName": "Asia/Hong_Kong"},
        {"currency": "USD"},
    ],
)
def test_daily_history_rejects_missing_or_incompatible_source_metadata(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any],
) -> None:
    ticker = Mock()
    ticker.history.return_value = pd.DataFrame()
    ticker.get_history_metadata.return_value = metadata
    monkeypatch.setattr(yf, "Ticker", Mock(return_value=ticker))
    with pytest.raises(ValueError, match="币种|时区"):
        YahooMarketDataProvider().history_range(
            "US", "AAPL", date(2026, 9, 14), date(2026, 9, 16),
        )


def test_daily_history_unsupported_market_is_rejected_before_any_provider_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock()
    monkeypatch.setattr(yf, "Ticker", factory)
    with pytest.raises(ValueError, match="美股和港股"):
        YahooMarketDataProvider().history_range(
            "A", "600000.SH", date(2026, 9, 14), date(2026, 9, 16),
        )
    factory.assert_not_called()


def test_daily_history_propagates_fetch_failure_for_service_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticker = Mock()
    ticker.history.side_effect = RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(yf, "Ticker", Mock(return_value=ticker))
    with pytest.raises(RuntimeError, match="429"):
        YahooMarketDataProvider().history_range(
            "US", "AAPL", date(2026, 9, 14), date(2026, 9, 16),
        )
    ticker.get_history_metadata.assert_not_called()
