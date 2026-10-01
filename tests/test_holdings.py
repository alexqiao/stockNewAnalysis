from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.holding_models import HoldingSnapshot, HoldingSyncRun
from trade_news_analysis.models import Security, Watchlist
from trade_news_analysis.services.holdings import (
    HOLDINGS_LOCK,
    HoldingsBusyError,
    HoldingService,
    read_holdings,
    security_identity,
)
from trade_news_analysis.services.ibkr import (
    BrokerError,
    BrokerPosition,
    BrokerSnapshot,
    account_key,
)
from trade_news_analysis.services.risk import (
    build_risk_plan,
    get_risk_inputs,
    save_portfolio_risk,
    save_security_risk,
)

from . import test_decisions_api as api_fixtures

decisions_client = api_fixtures.decisions_client
KEY = account_key("U1234567")


def position(**changes: Any) -> BrokerPosition:
    values: dict[str, Any] = {
        "con_id": 101,
        "symbol": "AAPL",
        "security_type": "STK",
        "exchange": "NASDAQ",
        "currency": "USD",
        "quantity": 10,
        "average_cost": 90,
        "market_price": 100,
        "market_value": 1000,
        "unrealized_pnl": 100,
    }
    return BrokerPosition(**{**values, **changes})


def snapshot(**changes: Any) -> BrokerSnapshot:
    values: dict[str, Any] = {
        "account_key": KEY,
        "account_label": "IBKR ••••4567",
        "captured_at": datetime.now(UTC),
        "currency": "USD",
        "net_liquidation": 100000,
        "cash_balance": 6000,
        "settled_cash": 5000,
        "available_funds": 4000,
        "positions": [position()],
        "exchange_rates": {"USD": 1, "HKD": 0.128},
    }
    return BrokerSnapshot(**{**values, **changes})


class FakeBroker:
    def __init__(self, **_kwargs: Any) -> None:
        self.data = snapshot()
        self.error: Exception | None = None
        self.closed = False

    def __enter__(self) -> FakeBroker:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.closed = True

    def accounts(self) -> list[dict[str, str]]:
        return [{"account_key": KEY, "label": "IBKR ••••4567"}]

    def snapshot(self, _key: str) -> BrokerSnapshot:
        if self.error:
            raise self.error
        return self.data


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def service(session_factory: SessionFactory, broker: FakeBroker) -> HoldingService:
    return HoldingService(session_factory, lambda **kwargs: broker)


def apple_id(session: Session) -> int:
    value = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
    assert value is not None
    return value


def test_import_updates_facts_preserves_constraints_and_order(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        apple = apple_id(session)
        save_security_risk(session, apple, {"max_weight": 0.2, "stop_price": 80})
        initial = list(session.scalars(select(Watchlist.security_id).order_by(Watchlist.position)))
        session.commit()
    broker.data = snapshot(
        positions=[position(), position(con_id=102, symbol="SPY", exchange="ARCA")]
    )
    state = service.sync(KEY, 7496, 72)
    assert state["portfolio"]["available_cash"] == 4000
    assert broker.closed
    with session_factory() as session:
        ordered = list(session.scalars(select(Watchlist).order_by(Watchlist.position)))
        assert [row.security_id for row in ordered[:2]] == initial
        assert [row.holding_status for row in ordered] == ["long", "flat", "long"]
        facts = get_risk_inputs(session, apple)["security"]
        assert {
            key: facts[key]
            for key in (
                "current_quantity",
                "average_cost",
                "current_weight",
                "max_weight",
                "stop_price",
            )
        } == {
            "current_quantity": 10,
            "average_cost": 90,
            "current_weight": 0.01,
            "max_weight": 0.2,
            "stop_price": 80,
        }
        assert (
            session.scalar(select(Security).where(Security.symbol == "SPY")).exchange == "NYSEARCA"
        )
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        assert session.scalar(select(func.count(Watchlist.id))) == 3
        assert session.scalar(select(func.count(HoldingSnapshot.id))) == 2


def test_sector_defaults_sum_same_snapshot_and_keep_manual_overrides(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        apple = session.get(Security, apple_id(session))
        apple.industry = "Consumer Electronics"
        microsoft = session.scalar(select(Security).where(Security.symbol == "MSFT"))
        microsoft.industry = "Software - Infrastructure"
        session.commit()
    broker.data = snapshot(positions=[
        position(), position(con_id=102, symbol="MSFT", market_value=2000),
    ])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        result = get_risk_inputs(session, apple_id(session))
        assert result["security"]["sector_current_weight"] == pytest.approx(0.03)
        assert result["security"]["stop_price"] == 90
        assert not result["defaults_blockers"]
        assert result["field_sources"]["sector_current_weight"]["kind"] == "automatic"
        save_security_risk(session, apple_id(session), {"sector_current_weight": 0.3})
        session.commit()
    broker.data = snapshot(positions=[position(market_value=5000, market_price=500)])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        result = get_risk_inputs(session, apple_id(session))
        assert result["security"]["sector_current_weight"] == 0.3
        assert result["security"]["stop_price"] == 450
        assert result["security"]["current_weight"] == 0.05
        assert result["field_sources"]["sector_current_weight"]["kind"] == "manual"


def test_sector_default_retains_weight_precision_without_false_concentration_blocker(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(net_liquidation=123456.78, positions=[position(market_value=29123.45)])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        result = build_risk_plan(session, apple_id(session))
        values = result["inputs"]["security"]
        assert values["sector_current_weight"] == values["current_weight"]
        assert not any("单股仓位超过" in blocker for blocker in result["blockers"])


@pytest.mark.parametrize("extra", [
    position(con_id=102, symbol="UNKNOWN"),
    position(con_id=102, symbol="OPTION", security_type="OPT"),
    position(con_id=102, symbol="MSFT", market_value=None),
    position(con_id=102, symbol="MSFT", quantity=-10, market_value=-1000),
])
def test_incomplete_sector_defaults_do_not_claim_full_coverage(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
    extra: BrokerPosition,
) -> None:
    with session_factory() as session:
        microsoft = session.scalar(select(Security).where(Security.symbol == "MSFT"))
        microsoft.industry = "Software - Infrastructure"
        session.commit()
    broker.data = snapshot(positions=[position(), extra])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        result = get_risk_inputs(session, apple_id(session))
        assert result["defaults_blockers"]
        assert result["field_sources"]["sector_current_weight"]["kind"] == "estimate"
        assert build_risk_plan(session, apple_id(session))["max_buy_quantity"] is None
        confirmed = save_security_risk(session, apple_id(session), {"sector_current_weight": 0.2})
        assert not confirmed["defaults_blockers"]
        assert confirmed["security"]["sector_current_weight"] == 0.2


def test_reduction_empty_account_and_switch_use_only_latest_snapshot(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    service.sync(KEY, 7496, 72)
    broker.data = snapshot(positions=[position(quantity=2, market_value=200)])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        assert get_risk_inputs(session, apple_id(session))["security"]["current_quantity"] == 2
    broker.data = snapshot(positions=[])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        risk = get_risk_inputs(session, apple_id(session))["security"]
        assert (risk["current_quantity"], risk["current_weight"], risk["average_cost"]) == (
            0,
            0,
            None,
        )
        assert risk["sector_current_weight"] == 0
        assert set(session.scalars(select(Watchlist.holding_status))) == {"flat"}
    other = account_key("U7654321")
    broker.data = snapshot(account_key=other, positions=[position(con_id=103, symbol="MSFT")])
    result = service.sync(other, 7496, 72)
    assert result["account_key"] == other
    assert [item["symbol"] for item in result["positions"]] == ["MSFT"]
    with session_factory() as session:
        assert get_risk_inputs(session, apple_id(session))["security"]["current_quantity"] == 0


@pytest.mark.parametrize(
    "error", [BrokerError("读取超时"), RuntimeError("private account payload")]
)
def test_failed_sync_retains_success_and_position_review(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
    error: Exception,
) -> None:
    before = service.sync(KEY, 7496, 72)
    broker.error = error
    with pytest.raises(BrokerError):
        service.sync(KEY, 7496, 72)
    with session_factory() as session:
        state = read_holdings(session)
        assert state["snapshot_id"] == before["snapshot_id"]
        assert state["positions"] == before["positions"]
        assert state["last_sync"]["status"] == "failed"
        assert "private" not in state["last_sync"]["error"]
        risk = build_risk_plan(session, apple_id(session))
        assert state["using_cached"] is True
        assert state["blockers"] == []
        assert state["warnings"] == ["最近同步失败"]
        assert risk["inputs"]["security"]["current_quantity"] == 10
        assert risk["position_review"]["status"] != "unavailable"
        assert not any("同步未成功" in gap for gap in risk["blockers"])
        assert "最近同步失败" in risk["inputs"]["holdings_source"]["note"]


@pytest.mark.parametrize("sync_status", [None, "failed", "running"])
def test_old_snapshot_remains_usable_until_successful_replacement(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
    sync_status: str | None,
) -> None:
    captured = datetime.now(UTC) - timedelta(days=30)
    broker.data = snapshot(
        captured_at=captured, positions=[position(quantity=250, market_value=25000)],
    )
    before = service.sync(KEY, 7496, 72)
    with session_factory() as session:
        if sync_status:
            session.add(HoldingSyncRun(account_key=KEY, status=sync_status))
            session.commit()
        state = read_holdings(session)
        assert state["snapshot_id"] == before["snapshot_id"]
        assert state["positions"] == before["positions"]
        assert state["stale"] and state["using_cached"]
        assert state["blockers"] == []
        assert captured.strftime("%Y-%m-%d %H:%M") in state["note"]
        risk = build_risk_plan(session, apple_id(session))
        assert risk["position_review"]["status"] == "attention"
        assert risk["position_review"]["alerts"][0]["code"] == "concentration"
    broker.data = snapshot(positions=[])
    replaced = service.sync(KEY, 7496, 72)
    assert replaced["snapshot_id"] != before["snapshot_id"]
    assert replaced["positions"] == []
    assert not replaced["stale"] and not replaced["using_cached"]
    assert replaced["warnings"] == []


def test_future_snapshot_still_blocks_position_review(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(captured_at=datetime.now(UTC) + timedelta(hours=1))
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        state = read_holdings(session)
        assert state["blockers"] == ["真实持仓快照时间异常，请重新同步"]
        assert state["using_cached"] is False
        risk = build_risk_plan(session, apple_id(session))
        assert risk["position_review"]["status"] == "unavailable"
        assert risk["max_buy_quantity"] is None


def test_first_sync_failure_does_not_invent_flat_holdings(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    broker.error = BrokerError("读取超时")
    with pytest.raises(BrokerError):
        service.sync(KEY, 7496, 72)
    with session_factory() as session:
        state = read_holdings(session)
        assert not state["active"] and not state["using_cached"]
        assert state["captured_at"] is None
        assert state["last_sync"]["status"] == "failed"
        inputs = get_risk_inputs(session, apple_id(session))
        assert inputs["security"]["current_quantity"] is None
        assert inputs["holdings_source"].get("holding_status") is None


def test_mapping_failure_rolls_back_new_security_and_snapshot(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    before = service.sync(KEY, 7496, 72)
    broker.data = snapshot(
        positions=[
            position(con_id=104, symbol="NEW"),
            position(currency="HKD"),
        ]
    )
    with pytest.raises(BrokerError, match="不一致"):
        service.sync(KEY, 7496, 72)
    with session_factory() as session:
        assert session.scalar(select(Security).where(Security.symbol == "NEW")) is None
        assert read_holdings(session)["snapshot_id"] == before["snapshot_id"]


def test_ambiguous_symbol_is_not_guessed(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        session.add(Security(market="US", exchange="NASD", symbol="AAPL", name="Ambiguous"))
        session.commit()
    broker.data = snapshot(positions=[position(exchange="NYSE")])
    with pytest.raises(BrokerError, match="多个证券"):
        service.sync(KEY, 7496, 72)
    with session_factory() as session:
        assert not read_holdings(session)["active"]


def test_missing_fields_fx_and_unsupported_assets_remain_explicit(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(
        settled_cash=None,
        positions=[
            position(market_price=None, market_value=None, average_cost=float("nan")),
            position(con_id=102, symbol="700", exchange="SEHK", currency="HKD", market_value=1000),
            position(con_id=103, symbol="AAPL", security_type="OPT"),
        ],
    )
    result = service.sync(KEY, 7496, 72)
    assert result["portfolio"]["available_cash"] is None
    apple, hk, option = result["positions"]
    assert apple["average_cost"] is None and apple["weight"] is None
    assert hk["weight"] == pytest.approx(0.00128)
    assert option["unsupported_reason"] and option["security_id"] is None
    with session_factory() as session:
        assert session.scalar(select(Security).where(Security.symbol == "00700.HK")) is not None
    broker.data = replace(broker.data, exchange_rates={})
    result = service.sync(KEY, 7496, 72)
    assert result["positions"][1]["weight"] is None


def test_short_stale_and_negative_nav_never_produce_long_sizing(
    service: HoldingService,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(
        captured_at=datetime.now(UTC) - timedelta(hours=25),
        net_liquidation=-100,
        positions=[position(quantity=-10, market_value=-1000)],
    )
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        risk = build_risk_plan(session, apple_id(session))
        assert any("空头" in gap for gap in risk["blockers"])
        assert risk["inputs"]["holdings_source"]["stale"] is True
        assert "快照超过 24 小时" in risk["inputs"]["holdings_source"]["warnings"]
        assert any("净值" in gap for gap in risk["blockers"])
        assert risk["max_buy_quantity"] is None
        assert risk["position_review"]["status"] == "unsupported"
        assert risk["position_review"]["alerts"] == []
        assert (
            session.scalar(
                select(Watchlist).where(Watchlist.security_id == apple_id(session))
            ).holding_status
            == "short"
        )


def test_current_short_is_excluded_from_long_position_alerts(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(positions=[position(quantity=-250, market_value=-25000)])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        review = build_risk_plan(session, apple_id(session))["position_review"]
        assert review["status"] == "unsupported"
        assert review["alerts"] == []


@pytest.mark.parametrize("last_status", ["failed", "running"])
def test_cached_holdings_keep_action_and_snapshot_note_across_pages(
    decisions_client: TestClient, broker: FakeBroker, session_factory: SessionFactory,
    last_status: str,
) -> None:
    captured = datetime.now(UTC) - timedelta(days=30)
    broker.data = snapshot(
        captured_at=captured, positions=[position(quantity=250, market_value=25000)],
    )
    decisions_client.app.state.holdings.client_factory = lambda **kwargs: broker
    assert decisions_client.post(
        "/api/v1/holdings/ibkr/sync", json={"account_key": KEY},
    ).status_code == 200
    with session_factory() as session:
        session.add(HoldingSyncRun(account_key=KEY, status=last_status))
        session.commit()
    item = next(row for row in decisions_client.get("/api/v1/watchlist").json()
                if row["security"]["symbol"] == "AAPL")
    sid = item["security_id"]
    execution = item["judgment"]["strategy"]["execution"]
    assert execution["action_code"] == "reduce"
    assert "先核对真实持仓" not in execution["next_action"]
    note = execution["brief"]["holdings_note"]
    assert captured.strftime("%Y-%m-%d %H:%M") in note
    assert "按该快照估算" in note
    detail = decisions_client.get(f"/api/v1/securities/{sid}").json()
    assert detail["judgment"]["strategy"]["execution"] == execution
    assert item["risk_plan"]["inputs"]["security"]["current_quantity"] == 250
    for path in ("/", "/watchlist", f"/research?security_id={sid}", f"/securities/{sid}"):
        page = decisions_client.get(path)
        assert page.status_code == 200
        assert note in page.text
        assert "已过期，请重新同步" not in page.text


def test_position_direction_matches_home_detail_and_research_without_cash(
    decisions_client: TestClient, broker: FakeBroker,
) -> None:
    broker.data = snapshot(
        settled_cash=None, positions=[position(quantity=250, market_value=25000)],
    )
    decisions_client.app.state.holdings.client_factory = lambda **kwargs: broker
    assert decisions_client.post(
        "/api/v1/holdings/ibkr/sync", json={"account_key": KEY},
    ).status_code == 200
    item = next(row for row in decisions_client.get("/api/v1/watchlist").json()
                if row["security"]["symbol"] == "AAPL")
    sid = item["security_id"]
    execution = item["judgment"]["strategy"]["execution"]
    assert execution["action_code"] == "reduce"
    assert "默认上限 20.00%" in execution["reason"]
    assert not execution["ready"]
    assert item["risk_plan"]["required_reduce_quantity"] is None
    detail = decisions_client.get(f"/api/v1/securities/{sid}").json()
    assert detail["judgment"]["strategy"]["execution"] == execution
    research = decisions_client.get(f"/api/v1/research/securities/{sid}").json()
    assert research["risk"]["position_review"] == item["risk_plan"]["position_review"]
    assert item["judgment"]["action"]["plan"]["tasks"][0]["kind"] == "position_risk"
    for url in ("/", f"/securities/{sid}", f"/research?security_id={sid}"):
        page = decisions_client.get(url)
        assert page.status_code == 200
        assert "默认上限 20.00%" in page.text
        if not url.startswith("/research"):
            assert execution["next_action"] in page.text
            assert "加仓条件" in page.text
    home = decisions_client.get("/").text
    assert "先核对仓位风险与报价" in home
    assert "跟踪催化，检查加减仓条件" in home
    assert "复核经营假设与行业表现" in home


def test_partial_sector_estimate_does_not_claim_a_verified_sector_breach(
    service: HoldingService, broker: FakeBroker, session_factory: SessionFactory,
) -> None:
    broker.data = snapshot(positions=[
        position(quantity=500, market_value=50000),
        position(con_id=102, symbol="UNKNOWN", security_type="OPT", market_value=1000),
    ])
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        apple = session.get(Security, apple_id(session))
        apple.industry = "Consumer Electronics"
        risk = build_risk_plan(session, apple.id)
        assert risk["inputs"]["field_sources"]["sector_current_weight"]["kind"] == "estimate"
        assert [alert["code"] for alert in risk["position_review"]["alerts"]] == ["concentration"]
        assert risk["required_reduce_quantity"] is None


def test_fact_edits_locked_but_constraints_remain_editable(
    service: HoldingService,
    session_factory: SessionFactory,
) -> None:
    service.sync(KEY, 7496, 72)
    with session_factory() as session:
        with pytest.raises(ValueError, match="IBKR"):
            save_portfolio_risk(session, {"total_value": 1})
        with pytest.raises(ValueError, match="IBKR"):
            save_security_risk(session, apple_id(session), {"current_quantity": 0})
        result = save_security_risk(session, apple_id(session), {"max_weight": 0.15})
        assert result["security"]["max_weight"] == 0.15
        assert result["security"]["current_quantity"] == 10


def test_concurrent_sync_and_discovery_rejected(service: HoldingService) -> None:
    with HOLDINGS_LOCK:
        with pytest.raises(HoldingsBusyError):
            service.sync(KEY, 7496, 72)
        with pytest.raises(HoldingsBusyError):
            service.accounts(7496, 72)


@pytest.mark.parametrize(
    "market,exchange,symbol,expected",
    [
        ("USD", "ARCA", "SPY", ("US", "NYSEARCA", "SPY")),
        ("USD", "NYSE", "BRK B", ("US", "NYSE", "BRK.B")),
        ("HKD", "SEHK", "700", ("HK", "HK", "00700.HK")),
        ("CNH", "SEHKNTL", "600000", ("A", "SH", "600000.SH")),
        ("CNY", "SEHKSZSE", "000001", ("A", "SZ", "000001.SZ")),
    ],
)
def test_market_qualified_identity(market: str, exchange: str, symbol: str, expected: Any) -> None:
    assert (
        security_identity(position(currency=market, exchange=exchange, symbol=symbol)) == expected
    )


def test_holdings_api_and_pages_share_snapshot(
    decisions_client: TestClient,
    broker: FakeBroker,
    session_factory: SessionFactory,
) -> None:
    decisions_client.app.state.holdings.client_factory = lambda **kwargs: broker
    accounts = decisions_client.post("/api/v1/holdings/ibkr/accounts", json={}).json()["accounts"]
    assert "U1234567" not in str(accounts)
    endpoint = "/api/v1/holdings/ibkr/sync"
    assert (
        decisions_client.post(endpoint, json={"account_key": KEY, "port": 7497}).status_code == 422
    )
    response = decisions_client.post(endpoint, json={"account_key": KEY})
    assert response.status_code == 200
    assert decisions_client.get("/api/v1/holdings").json()["active"]
    rows = decisions_client.get("/api/v1/watchlist").json()
    apple = next(row for row in rows if row["security"]["symbol"] == "AAPL")
    assert apple["holding_status"] == "long"
    assert apple["risk_plan"]["inputs"]["security"]["current_quantity"] == 10
    assert apple["holdings_source"]["source"] == "ibkr"
    sid = apple["security_id"]
    assert (
        decisions_client.put(
            "/api/v1/watchlist",
            json={
                "items": [
                    {"security_id": sid, "holding_status": "flat"},
                ]
            },
        ).status_code
        == 409
    )
    assert (
        decisions_client.put(
            "/api/v1/watchlist",
            json={
                "items": [
                    {"security_id": sid},
                ]
            },
        ).json()[0]["holding_status"]
        == "long"
    )
    assert (
        decisions_client.put("/api/v1/research/portfolio", json={"total_value": 1}).status_code
        == 422
    )
    assert (
        decisions_client.put(
            f"/api/v1/research/securities/{sid}/risk", json={"current_quantity": 0}
        ).status_code
        == 422
    )
    assert (
        decisions_client.put(
            f"/api/v1/research/securities/{sid}/risk", json={"max_weight": 0.2}
        ).status_code
        == 200
    )
    for path in ("/", "/watchlist", f"/research?security_id={sid}", f"/securities/{sid}"):
        page = decisions_client.get(path)
        assert page.status_code == 200, path
    watchlist = decisions_client.get("/watchlist").text
    assert "同步真实持仓" in watchlist and "holdings.js" in watchlist
    research = decisions_client.get(f"/research?security_id={sid}").text
    from bs4 import BeautifulSoup

    quantity_input = BeautifulSoup(research, "html.parser").select_one('[name="current_quantity"]')
    assert quantity_input is not None
    assert float(quantity_input["value"]) == 10
    assert quantity_input.has_attr("disabled")
    broker.data = snapshot(positions=[position(quantity=-10, market_value=-1000)])
    assert decisions_client.post(endpoint, json={"account_key": KEY}).status_code == 200
    detail = decisions_client.get(f"/api/v1/securities/{sid}").json()
    assert detail["judgment"]["action"]["holding_status"] == "short"
    assert detail["judgment"]["strategy"]["execution"]["ready"] is False
    assert "复核空头" in detail["judgment"]["strategy"]["execution"]["next_action"]
    assert decisions_client.get(f"/securities/{sid}").status_code == 200


def test_restart_marks_interrupted_attempt_failed(
    service: HoldingService, session_factory: SessionFactory,
) -> None:
    before = service.sync(KEY, 7496, 72)
    with session_factory() as session:
        session.add(HoldingSyncRun(account_key=KEY))
        session.commit()
    service = HoldingService(session_factory)
    with session_factory() as session:
        assert read_holdings(session)["last_sync"]["status"] == "running"
    service.recover_interrupted()
    with session_factory() as session:
        state = read_holdings(session)
        assert state["last_sync"]["status"] == "failed"
        assert state["snapshot_id"] == before["snapshot_id"]
        assert state["positions"] == before["positions"]
        assert state["blockers"] == []
