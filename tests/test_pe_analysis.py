from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trade_news_analysis.models import PEAnalysisProfile, Security
from trade_news_analysis.services.pe_analysis import (
    analysis_response,
    calculate_forecast,
    default_assumptions,
    is_refresh_recommended,
)


def amd_assumptions() -> list[dict[str, float | int | None]]:
    return [
        {
            "year_offset": offset,
            "revenue_growth": 0.27,
            "net_income_growth": growth,
            "pe_low": 15.0,
            "pe_high": 20.0,
        }
        for offset, growth in enumerate([1.13, 0.3, 0.3, 0.3], 1)
    ]


def test_calculate_forecast_ties_to_amd_reference_workbook() -> None:
    status, rows, warnings = calculate_forecast(
        fiscal_year=2024,
        current_price=150.77,
        revenue=25_796_000_000,
        net_income=3_910_465_642,
        shares_outstanding=1_616_000_000,
        assumptions=amd_assumptions(),
    )

    assert status == "ready"
    assert warnings == []
    assert rows[0]["cagr_low"] == pytest.approx(77.3139710779084 / 150.77 - 1)
    assert rows[-1]["revenue"] == pytest.approx(67_106_911_592.36)
    assert rows[-1]["eps"] == pytest.approx(11.32391963054432)
    assert rows[-1]["price_low"] == pytest.approx(169.85879445816482)
    assert rows[-1]["price_high"] == pytest.approx(226.47839261088643)
    assert rows[-1]["cagr_low"] == pytest.approx(0.03025154777698602)
    assert rows[-1]["cagr_high"] == pytest.approx(0.10707733545581433)


def test_analysis_response_prefers_manual_overrides() -> None:
    security = Security(
        id=1,
        market="US",
        exchange="NASDAQ",
        symbol="AMD",
        name="Advanced Micro Devices",
        currency="USD",
    )
    profile = PEAnalysisProfile(
        security_id=1,
        source_fiscal_year=2024,
        source_price=100,
        source_revenue=1_000,
        source_net_income=100,
        source_shares_outstanding=100,
        price_override=80,
        assumptions=[
            {**item, "revenue_growth": 0.1, "net_income_growth": 0.1}
            for item in default_assumptions()
        ],
    )

    result = analysis_response(security, profile)

    assert result["effective_inputs"]["price"] == {
        "value": 80,
        "provenance": "manual",
    }
    assert result["effective_inputs"]["revenue"]["provenance"] == "auto"
    assert result["forecast"][0]["year"] == 2025


def test_negative_earnings_and_missing_inputs_are_guarded() -> None:
    complete = [
        {**item, "revenue_growth": 0.1, "net_income_growth": 0.1}
        for item in default_assumptions()
    ]
    status, rows, warnings = calculate_forecast(
        fiscal_year=2024,
        current_price=100,
        revenue=1_000,
        net_income=-100,
        shares_outstanding=100,
        assumptions=complete,
    )
    assert status == "not_applicable"
    assert all(row["eps"] < 0 for row in rows)
    assert all(row["price_low"] is None and row["cagr_low"] is None for row in rows)
    assert any("EPS 非正" in warning for warning in warnings)

    missing_status, _, missing_warnings = calculate_forecast(
        fiscal_year=2024,
        current_price=0,
        revenue=0,
        net_income=10,
        shares_outstanding=0,
        assumptions=complete,
    )
    assert missing_status == "needs_data"
    assert any("当前价" in warning and "营收" in warning for warning in missing_warnings)


def test_refresh_recommendation_uses_24_hour_ttl() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    profile = PEAnalysisProfile(
        security_id=1,
        assumptions=default_assumptions(),
        source_attempted_at=now - timedelta(hours=23),
    )
    assert not is_refresh_recommended(profile, now)
    profile.source_attempted_at = now - timedelta(hours=24)
    assert is_refresh_recommended(profile, now)
