from datetime import UTC, datetime

from trade_news_analysis.services.event_reaction import event_price_reaction


def market() -> dict:
    return {
        "metadata": {
            "adjustment_status": "verified",
            "currency": "USD",
            "analysis_price_basis": "total_return_adjusted",
        },
        "bars": [
            {
                "date": f"2026-09-{day}",
                "observed_at": f"2026-09-{day}T20:15:00+00:00",
                "valid": True,
                "adj_close": price,
            }
            for day, price in [(14, 100), (15, 105), (16, 110)]
        ],
    }


def test_price_response_uses_last_completed_pre_disclosure_close() -> None:
    result = event_price_reaction(datetime(2026, 9, 15, 12, tzinfo=UTC), True, market(), 1)
    assert result["baseline_date"] == "2026-09-14"
    assert result["last_date"] == "2026-09-15"
    assert round(result["return_pct"], 5) == 5
    assert result["industry_excess_return_pct"] is None


def test_unknown_timing_or_adjustments_do_not_invent_response() -> None:
    data = market()
    assert event_price_reaction(datetime.now(UTC), False, data, 5)["status"] == "unknown"
    data["metadata"]["adjustment_status"] = "unavailable"
    assert event_price_reaction(datetime.now(UTC), True, data, 5)["return_pct"] is None
