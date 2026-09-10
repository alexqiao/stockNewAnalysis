from __future__ import annotations

from typing import Any

import pytest

from trade_news_analysis.services.judgment import build_watchlist_judgment


def signal(
    direction: str = "bullish", *, conflict: float = 0.1
) -> dict[str, Any]:
    return {
        "horizon": 5,
        "score": 24.0,
        "decision_score": 24.0,
        "direction": direction,
        "confidence": 0.7,
        "conflict": conflict,
        "evidence_event_ids": [7],
        "components": {
            "events": [
                {
                    "event_id": 7,
                    "direction": direction,
                    "contribution": 24.0,
                }
            ]
        },
    }


def pe_summary(valuation_status: str = "below_range") -> dict[str, Any]:
    labels = {
        "below_range": "当前价偏低",
        "within_range": "当前价位于合理区间",
        "above_range": "当前价偏高",
    }
    return {
        "status": "ready",
        "valuation_status": valuation_status,
        "valuation_label": labels[valuation_status],
        "valuation_year": 2027,
        "current_implied_pe": 18.0,
        "pe_low": 20.0,
        "pe_high": 30.0,
    }


@pytest.mark.parametrize(
    ("direction", "valuation_status", "expected_label"),
    [
        ("bullish", "below_range", "催化与估值共振"),
        ("bullish", "above_range", "催化偏多，估值承压"),
        ("bearish", "above_range", "新闻与估值双重承压"),
        ("neutral", "below_range", "估值有空间，催化不足"),
    ],
)
def test_combined_judgment_matrix(
    direction: str, valuation_status: str, expected_label: str
) -> None:
    result = build_watchlist_judgment(
        signal(direction), pe_summary(valuation_status), {7: "公司获得新增订单"}
    )

    assert result["label"] == expected_label
    assert "18.0x" in result["summary"]
    assert result["key_events"] == ["公司获得新增订单"]


def test_combined_judgment_exposes_missing_pe_inputs() -> None:
    result = build_watchlist_judgment(
        signal(),
        {"status": "needs_input", "valuation_status": "unavailable"},
    )

    assert result["status"] == "incomplete"
    assert result["label"] == "新闻偏多，PE 估值待补"
    assert "先补齐盈利预测和 PE 区间" in result["summary"]


def test_high_conflict_downgrades_directional_signal() -> None:
    result = build_watchlist_judgment(
        signal("bullish", conflict=0.6), pe_summary("below_range")
    )

    assert result["status"] == "caution"
    assert result["label"] == "新闻证据冲突"
    assert result["stance"] == "等待验证"


def test_macro_context_is_kept_separate_from_directional_judgment() -> None:
    macro = {
        "event_id": 12,
        "title": "美国非农就业增加 16.2 万",
        "direction_note": "缺少市场一致预期，暂不计入个股方向",
    }

    result = build_watchlist_judgment(
        None, {"status": "needs_data"}, macro_context=macro
    )

    assert result["macro"] == macro
    assert result["label"] == "新闻与估值数据不足"
