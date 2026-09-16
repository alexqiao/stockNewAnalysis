from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def fresh_signal(direction: str = "bullish", **changes: Any) -> dict[str, Any]:
    value = signal(direction)
    value.update(
        as_of=NOW,
        score=24.0 if direction == "bullish" else -24.0,
        decision_score=24.0 if direction == "bullish" else -24.0,
    )
    value.update(changes)
    return value


def fresh_pe(valuation_status: str = "below_range", **changes: Any) -> dict[str, Any]:
    value = pe_summary(valuation_status)
    value.update(
        current_price=100.0,
        price_as_of=NOW,
        price_provenance="auto",
        source_status="ready",
    )
    value.update(changes)
    return value


@pytest.mark.parametrize(
    ("holding_status", "direction", "valuation_status", "expected"),
    [
        ("flat", "bullish", "below_range", "buy_candidate"),
        ("flat", "bullish", "within_range", "buy_candidate"),
        ("flat", "bullish", "above_range", "wait"),
        ("long", "bullish", "below_range", "hold"),
        ("long", "bullish", "within_range", "hold"),
        ("long", "bullish", "above_range", "review"),
        ("flat", "bearish", "below_range", "avoid"),
        ("long", "bearish", "below_range", "reduce"),
        ("long", "bearish", "within_range", "reduce"),
        ("long", "bearish", "above_range", "sell_candidate"),
    ],
)
def test_position_changes_action(
    holding_status: str, direction: str, valuation_status: str, expected: str
) -> None:
    result = build_watchlist_judgment(
        fresh_signal(direction),
        fresh_pe(valuation_status),
        holding_status=holding_status,
        now=NOW,
    )

    assert result["action"]["code"] == expected
    assert result["action"]["holding_status"] == holding_status
    assert result["action"]["entry_conditions"]
    assert result["action"]["exit_conditions"]
    assert result["action"]["as_of"] == NOW


def test_unknown_holding_shows_both_scenarios_without_assuming_position() -> None:
    action = build_watchlist_judgment(fresh_signal(), fresh_pe(), now=NOW)["action"]

    assert action["code"] == "review"
    assert action["holding_status"] == "unknown"
    assert action["flat_action"]["code"] == "buy_candidate"
    assert action["long_action"]["code"] == "hold"
    assert "持仓状态未填写" in action["blockers"]
    assert "尚未回测" in action["policy_note"]
    assert "不是胜率" in action["policy_note"]
    assert "不是短期目标价或止损价" in action["policy_note"]


@pytest.mark.parametrize(
    "changes",
    [
        {"as_of": None},
        {"as_of": "invalid"},
        {"as_of": NOW - timedelta(days=4)},
        {"as_of": NOW + timedelta(minutes=6)},
        {"evidence_event_ids": []},
        {"confidence": 0.49},
        {"confidence": float("nan")},
        {"decision_score": 19.99},
        {"decision_score": float("inf")},
        {"decision_score": -24.0},
        {"conflict": 0.35},
        {"conflict": None},
        {"conflict": float("nan")},
    ],
)
def test_signal_gaps_prevent_buying(changes: dict[str, Any]) -> None:
    action = build_watchlist_judgment(
        fresh_signal(**changes), fresh_pe(), holding_status="flat", now=NOW
    )["action"]

    assert action["code"] == "wait"
    assert action["blockers"]
    assert action["next_steps"]


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "needs_input"},
        {"status": "not_applicable"},
        {"current_price": None},
        {"price_as_of": None, "updated_at": NOW},
        {"price_as_of": NOW - timedelta(days=6)},
        {"price_as_of": NOW + timedelta(minutes=6)},
        {"source_status": "error", "updated_at": NOW},
        {"valuation_year": 2025},
    ],
)
def test_valuation_gaps_prevent_buying(changes: dict[str, Any]) -> None:
    action = build_watchlist_judgment(
        fresh_signal(), fresh_pe(**changes), holding_status="flat", now=NOW
    )["action"]

    assert action["code"] == "wait"
    assert action["blockers"]


def test_neutral_high_conflict_is_reported_before_neutral_direction() -> None:
    result = build_watchlist_judgment(
        fresh_signal("neutral", decision_score=0, conflict=1.0),
        fresh_pe(),
        holding_status="long",
        now=NOW,
    )

    assert result["news"]["state"] == "mixed"
    assert result["label"] == "新闻证据冲突"
    assert result["action"]["code"] == "review"


@pytest.mark.parametrize("valuation", [{"status": "needs_input"}, fresh_pe(price_as_of=None)])
def test_missing_valuation_does_not_suppress_strong_bearish_risk(
    valuation: dict[str, Any],
) -> None:
    action = build_watchlist_judgment(
        fresh_signal("bearish"), valuation, holding_status="long", now=NOW
    )["action"]

    assert action["code"] == "reduce"
    assert action["blockers"] == []
    assert action["next_steps"]


def test_stale_high_valuation_cannot_escalate_reduction_to_sell() -> None:
    action = build_watchlist_judgment(
        fresh_signal("bearish"),
        fresh_pe("above_range", price_as_of=NOW - timedelta(days=7)),
        holding_status="long",
        now=NOW,
    )["action"]

    assert action["code"] == "reduce"


@pytest.mark.parametrize("other_is_fresh", [True, False])
def test_only_current_strong_opposite_horizon_blocks_entry(other_is_fresh: bool) -> None:
    other = fresh_signal("bearish", horizon=20)
    if not other_is_fresh:
        other["as_of"] = NOW - timedelta(days=7)
    action = build_watchlist_judgment(
        fresh_signal(),
        fresh_pe(),
        holding_status="flat",
        signals_by_horizon={"20": other},
        now=NOW,
    )["action"]

    assert action["code"] == ("wait" if other_is_fresh else "buy_candidate")


def test_opposite_horizon_does_not_suppress_existing_holding_risk() -> None:
    action = build_watchlist_judgment(
        fresh_signal("bearish"),
        fresh_pe("above_range"),
        holding_status="long",
        signals_by_horizon={"20": fresh_signal("bullish", horizon=20)},
        now=NOW,
    )["action"]

    assert action["code"] == "sell_candidate"


def test_social_opinion_adds_verification_without_overriding_event_direction() -> None:
    social = {
        "post_count": 1,
        "conflicts": ["相关博主观点偏空"],
        "verification_needs": ["核实博主引用的订单数据"],
    }
    result = build_watchlist_judgment(
        fresh_signal(),
        fresh_pe(),
        holding_status="flat",
        social_context=social,
        now=NOW,
    )

    assert result["social"] == social
    assert result["action"]["code"] == "buy_candidate"
    assert "核实博主引用的订单数据" in result["action"]["next_steps"]
    assert any("社交观点不直接增加" in step for step in result["action"]["next_steps"])


def test_social_opinion_alone_cannot_create_a_buy_signal() -> None:
    action = build_watchlist_judgment(
        None,
        fresh_pe(),
        holding_status="flat",
        social_context={"post_count": 8, "stance_counts": {"bullish": 8}},
        now=NOW,
    )["action"]

    assert action["code"] == "wait"


def test_freshness_thresholds_and_naive_sqlite_timestamps() -> None:
    action = build_watchlist_judgment(
        fresh_signal(
            as_of=(NOW - timedelta(days=1)).replace(tzinfo=None),
            confidence=0.5,
            decision_score=20,
            conflict=0.349,
        ),
        fresh_pe(price_as_of=(NOW - timedelta(days=5)).isoformat()),
        holding_status="flat",
        now=NOW.replace(tzinfo=None),
    )["action"]

    assert action["code"] == "buy_candidate"


def test_manually_confirmed_quote_is_independent_of_automatic_refresh_failure() -> None:
    action = build_watchlist_judgment(
        fresh_signal(),
        fresh_pe(price_provenance="manual", source_status="error"),
        holding_status="flat",
        now=NOW,
    )["action"]

    assert action["code"] == "buy_candidate"


def test_withdrawn_evidence_does_not_reappear_as_key_event() -> None:
    result = build_watchlist_judgment(
        fresh_signal(evidence_event_ids=[]), fresh_pe(), {7: "已经撤回的利好"}, now=NOW
    )

    assert result["key_events"] == []
    assert result["action"]["flat_action"]["code"] == "wait"
