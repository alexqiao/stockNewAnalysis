from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from pydantic import SecretStr

from trade_news_analysis.config import Settings
from trade_news_analysis.services.providers import FallbackMarketDataProvider, TushareProvider

from .test_history_repository import RangeProvider
from .test_market_research import market_frame


def test_tushare_uses_inclusive_dates_for_prices_adjustment_and_benchmark(
    settings: Settings,
) -> None:
    calls = []

    class Client:
        def query(self, name: str, params: dict[str, Any], fields: str) -> list[dict[str, Any]]:
            calls.append((name, params))
            if name == "adj_factor":
                return [{"trade_date": "20240102", "adj_factor": 1}]
            return [{"trade_date": "20240102", "open": 100, "high": 101,
                     "low": 99, "close": 100, "vol": 10, "amount": 100}]

    settings.tushare_token = SecretStr("fixture")
    provider = TushareProvider(settings)
    provider.client = Client()  # type: ignore[assignment]
    provider.history_range("A", "600000.SH", date(2024, 1, 2), date(2024, 1, 9))
    provider.benchmark_history_range("A", date(2024, 1, 2), date(2024, 1, 9))
    assert [name for name, _params in calls] == ["daily", "adj_factor", "index_daily"]
    assert all(params["start_date"] == "20240102" and params["end_date"] == "20240108"
               for _name, params in calls)


def test_range_fallback_replaces_whole_series_and_keeps_requested_dates() -> None:
    raw = market_frame(start="2026-09-14", end="2026-09-16")
    raw.attrs["adjustment_status"] = "unavailable"
    raw["Amount"] = 999
    adjusted = market_frame(price=200, start="2026-09-14", end="2026-09-16").drop(columns="Amount")
    adjusted.attrs.update(price_basis="split_adjusted", timezone="America/New_York")

    class Primary(RangeProvider):
        name = "primary"

        def history_range(self, *args, **kwargs) -> pd.DataFrame:
            self.calls.append((args[2], args[3]))
            return raw.copy()

    primary, fallback = Primary(raw), RangeProvider(adjusted)
    provider = FallbackMarketDataProvider(primary, fallback, frozenset({"US"}),
                                          prefer_verified_adjustment=True)
    result = provider.history_range("US", "AAPL", date(2026, 9, 15), date(2026, 9, 17))
    assert primary.calls == fallback.calls == [(date(2026, 9, 15), date(2026, 9, 17))]
    assert result["Close"].tolist() == [200, 200]
    assert "Amount" not in result
    assert result.attrs["adjustment_status"] == "verified"
    assert result.attrs["source_version"]
