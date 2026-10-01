from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from sqlalchemy import func, select

from trade_news_analysis.daily_bar_models import DailyBar, DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.holding_models import HoldingPosition, HoldingSnapshot, HoldingSyncRun
from trade_news_analysis.models import Security, Watchlist
from trade_news_analysis.risk_models import SecurityRiskProfile
from trade_news_analysis.services import daily_bars

from . import test_decisions_api as api_fixtures
from .test_daily_bars_api import security_id

decisions_client = api_fixtures.decisions_client


@pytest.fixture
def reference_data(session_factory: SessionFactory, monkeypatch: MonkeyPatch) -> int:
    identity = security_id(session_factory)
    last = date(2026, 5, 12)
    days = [last - timedelta(days=i) for i in range(240) if (
        last - timedelta(days=i)
    ).weekday() < 5]
    monkeypatch.setattr(daily_bars, "target_trade_date", lambda *_args: last)
    with session_factory() as session:
        session.add_all([
            DailyBar(
                security_id=identity, trade_date=day, open=100, high=101, low=99,
                close=100, adj_close=50, volume=1000,
            ) for day in days
        ])
        session.add(DailyBarSyncState(
            security_id=identity, currency="USD", timezone="America/New_York",
            coverage_start=min(days), coverage_end=last, checked_through=last,
            status="success", last_success_at=datetime.now(UTC),
            last_full_refresh_at=datetime.now(UTC),
        ))
        watch = session.scalar(select(Watchlist).where(Watchlist.security_id == identity))
        assert watch is not None
        watch.holding_status = "flat"
        session.commit()
    return identity


def test_reference_uses_full_cache_and_never_fetches_or_writes(
    decisions_client: TestClient, session_factory: SessionFactory,
    reference_data: int, monkeypatch: MonkeyPatch,
) -> None:
    submit = Mock()
    monkeypatch.setattr(cast(FastAPI, decisions_client.app).state.coordinator,
                        "submit_daily_bars", submit)
    url = f"/api/v1/securities/{reference_data}/bollinger-reference"
    with session_factory() as session:
        count = session.scalar(select(func.count()).select_from(DailyBar))
    response = decisions_client.get(url)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"
    assert data["price_basis"] == "total_return_adjusted"
    assert data["levels"]["middle"] == 50
    assert data["parameters"]["squeeze_lookback"] == 120
    assert data["guardrails"]["blocked"] is False
    assert data["execution_ready"] is False
    assert data["add_on"]["enabled"] is False
    changed = decisions_client.get(url, params={
        "slope_min_pct": 0.75, "adjustment": "split_adjusted",
    }).json()
    assert changed["parameters"]["slope_min_pct"] == 0.75
    assert changed["levels"]["middle"] == 100
    submit.assert_not_called()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DailyBar)) == count
        assert session.scalar(select(func.count()).select_from(SecurityRiskProfile)) == 0


@pytest.mark.parametrize("query", [
    {"adjustment": "raw"}, {"squeeze_lookback": 1}, {"max_stop_pct": -1},
    {"slope_lookback": 1.5}, {"slope_min_pct": "nan"}, {"expansion_ratio": "inf"},
])
def test_reference_validates_query(
    decisions_client: TestClient, reference_data: int, query: dict[str, Any],
) -> None:
    assert decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference", params=query,
    ).status_code == 422


def test_empty_unknown_and_unsupported_references(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    identity = security_id(session_factory, "GLD")
    data = decisions_client.get(f"/api/v1/securities/{identity}/bollinger-reference").json()
    assert data["status"] == "insufficient"
    assert data["position"]["status"] == "unknown"
    assert data["action"]["code"] == "blocked"
    assert decisions_client.get("/api/v1/securities/999999/bollinger-reference").status_code == 404
    with session_factory() as session:
        security = Security(
            market="A", symbol="600000.SH", name="示例", currency="CNY",
            timezone="Asia/Shanghai", exchange="SSE", calendar="XSHG",
        )
        session.add(security)
        session.commit()
        identity = security.id
    assert decisions_client.get(
        f"/api/v1/securities/{identity}/bollinger-reference",
    ).status_code == 422


def record_long(session_factory: SessionFactory, identity: int, **changes: Any) -> None:
    with session_factory() as session:
        watch = session.scalar(select(Watchlist).where(Watchlist.security_id == identity))
        assert watch is not None
        watch.holding_status = "long"
        values = {"current_quantity": 10, "current_weight": 0.01, "average_cost": 80}
        session.add(SecurityRiskProfile(security_id=identity, **(values | changes)))
        session.commit()


def test_stops_are_explicit_and_scaled_with_costs_including_fallback(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(session_factory, reference_data, stop_price=105)
    url = f"/api/v1/securities/{reference_data}/bollinger-reference"
    adjusted = decisions_client.get(url).json()
    assert adjusted["levels"]["current_stop"] == 52.5
    assert adjusted["position"]["profit_pct"] == 25
    assert adjusted["action"]["code"] == "stop_triggered"
    split = decisions_client.get(url, params={"adjustment": "split_adjusted"}).json()
    assert split["levels"]["current_stop"] == 105
    assert split["position"]["profit_pct"] == 25
    with session_factory() as session:
        bar = session.scalar(select(DailyBar).where(DailyBar.security_id == reference_data))
        assert bar is not None
        bar.adj_close = None
        session.commit()
    fallback = decisions_client.get(url).json()
    assert fallback["price_basis"] == "split_adjusted"
    assert fallback["levels"]["current_stop"] == 105
    assert fallback["position"]["profit_pct"] == 25


def test_default_stop_is_not_a_recorded_stop_and_old_data_blocks_advice(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(session_factory, reference_data)
    url = f"/api/v1/securities/{reference_data}/bollinger-reference"
    data = decisions_client.get(url).json()
    assert data["levels"]["current_stop"] is None
    with session_factory() as session:
        state = session.get(DailyBarSyncState, reference_data)
        assert state is not None
        state.status = "failed"
        session.commit()
    stale = decisions_client.get(url).json()
    assert stale["data"]["stale"] is True
    assert stale["action"]["code"] == "blocked"
    assert stale["levels"]["middle"] == 50


@pytest.mark.parametrize("future_snapshot", [False, True])
def test_broker_facts_override_manual_with_cached_or_invalid_timestamp(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
    future_snapshot: bool,
) -> None:
    record_long(session_factory, reference_data, stop_price=105)
    with session_factory() as session:
        run = HoldingSyncRun(account_key="fixture", status="success")
        session.add(run)
        session.flush()
        snapshot = HoldingSnapshot(
            run_id=run.id, account_key="fixture", account_label="fixture",
            captured_at=datetime.now(UTC) + timedelta(days=1 if future_snapshot else -2),
            currency="USD",
        )
        session.add(snapshot)
        session.flush()
        session.add(HoldingPosition(
            snapshot_id=snapshot.id, security_id=reference_data, con_id=1, symbol="AAPL",
            security_type="STK", exchange="NASDAQ", currency="USD", quantity=12,
            average_cost=90, weight=0.012,
        ))
        session.commit()
    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["holding_source"] == "券商同步"
    assert data["position"]["reliable"] is not future_snapshot
    if future_snapshot:
        assert data["action"]["code"] == "blocked"
        assert any("快照时间异常" in reason for reason in data["guardrails"]["reasons"])
    else:
        assert data["action"]["code"] == "stop_triggered"
        assert data["holdings_source"]["using_cached"] is True
        assert "快照超过 24 小时" in data["holdings_source"]["note"]


def test_known_risk_limit_blocks_current_advice_but_preserves_shape(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(session_factory, reference_data, current_weight=0.3, max_weight=0.2)
    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["guardrails"]["blocked"] is True
    assert data["action"]["label"] == "先复核持仓风险"
    assert data["technical_action"]["code"] != "blocked"


def test_split_stop_basis_cannot_be_confirmed_by_an_unrelated_profile_edit(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(session_factory, reference_data, stop_price=105,
                updated_at=datetime.now(UTC))
    with session_factory() as session:
        bar = session.scalar(select(DailyBar).where(
            DailyBar.security_id == reference_data, DailyBar.trade_date == date(2026, 5, 12),
        ))
        assert bar is not None
        bar.stock_splits = 2
        session.commit()
    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["levels"]["current_stop"] is None
    assert data["recorded_stop"]["price"] == 105
    assert data["recorded_stop"]["basis_verified"] is False
    assert any("拆股" in reason for reason in data["guardrails"]["reasons"])
