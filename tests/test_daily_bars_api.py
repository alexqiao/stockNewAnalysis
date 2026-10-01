from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import select

from trade_news_analysis.daily_bar_models import DailyBar, DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security, Watchlist
from trade_news_analysis.services.coordinator import PipelineBusyError
from trade_news_analysis.services.ibkr import BrokerError

from . import test_decisions_api as api_fixtures

decisions_client = api_fixtures.decisions_client


def security_id(session_factory: SessionFactory, symbol: str = "AAPL") -> int:
    with session_factory() as session:
        result = session.scalar(select(Security.id).where(Security.symbol == symbol))
        assert result is not None
        return result


def test_empty_daily_bars_get_is_read_only_and_available_without_watchlist(
    decisions_client: TestClient, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    submit = Mock()
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    identity = security_id(session_factory, "GLD")
    response = decisions_client.get(f"/api/v1/securities/{identity}/daily-bars")
    assert response.status_code == 200
    data = response.json()
    assert data["security_id"] == identity
    assert data["bars"] == []
    assert data["currency"] == "USD"
    assert data["volume_unit"] == "shares"
    assert data["latest_trade_date"] is None
    assert data["needs_refresh"] is True
    submit.assert_not_called()
    with session_factory() as session:
        assert session.scalar(select(Watchlist).where(Watchlist.security_id == identity)) is None


def test_cached_daily_bars_filters_inclusive_dates_and_preserves_failure_metadata(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    identity = security_id(session_factory)
    first, last = date(2026, 5, 11), date(2026, 5, 12)
    success = datetime(2026, 5, 12, 22, tzinfo=UTC)
    with session_factory() as session:
        session.add_all([
            DailyBar(
                security_id=identity, trade_date=day, open=10, high=12, low=9, close=11,
                adj_close=5.5, volume=volume,
            ) for day, volume in [(last, None), (first, 1250)]
        ])
        session.add(DailyBarSyncState(
            security_id=identity, currency="USD", timezone="America/New_York",
            coverage_start=first, coverage_end=last, last_success_at=success,
            status="failed", error="行情暂不可用", failure_count=1,
        ))
        session.commit()
    url = f"/api/v1/securities/{identity}/daily-bars"
    response = decisions_client.get(url, params={"start": str(first), "end": str(last)})
    assert response.status_code == 200
    result = response.json()
    assert [bar["date"] for bar in result["bars"]] == [str(first), str(last)]
    assert result["bars"][0]["adj_open"] == 5
    assert result["bars"][1]["volume"] is None
    assert result["stale"] is True
    assert result["error"] == "行情暂不可用"
    assert result["latest_trade_date"] == str(last)
    assert result["last_success_at"] == "2026-05-12T22:00:00Z"
    one_day = decisions_client.get(url, params={"start": str(last), "end": str(last)}).json()
    assert [bar["date"] for bar in one_day["bars"]] == [str(last)]
    assert one_day["coverage"] == {"start": str(first), "end": str(last)}


@pytest.mark.parametrize("query", [
    "?start=2026-06-02&end=2026-06-01", "?start=not-a-date", "?end=2026-02-30",
])
def test_daily_bars_reject_invalid_dates(
    decisions_client: TestClient, session_factory: SessionFactory, query: str,
) -> None:
    identity = security_id(session_factory)
    response = decisions_client.get(f"/api/v1/securities/{identity}/daily-bars{query}")
    assert response.status_code == 422


@pytest.mark.parametrize("method,suffix", [("get", ""), ("post", "/refresh")])
def test_daily_bars_reject_unknown_and_unsupported_securities(
    decisions_client: TestClient, session_factory: SessionFactory, method: str, suffix: str,
) -> None:
    request = getattr(decisions_client, method)
    assert request(f"/api/v1/securities/999999/daily-bars{suffix}").status_code == 404
    with session_factory() as session:
        row = Security(
            market="A", symbol="600000.SH", name="示例", currency="CNY",
            timezone="Asia/Shanghai", exchange="SSE", calendar="XSHG",
        )
        session.add(row)
        session.commit()
        identity = row.id
    response = request(f"/api/v1/securities/{identity}/daily-bars{suffix}")
    assert response.status_code == 422
    assert "美股和港股" in response.json()["detail"]


@pytest.mark.parametrize("force", [False, True])
def test_manual_refresh_works_with_automatic_updates_disabled(
    decisions_client: TestClient, session_factory: SessionFactory,
    monkeypatch: MonkeyPatch, force: bool,
) -> None:
    identity = security_id(session_factory)
    submit = Mock(return_value=81)
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    assert decisions_client.app.state.settings.daily_bars_enabled is False
    response = decisions_client.post(
        f"/api/v1/securities/{identity}/daily-bars/refresh", params={"force": force},
    )
    assert response.status_code == 202
    assert response.json() == {"run_id": 81, "status": "queued"}
    submit.assert_called_once_with([identity], force=force)


def test_inactive_security_keeps_read_access_but_rejects_empty_refresh_jobs(
    decisions_client: TestClient, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    identity = security_id(session_factory)
    with session_factory() as session:
        row = session.get(Security, identity)
        assert row is not None
        row.active = False
        session.commit()
    submit = Mock()
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    url = f"/api/v1/securities/{identity}/daily-bars"
    assert decisions_client.get(url).status_code == 200
    assert decisions_client.post(url + "/refresh").status_code == 422
    submit.assert_not_called()


def test_refresh_reports_coordinator_shutdown(
    decisions_client: TestClient, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    identity = security_id(session_factory)
    monkeypatch.setattr(
        decisions_client.app.state.coordinator, "submit_daily_bars",
        Mock(side_effect=PipelineBusyError("服务正在关闭")),
    )
    response = decisions_client.post(f"/api/v1/securities/{identity}/daily-bars/refresh")
    assert response.status_code == 409


def test_watchlist_only_queues_new_or_reactivated_symbols_after_commit(
    decisions_client: TestClient, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    apple, gold = security_id(session_factory), security_id(session_factory, "GLD")
    decisions_client.app.state.settings.daily_bars_enabled = True

    def committed(ids: list[int]) -> int:
        with session_factory() as session:
            active = set(session.scalars(
                select(Watchlist.security_id).where(Watchlist.active.is_(True))
            ))
        assert set(ids) <= active
        return 1

    submit = Mock(side_effect=committed)
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    items = [{"security_id": apple}, {"security_id": gold}]
    assert decisions_client.put("/api/v1/watchlist", json={"items": items}).status_code == 200
    submit.assert_called_once_with([gold])
    submit.reset_mock()
    assert decisions_client.put("/api/v1/watchlist", json={"items": items}).status_code == 200
    submit.assert_not_called()
    items[1]["active"] = False
    assert decisions_client.put("/api/v1/watchlist", json={"items": items}).status_code == 200
    submit.assert_not_called()
    items[1]["active"] = True
    assert decisions_client.put("/api/v1/watchlist", json={"items": items}).status_code == 200
    submit.assert_called_once_with([gold])


def test_queue_failure_does_not_undo_saved_watchlist(
    decisions_client: TestClient, session_factory: SessionFactory, monkeypatch: MonkeyPatch,
) -> None:
    identity = security_id(session_factory, "GLD")
    decisions_client.app.state.settings.daily_bars_enabled = True
    monkeypatch.setattr(
        decisions_client.app.state.coordinator, "submit_daily_bars",
        Mock(side_effect=PipelineBusyError("服务正在关闭")),
    )
    response = decisions_client.put(
        "/api/v1/watchlist", json={"items": [{"security_id": identity}]},
    )
    assert response.status_code == 200
    with session_factory() as session:
        assert session.scalar(select(Watchlist.security_id)) == identity


@pytest.mark.parametrize("enabled", [False, True])
def test_holding_sync_queues_supported_positions_only_after_success(
    decisions_client: TestClient, session_factory: SessionFactory,
    monkeypatch: MonkeyPatch, enabled: bool,
) -> None:
    identity = security_id(session_factory)
    decisions_client.app.state.settings.daily_bars_enabled = enabled
    result = {"positions": [
        {"security_id": identity, "quantity": 5},
        {"security_id": identity, "quantity": -1},
        {"security_id": security_id(session_factory, "MSFT"), "quantity": 0},
        {"security_id": None, "quantity": 2},
    ]}
    monkeypatch.setattr(decisions_client.app.state.holdings, "sync", Mock(return_value=result))
    submit = Mock(return_value=3)
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    response = decisions_client.post(
        "/api/v1/holdings/ibkr/sync", json={"account_key": "a" * 64},
    )
    assert response.status_code == 200
    assert response.json() == result
    if enabled:
        submit.assert_called_once_with([identity])
    else:
        submit.assert_not_called()


def test_failed_holding_sync_never_queues_daily_bars(
    decisions_client: TestClient, monkeypatch: MonkeyPatch,
) -> None:
    decisions_client.app.state.settings.daily_bars_enabled = True
    monkeypatch.setattr(
        decisions_client.app.state.holdings, "sync", Mock(side_effect=BrokerError("未连接")),
    )
    submit = Mock()
    monkeypatch.setattr(decisions_client.app.state.coordinator, "submit_daily_bars", submit)
    response = decisions_client.post(
        "/api/v1/holdings/ibkr/sync", json={"account_key": "a" * 64},
    )
    assert response.status_code == 503
    submit.assert_not_called()
