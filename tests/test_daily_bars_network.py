"""Explicit opt-in smoke checks; all quotes are saved only to pytest's temporary DB."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security
from trade_news_analysis.services.daily_bars import (
    DailyBarService,
    get_daily_bars,
    target_trade_date,
)

pytestmark = pytest.mark.network


@pytest.mark.parametrize("market,symbol,currency,timezone", [
    ("US", "AAPL", "USD", "America/New_York"),
    ("HK", "00700.HK", "HKD", "Asia/Hong_Kong"),
])
def test_live_yahoo_five_year_history_round_trip(
    settings: Settings, session_factory: SessionFactory,
    market: str, symbol: str, currency: str, timezone: str,
) -> None:
    with session_factory() as session:
        security = session.scalar(select(Security).where(
            Security.market == market, Security.symbol == symbol,
        ))
        if security is None:
            security = Security(
                market=market, symbol=symbol, name=symbol, currency=currency,
                timezone=timezone, exchange="HKEX", calendar="XHKG",
            )
            session.add(security)
            session.commit()
        identity = security.id
    now = datetime.now(UTC)
    service = DailyBarService(settings)
    result = service.refresh_isolated(session_factory, [identity], now=now, force=True)
    assert result["failed"] == 0, result["errors"]
    assert result["updated"] == 1
    with session_factory() as session:
        security = session.get(Security, identity)
        assert security is not None
        payload = get_daily_bars(session, security, now=now)
    bars = payload["bars"]
    assert len(bars) > 1000
    assert payload["currency"] == currency
    assert payload["source"] == "yfinance"
    assert "total_return_adjusted" in payload["available_adjustments"]
    assert len({bar["date"] for bar in bars}) == len(bars)
    assert bars[-1]["date"] <= target_trade_date(market, now).isoformat()
    assert all(bar["low"] <= min(bar["open"], bar["close"]) for bar in bars)
    assert all(bar["high"] >= max(bar["open"], bar["close"]) for bar in bars)
    print(f"{market} {symbol}: {len(bars)} bars, {bars[0]['date']} .. {bars[-1]['date']}")
    unchanged = service.refresh_isolated(session_factory, [identity], now=now)
    assert unchanged["skipped"] == 1
