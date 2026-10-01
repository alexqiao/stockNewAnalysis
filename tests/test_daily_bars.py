from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select

from trade_news_analysis.config import Settings
from trade_news_analysis.daily_bar_models import DailyBar, DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security
from trade_news_analysis.services.daily_bars import (
    DailyBarService,
    get_daily_bars,
    refresh_due,
    target_trade_date,
)

NOW = datetime(2026, 9, 28, 22, tzinfo=UTC)


def frame(
    days: tuple[str, ...] = ("2026-09-25", "2026-09-28"),
    *, price: float = 100, adjusted: float | None = 95,
) -> pd.DataFrame:
    result = pd.DataFrame({
        "Open": price, "High": price + 2, "Low": price - 2, "Close": price + 1,
        "Adj Close": adjusted, "Volume": 1000, "Dividends": 0, "Stock Splits": 0,
    }, index=pd.to_datetime(list(days)).tz_localize("America/New_York"), dtype=float)
    result.attrs.update(
        currency="USD", timezone="America/New_York", volume_unit="shares",
        price_basis="split_adjusted",
    )
    return result


class Provider:
    def __init__(self, *results: pd.DataFrame | Exception):
        self.results = list(results)
        self.calls: list[tuple[str, str, date, date]] = []

    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        self.calls.append((market, symbol, start, end))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def apple_id(factory: SessionFactory) -> int:
    with factory() as session:
        return session.scalars(select(Security.id).where(Security.symbol == "AAPL")).one()


@pytest.mark.parametrize(("market", "now", "expected"), [
    ("US", "2026-03-06T21:59:00+00:00", "2026-03-05"),
    ("US", "2026-03-06T22:00:00+00:00", "2026-03-06"),
    ("US", "2026-03-09T20:59:00+00:00", "2026-03-06"),
    ("US", "2026-03-09T21:00:00+00:00", "2026-03-09"),
    ("US", "2026-11-27T18:59:00+00:00", "2026-11-25"),
    ("US", "2026-11-27T19:00:00+00:00", "2026-11-27"),
    ("HK", "2026-10-01T12:00:00+00:00", "2026-09-30"),
])
def test_target_uses_actual_exchange_close_and_one_hour_delay(
    market: str, now: str, expected: str,
) -> None:
    assert target_trade_date(market, datetime.fromisoformat(now)) == date.fromisoformat(expected)


def test_initial_ipo_history_normalizes_dates_and_deduplicates(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    data = frame(("2026-09-28", "2026-09-25", "2026-09-28", "2026-09-29"))
    data.loc[pd.Timestamp("2026-09-25", tz="America/New_York"), "Volume"] = None
    provider = Provider(data)
    service = DailyBarService(settings, provider)
    assert service.refresh_isolated(session_factory, [security_id], now=NOW)["updated"] == 1
    assert provider.calls[0][2:] == (date(2021, 9, 28), date(2026, 9, 29))
    with session_factory() as session:
        security = session.get(Security, security_id)
        assert security is not None
        payload = get_daily_bars(session, security, now=NOW)
        assert [item["date"] for item in payload["bars"]] == ["2026-09-25", "2026-09-28"]
        assert payload["bars"][0]["volume"] is None
        assert payload["bars"][0]["amount"] is None
        assert payload["bars"][0]["adj_open"] == pytest.approx(100 * 95 / 101)
        assert payload["coverage"]["start"] == date(2026, 9, 25)
        assert payload["stale"] is False
        assert payload["needs_refresh"] is False
    assert service.refresh_isolated(session_factory, [security_id], now=NOW)["skipped"] == 1
    assert len(provider.calls) == 1


@pytest.mark.parametrize(("column", "value"), [
    ("Open", -1), ("Close", float("inf")), ("High", 99), ("Volume", -2),
    ("Adj Close", 0), ("Stock Splits", -1),
])
def test_invalid_bars_preserve_previous_data_and_back_off(
    session_factory: SessionFactory, settings: Settings, column: str, value: float,
) -> None:
    security_id = apple_id(session_factory)
    invalid = frame()
    invalid.iloc[0, invalid.columns.get_loc(column)] = value
    provider = Provider(frame(), invalid)
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [security_id], now=NOW)
    result = service.refresh_isolated(
        session_factory, [security_id], now=NOW + timedelta(minutes=1), force=True
    )
    assert result["failed"] == 1
    assert isinstance(result["errors"][0], str)
    with session_factory() as session:
        state = session.get(DailyBarSyncState, security_id)
        assert state is not None
        assert state.status == "failed"
        assert state.next_retry_at == (NOW + timedelta(minutes=16)).replace(tzinfo=None)
        assert state.last_success_at == NOW.replace(tzinfo=None)
        assert session.scalar(select(func.count()).select_from(DailyBar)) == 2
        assert session.scalars(select(DailyBar.close)).first() == 101


def test_partial_fetch_retries_and_does_not_claim_latest_prices(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    provider = Provider(frame(("2026-09-25",)), frame(), frame())
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [security_id], now=NOW)
    with session_factory() as session:
        security = session.get(Security, security_id)
        assert security is not None
        payload = get_daily_bars(session, security, now=NOW)
        assert payload["sync_status"] == "partial"
        assert payload["stale"] is True
        assert payload["latest_trade_date"] == date(2026, 9, 25)
        assert payload["needs_refresh"] is False
        assert refresh_due(session, security, NOW + timedelta(hours=1)) is True
    assert service.refresh_isolated(
        session_factory, [security_id], now=NOW + timedelta(minutes=10), force=True
    )["skipped"] == 1
    assert service.refresh_isolated(
        session_factory, [security_id], now=NOW + timedelta(hours=1)
    )["updated"] == 1
    with session_factory() as session:
        state = session.get(DailyBarSyncState, security_id)
        assert state is not None and state.status == "success" and state.next_retry_at is None


def test_action_change_reloads_entire_history_atomically(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    old = frame(("2025-01-02", "2026-09-25", "2026-09-28"))
    changed = frame(adjusted=94)
    full = frame(("2025-01-02", "2026-09-25", "2026-09-28"), adjusted=94)
    provider = Provider(old, changed, full)
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [security_id], now=NOW)
    assert service.refresh_isolated(
        session_factory, [security_id], now=NOW + timedelta(minutes=1), force=True
    )["updated"] == 1
    assert len(provider.calls) == 3
    assert provider.calls[1][2] == date(2026, 9, 15)
    assert provider.calls[2][2] == date(2021, 9, 28)
    with session_factory() as session:
        assert set(session.scalars(select(DailyBar.adj_close))) == {94}


def test_incomplete_full_correction_keeps_entire_old_history(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    old = frame(("2025-01-02", "2026-09-25", "2026-09-28"))
    provider = Provider(old, frame(adjusted=94), frame(adjusted=94))
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [security_id], now=NOW)
    assert service.refresh_isolated(
        session_factory, [security_id], now=NOW + timedelta(minutes=1), force=True
    )["failed"] == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DailyBar)) == 3
        assert set(session.scalars(select(DailyBar.adj_close))) == {95}


def test_rate_limit_shared_cooldown_and_progressive_backoff(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    provider = Provider(*(RuntimeError("429 secret_provider_url") for _ in range(3)))
    service = DailyBarService(settings, provider)
    result = service.refresh_isolated(session_factory, now=NOW)
    assert result["failed"] == 1 and result["skipped"] == 1
    assert "secret_provider_url" not in str(result)
    with session_factory() as session:
        another = session.scalars(select(Security).where(Security.symbol == "MSFT")).one()
        assert refresh_due(session, another, NOW + timedelta(minutes=1)) is False
    for advance, expected in ((15, 75), (75, 435)):
        assert service.refresh_isolated(
            session_factory, [security_id], now=NOW + timedelta(minutes=advance)
        )["failed"] == 1
        with session_factory() as session:
            state = session.get(DailyBarSyncState, security_id)
            assert state is not None
            assert state.next_retry_at == (NOW + timedelta(minutes=expected)).replace(tzinfo=None)


def test_network_request_does_not_hold_database_transaction_and_same_stock_merges(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)

    class InspectingProvider(Provider):
        def history_range(
            self, market: str, symbol: str, start: date, end: date,
            provider_data: Mapping[str, Any] | None = None,
        ) -> pd.DataFrame:
            with session_factory() as session:
                security = session.get(Security, security_id)
                assert security is not None
                security.name = "A concurrently updated name"
                session.commit()
            assert service.refresh_isolated(
                session_factory, [security_id], now=NOW, force=True
            )["skipped"] == 1
            return super().history_range(market, symbol, start, end, provider_data)

    service = DailyBarService(settings, InspectingProvider(frame()))
    assert service.refresh_isolated(session_factory, [security_id], now=NOW)["updated"] == 1


def test_missing_adjustment_keeps_valid_basic_prices_and_weekly_refresh(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    security_id = apple_id(session_factory)
    provider = Provider(frame(adjusted=None), frame(adjusted=None))
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [security_id], now=NOW)
    with session_factory() as session:
        security = session.get(Security, security_id)
        assert security is not None
        payload = get_daily_bars(session, security, date(2026, 9, 28), now=NOW)
        assert payload["available_adjustments"] == ["split_adjusted"]
        assert payload["bars"][0]["adj_close"] is None
        assert len(payload["bars"]) == 1
        assert refresh_due(session, security, NOW + timedelta(days=7)) is True
    service.refresh_isolated(session_factory, [security_id], now=NOW + timedelta(days=7))
    assert provider.calls[-1][2] == date(2021, 10, 5)
