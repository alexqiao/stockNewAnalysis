from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.daily_bar_models import DailyBar, DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.holding_models import HoldingPosition, HoldingSnapshot, HoldingSyncRun
from trade_news_analysis.models import Watchlist
from trade_news_analysis.risk_models import SecurityRiskProfile
from trade_news_analysis.services.bollinger_context import _risk_limits

from . import test_bollinger_reference_api as reference_fixtures

decisions_client = reference_fixtures.decisions_client
reference_data = reference_fixtures.reference_data
record_long = reference_fixtures.record_long


def test_market_currency_mismatch_blocks_current_reference(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    with session_factory() as session:
        state = session.get(DailyBarSyncState, reference_data)
        assert state is not None
        state.currency = "HKD"
        session.commit()

    response = decisions_client.get(f"/api/v1/securities/{reference_data}/bollinger-reference")
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["stale"] is False
    assert data["levels"]["middle"] == 50
    assert data["action"]["code"] == "blocked"
    assert any("行情与证券币种不一致" in item for item in data["guardrails"]["reasons"])
    assert data["execution_ready"] is False


def test_fresh_broker_short_cannot_become_a_long_reference(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(session_factory, reference_data, stop_price=105)
    with session_factory() as session:
        run = HoldingSyncRun(account_key="fixture", status="success")
        session.add(run)
        session.flush()
        snapshot = HoldingSnapshot(
            run_id=run.id, account_key="fixture", account_label="fixture",
            captured_at=datetime.now(UTC), currency="USD", net_liquidation=100_000,
            settled_cash=5_000, available_funds=5_000,
        )
        session.add(snapshot)
        session.flush()
        session.add(HoldingPosition(
            snapshot_id=snapshot.id, security_id=reference_data, con_id=1, symbol="AAPL",
            security_type="STK", exchange="NASDAQ", currency="USD", quantity=-10,
            average_cost=80, market_price=100, market_value=-1_000, weight=-0.01,
        ))
        session.commit()

    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["holding_source"] == "券商同步"
    assert data["position"]["status"] == "short"
    assert data["position"]["reliable"] is False
    assert data["action"]["code"] == "blocked"
    assert any("空头" in item for item in data["guardrails"]["reasons"])
    assert not any("过期" in item for item in data["guardrails"]["reasons"])


def test_unknown_position_does_not_infer_flat_from_missing_facts(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    with session_factory() as session:
        watch = session.scalar(select(Watchlist).where(Watchlist.security_id == reference_data))
        assert watch is not None
        watch.holding_status = "unknown"
        session.commit()

    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["position"]["status"] == "unknown"
    assert data["position"]["reliable"] is False
    assert data["action"]["code"] == "blocked"
    assert any("尚未确认持仓状态" in item for item in data["guardrails"]["reasons"])


@pytest.mark.parametrize("portfolio_currency,quantity,expected_excess", [
    ("USD", 100, True),
    ("HKD", 100, False),
    ("USD", 10, False),
])
def test_loss_budget_requires_same_currency_and_consistent_weight(
    portfolio_currency: str, quantity: int, expected_excess: bool,
) -> None:
    inputs: dict[str, Any] = {
        "portfolio": {"total_value": 100_000, "currency": portfolio_currency},
        "security": {
            "current_quantity": quantity, "current_weight": 0.1, "max_weight": 0.2,
            "sector_current_weight": 0.1, "sector_limit_pct": 0.4,
            "risk_budget_pct": 0.001, "fee_bps": 10, "slippage_bps": 10,
        },
    }
    reasons = _risk_limits(inputs, price=100, stop=80, currency="USD")
    assert any("风险预算" in reason for reason in reasons) is expected_excess
    if not expected_excess:
        assert reasons == []


def test_recorded_stop_trigger_takes_priority_over_concentration_limit(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(
        session_factory, reference_data, current_quantity=300, current_weight=0.3,
        max_weight=0.2, stop_price=105,
    )
    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["position"]["status"] == "long"
    assert data["position"]["reliable"] is True
    assert data["guardrails"]["blocked"] is True
    assert any("单股" in item for item in data["guardrails"]["reasons"])
    assert data["levels"]["current_stop"] == 52.5
    assert data["action"]["code"] == "stop_triggered"
    assert data["technical_action"]["code"] == "stop_triggered"
    assert data["execution_ready"] is False


def test_updating_weight_limit_after_split_does_not_revalidate_recorded_stop(
    decisions_client: TestClient, session_factory: SessionFactory, reference_data: int,
) -> None:
    record_long(
        session_factory, reference_data, stop_price=105,
        updated_at=datetime(2026, 5, 11, tzinfo=UTC),
    )
    with session_factory() as session:
        bar = session.scalar(select(DailyBar).where(
            DailyBar.security_id == reference_data, DailyBar.trade_date == date(2026, 5, 12),
        ))
        assert bar is not None
        bar.stock_splits = 2
        session.commit()

    response = decisions_client.put(
        f"/api/v1/research/securities/{reference_data}/risk", json={"max_weight": 0.25},
    )
    assert response.status_code == 200
    data = decisions_client.get(
        f"/api/v1/securities/{reference_data}/bollinger-reference",
    ).json()
    assert data["recorded_stop"] == {"price": 105, "currency": "USD", "basis_verified": False}
    assert data["levels"]["current_stop"] is None
    assert data["action"]["code"] == "blocked"
    assert data["technical_action"]["code"] != "stop_triggered"
    assert any("拆股" in item for item in data["guardrails"]["reasons"])
    with session_factory() as session:
        profile = session.scalar(select(SecurityRiskProfile).where(
            SecurityRiskProfile.security_id == reference_data,
        ))
        assert profile is not None
        assert profile.stop_price == 105
        assert profile.max_weight == 0.25
