from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.risk_models import SecurityRiskProfile
from trade_news_analysis.services.market_research import MarketResearchService
from trade_news_analysis.services.risk import (
    build_risk_plan,
    get_risk_inputs,
    save_portfolio_risk,
    save_security_risk,
)

from .test_market_research import NOW, FixtureProvider, apple, market_frame


def configure_risk(session: Session, settings: Settings, **changes: Any) -> int:
    security = apple(session)
    MarketResearchService(settings, FixtureProvider({"AAPL": market_frame()})).refresh(
        session, [security.id], now=NOW
    )
    save_portfolio_risk(
        session, {"total_value": 100_000, "available_cash": 80_000, "currency": "USD"}
    )
    save_security_risk(
        session,
        security.id,
        {
            "current_weight": 0,
            "current_quantity": 0,
            "max_weight": 0.2,
            "risk_budget_pct": 0.01,
            "stop_price": 95,
            "sector_limit_pct": 0.4,
            "sector_current_weight": 0,
            "lot_size": 1,
            "max_participation_pct": 0.1,
            "fee_bps": 10,
            "slippage_bps": 10,
            **changes,
        },
    )
    return security.id


def test_missing_inputs_never_create_default_quantity(session: Session) -> None:
    result = build_risk_plan(session, apple(session).id, now=NOW)
    assert result["status"] == "blocked"
    assert result["max_buy_quantity"] is None
    assert "请填写组合总额" in result["blockers"]
    assert "请填写每筆风险预算" not in result["blockers"]


def test_position_is_limited_by_risk_budget_after_explicit_costs(
    session: Session,
    settings: Settings,
) -> None:
    security_id = configure_risk(session, settings)
    result = build_risk_plan(session, security_id, now=NOW)

    assert result["status"] == "ready"
    assert result["loss_per_share"] == pytest.approx(5.4)
    assert result["max_buy_quantity"] == 185
    assert result["target_quantity_cap"] == 185
    assert result["required_reduce_quantity"] == 0


def test_cash_sector_and_lot_constraints_reduce_order_capacity(
    session: Session,
    settings: Settings,
) -> None:
    security_id = configure_risk(session, settings, lot_size=10, sector_current_weight=0.39)
    save_portfolio_risk(session, {"available_cash": 5_000})
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["max_buy_quantity"] == 10
    assert result["max_buy_quantity"] % 10 == 0


def test_existing_position_can_exceed_new_risk_cap_and_trigger_reduction(
    session: Session,
    settings: Settings,
) -> None:
    security_id = configure_risk(
        session,
        settings,
        current_weight=0.15,
        current_quantity=150,
        average_cost=80,
        sector_current_weight=0.3,
        risk_budget_pct=0.002,
    )
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "ready"
    assert result["target_quantity_cap"] == 37
    assert result["required_reduce_quantity"] == 113
    assert result["max_buy_quantity"] == 0


def test_breached_user_stop_does_not_invent_a_new_stop(
    session: Session, settings: Settings
) -> None:
    security_id = configure_risk(
        session,
        settings,
        current_weight=0.15,
        current_quantity=150,
        average_cost=80,
        sector_current_weight=0.3,
        stop_price=101,
    )
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "stop_triggered"
    assert result["required_reduce_quantity"] == 150
    assert result["max_buy_quantity"] == 0


@pytest.mark.parametrize("field,expected", [("fee_bps", 10), ("slippage_bps", 10), ("lot_size", 1)])
def test_clearing_planning_assumptions_restores_labelled_defaults(
    session: Session,
    settings: Settings,
    field: str,
    expected: float,
) -> None:
    security_id = configure_risk(session, settings)
    save_security_risk(session, security_id, {field: None})
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "ready"
    assert result["inputs"]["security"][field] == expected
    assert result["inputs"]["field_sources"][field]["kind"] == "default"


def test_unknown_manual_sector_exposure_still_blocks_sizing(
    session: Session, settings: Settings,
) -> None:
    security_id = configure_risk(session, settings, sector_current_weight=None)
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "blocked"
    assert result["inputs"]["security"]["sector_current_weight"] is None


def test_defaults_are_available_without_writing_profiles_or_inventing_facts(
    session: Session,
) -> None:
    security = apple(session)
    result = get_risk_inputs(session, security.id, now=NOW)
    values = result["security"]
    assert values["max_weight"] == 0.2
    assert values["risk_budget_pct"] == 0.01
    assert values["sector_limit_pct"] == 0.4
    assert values["max_participation_pct"] == 0.01
    assert values["lot_size"] == 1
    assert values["fee_bps"] == values["slippage_bps"] == 10
    assert values["benchmark_symbol"] == "XLK"
    assert values["benchmark_market"] == "US"
    assert values["benchmark_currency"] == "USD"
    for field in ("current_quantity", "current_weight", "average_cost", "stop_price"):
        assert values[field] is None
    assert result["portfolio"]["total_value"] is None
    assert session.scalar(select(func.count(SecurityRiskProfile.id))) == 0


def test_default_stop_follows_prices_but_saved_values_and_zero_costs_are_preserved(
    session: Session, settings: Settings,
) -> None:
    security = apple(session)
    provider = FixtureProvider({"AAPL": market_frame()})
    market = MarketResearchService(settings, provider)
    market.refresh(session, [security.id], now=NOW)
    assert get_risk_inputs(session, security.id, now=NOW)["security"]["stop_price"] == 90
    save_security_risk(session, security.id, {"max_weight": 0.35, "fee_bps": 0})
    provider.frames["AAPL"] = market_frame(price=200)
    market.refresh(session, [security.id], now=NOW)
    values = get_risk_inputs(session, security.id, now=NOW)["security"]
    assert (values["stop_price"], values["max_weight"], values["fee_bps"]) == (180, 0.35, 0)
    save_security_risk(session, security.id, {"stop_price": 120, "benchmark_symbol": "XLY"})
    provider.frames["AAPL"] = market_frame(price=250)
    market.refresh(session, [security.id], now=NOW)
    result = get_risk_inputs(session, security.id, now=NOW)
    assert result["security"]["stop_price"] == 120
    assert result["field_sources"]["stop_price"]["kind"] == "manual"
    assert result["security"]["benchmark_symbol"] == "XLY"
    assert result["security"]["benchmark_label"] == "自选行业基准"


@pytest.mark.parametrize("market,symbol,expected", [
    ("HK", "00700.HK", None), ("A", "600519.SH", 100), ("A", "688001.SH", None),
])
def test_lot_default_respects_market_and_existing_security_metadata(
    session: Session, market: str, symbol: str, expected: int | None,
) -> None:
    security = apple(session)
    security.market, security.symbol = market, symbol
    result = get_risk_inputs(session, security.id, now=NOW)
    assert result["security"]["lot_size"] == expected
    assert result["security"]["benchmark_symbol"] is None
    security.provider_data = {"board_lot": 500}
    assert get_risk_inputs(session, security.id, now=NOW)["security"]["lot_size"] == 500


def test_currency_mismatch_and_stale_quote_block_executable_numbers(
    session: Session,
    settings: Settings,
) -> None:
    security_id = configure_risk(session, settings)
    save_portfolio_risk(session, {"currency": "CNY"})
    result = build_risk_plan(session, security_id, now=NOW)
    assert any("币种" in item for item in result["blockers"])
    save_portfolio_risk(session, {"currency": "USD"})
    stale = build_risk_plan(session, security_id, now=datetime(2026, 9, 22, 22, tzinfo=UTC))
    assert stale["max_buy_quantity"] is None


def test_risk_inputs_reject_nonfinite_weights_and_inconsistent_holdings(
    session: Session,
    settings: Settings,
) -> None:
    security_id = configure_risk(session, settings)
    for payload in (
        {"max_weight": float("nan")},
        {"risk_budget_pct": 20},
        {"lot_size": 0.5},
        {"current_quantity": 10},
    ):
        with pytest.raises(ValueError):
            save_security_risk(session, security_id, payload)
    assert get_risk_inputs(session, security_id)["security"]["current_quantity"] == 0


def test_missing_cash_and_sector_do_not_hide_known_position_risk(
    session: Session, settings: Settings,
) -> None:
    security_id = configure_risk(
        session, settings, current_quantity=250, current_weight=0.25, average_cost=80,
        max_weight=None, sector_current_weight=None,
    )
    save_portfolio_risk(session, {"available_cash": None})
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "blocked"
    assert result["max_buy_quantity"] is result["required_reduce_quantity"] is None
    review = result["position_review"]
    assert review["status"] == "attention"
    assert [alert["code"] for alert in review["alerts"]] == ["concentration", "loss_budget"]
    assert review["loss_to_stop_pct"] == pytest.approx(0.0135)
    concentration = review["alerts"][0]
    assert concentration["default_fields"] == ["max_weight"]
    assert "默认上限 20.00%" in concentration["detail"]
    assert "5.00 个百分点" in concentration["detail"]
    profile = session.scalar(select(SecurityRiskProfile).where(
        SecurityRiskProfile.security_id == security_id,
    ))
    assert profile is not None
    assert profile.max_weight is None


def test_missing_cash_does_not_hide_stop_but_stale_quote_cannot_trigger_it(
    session: Session, settings: Settings,
) -> None:
    security_id = configure_risk(
        session, settings, current_quantity=150, current_weight=0.15, average_cost=80,
        sector_current_weight=0.3, stop_price=101,
    )
    save_portfolio_risk(session, {"available_cash": None})
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["status"] == "blocked"
    assert result["position_review"]["alerts"][0]["code"] == "stop"
    stale = build_risk_plan(session, security_id, now=datetime(2026, 9, 22, 22, tzinfo=UTC))
    assert stale["position_review"]["alerts"] == []
    assert stale["position_review"]["quote_usable"] is False
    assert stale["required_reduce_quantity"] is None


@pytest.mark.parametrize("currency,quantity", [("CNY", 250), ("USD", 25)])
def test_loss_estimate_requires_same_currency_and_consistent_valuation(
    session: Session, settings: Settings, currency: str, quantity: int,
) -> None:
    security_id = configure_risk(
        session, settings, current_quantity=quantity, current_weight=0.25,
        average_cost=80, sector_current_weight=0.3,
    )
    save_portfolio_risk(session, {"currency": currency, "available_cash": None})
    result = build_risk_plan(session, security_id, now=NOW)
    assert result["position_review"]["loss_to_stop_pct"] is None
    assert [alert["code"] for alert in result["position_review"]["alerts"]] == ["concentration"]
    assert result["required_reduce_quantity"] is None
