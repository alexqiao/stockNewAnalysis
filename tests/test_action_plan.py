from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trade_news_analysis.services.action_plan import build_action_plan
from trade_news_analysis.services.judgment import build_watchlist_judgment

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def check(**changes: Any) -> dict[str, Any]:
    return {
        "event_id": 71, "title": "公司披露新合同", "status": "complete",
        "occurred_at": NOW, "directions": {"1": "bearish", "5": "bullish", "20": "neutral"},
        "missing_proof": ["核对公告中的订单金额和交付时间"],
        "catalysts": ["合同首批交付"], "falsifiers": ["公司确认合同已取消"],
        **changes,
    }


def plan(
    events: list[dict[str, Any]] | None = None, *, horizon: int = 5,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_action_plan(
        action or {"code": "review", "holding_status": "long", "blockers": []},
        None, events if events is not None else [check()], None, horizon=horizon, now=NOW,
    )


def test_stale_quote_has_concrete_remediation_before_unverified_event() -> None:
    result = plan(action={
        "code": "review", "holding_status": "long", "blockers": ["报价已超过 3 个工作日，需刷新"]
    })
    task = result["tasks"][0]
    assert task["kind"] == "quote"
    assert task["source_kind"] == "valuation"
    assert task["when"] == "下一次交易决策前"
    event_task = next(item for item in result["tasks"] if item["kind"] == "evidence")
    assert event_task["detail"] == "核对公告中的订单金额和交付时间"
    assert event_task["source_id"] == 71
    assert event_task["status"] == "pending"


@pytest.mark.parametrize("horizon,expected,absent", [
    (1, "该利空假设失效", "重新评估减仓或退出"),
    (5, "该利好假设失效", "该利空假设失效"),
    (20, "撤回该研究假设", "重新评估减仓或退出"),
])
def test_invalidation_respects_the_direction_at_each_horizon(
    horizon: int, expected: str, absent: str,
) -> None:
    result = plan(horizon=horizon)
    task = next(item for item in result["tasks"] if item["kind"] == "invalidation")
    assert expected in task["on_pass"]
    assert absent not in task["on_pass"]
    assert "暂未证伪不代表已经证实" in task["on_fail"]


@pytest.mark.parametrize("changes", [
    {"status": "excluded"}, {"status": "pending"}, {"occurred_at": None},
    {"occurred_at": NOW + timedelta(minutes=6)},
    {"occurred_at": NOW - timedelta(days=40)}, {"event_id": True}, {"event_id": -1},
])
def test_invalid_or_unavailable_event_cannot_become_an_action_task(changes: dict[str, Any]) -> None:
    result = plan([check(**changes)])
    assert all(item["source_kind"] != "event" for item in result["tasks"])
    assert any(item["kind"] == "catalyst_gap" for item in result["tasks"])


def test_current_neutral_research_yields_verification_without_buy_signal() -> None:
    result = build_watchlist_judgment(
        None, {"status": "needs_data"}, holding_status="flat", now=NOW,
        event_checks=[check(directions={"5": "neutral"})],
    )
    assert result["action"]["code"] == "wait"
    tasks = result["action"]["plan"]["tasks"]
    assert any(item["source_id"] == 71 and item["kind"] == "evidence" for item in tasks)
    assert all(item["status"] == "pending" for item in tasks)


def test_larger_contribution_is_reviewed_first_without_duplicate_events() -> None:
    result = build_action_plan(
        {"holding_status": "long"},
        {"components": {"events": [
            {"event_id": 71, "contribution": -40}, {"event_id": 72, "contribution": 5}
        ]}},
        [check(event_id=72, occurred_at=NOW), check(occurred_at=NOW - timedelta(days=1)), check()],
        None, horizon=5, now=NOW,
    )
    proof_tasks = [item for item in result["tasks"] if item["kind"] == "evidence"]
    assert [item["source_id"] for item in proof_tasks] == [71, 72]


def test_bearish_risk_verification_is_not_buried_by_missing_quote() -> None:
    result = plan(horizon=1, action={
        "code": "reduce", "holding_status": "long", "blockers": [],
        "next_steps": ["缺少有效当前报价"],
    })
    assert result["tasks"][0]["kind"] == "evidence"
    assert result["tasks"][1]["kind"] == "invalidation"
    assert result["tasks"][2]["kind"] == "quote"


def test_empty_research_does_not_hide_another_event_with_concrete_checks() -> None:
    result = plan([
        check(event_id=73, missing_proof=[], catalysts=[], falsifiers=[]),
        check(event_id=72, missing_proof=[], catalysts=[], falsifiers=[]),
        check(event_id=71),
    ])
    assert any(task["source_id"] == 71 for task in result["tasks"])


def test_all_empty_research_explicitly_requests_a_verifiable_catalyst() -> None:
    result = plan([check(missing_proof=[], catalysts=[], falsifiers=[])])
    assert any(task["kind"] == "catalyst_gap" for task in result["tasks"])


def test_social_claim_keeps_verification_and_link_without_promotion() -> None:
    result = build_action_plan(
        {"holding_status": "flat"}, None, [],
        {"posts": [{"id": 81, "author": "researcher", "verification_needs": ["核对原始财报"]}]},
        horizon=5, now=NOW,
    )
    task = next(item for item in result["tasks"] if item["kind"] == "social")
    assert task["source_id"] == 81
    assert task["detail"] == "核对原始财报"
    assert task["status"] == "pending"
    assert "不作为买卖触发" in task["on_fail"]


def test_missing_calendar_and_risk_budget_are_explicit_not_invented() -> None:
    result = plan([])
    assert "未披露日期须保持未知" in result["tasks"][0]["detail"]
    assert "尚未核实" in result["calendar_note"]
    assert "暂不能计算买卖数量" in result["quantity_note"]


def test_invalid_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="1、5 或 20"):
        plan(horizon=2)


def test_partial_event_preserves_successful_security_verification_tasks() -> None:
    result = plan([check(status="partial")])
    tasks = [item for item in result["tasks"] if item["source_kind"] == "event"]
    assert tasks
    assert all(item["source_id"] == 71 for item in tasks)
