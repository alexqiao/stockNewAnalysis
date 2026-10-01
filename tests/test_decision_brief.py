from datetime import UTC, datetime

from trade_news_analysis.services.decision_brief import build_decision_brief

NOW = datetime(2026, 9, 21, 8, tzinfo=UTC)


def execution(**changes: object) -> dict[str, object]:
    return {
        "next_action": "暂停加仓，按当前风险上限评估减仓",
        "reason": "很长的详细理由应该在简报中被压缩，而完整内容保留在展开详情中。",
        "ready": False,
        "blockers": ["现金资料尚未提供", "行业资料尚未完整"],
        "event_reference": None,
        **changes,
    }


def risk() -> dict[str, object]:
    return {
        "status": "blocked",
        "inputs": {
            "security": {
                "current_weight": 0.235,
                "max_weight": 0.2,
                "risk_budget_pct": 0.01,
                "stop_price": 15.2,
            },
            "field_sources": {
                "max_weight": {"kind": "default"},
                "risk_budget_pct": {"kind": "default"},
                "stop_price": {"kind": "default"},
            },
            "holdings_source": {"active": True},
        },
        "quote": {"price": 16.0},
        "position_review": {
            "status": "attention",
            "alerts": [{
                "code": "concentration",
                "detail": "当前仓位 23.50%，超过默认上限 20.00%。",
            }],
            "loss_to_stop_pct": None,
        },
    }


def test_brief_is_bounded_and_points_to_first_resolvable_action() -> None:
    result = build_decision_brief(
        execution(), risk(), holding_status="long", candidate="observe",
        lead=None, horizon=5,
    )
    assert len(result["action"]) <= 40
    assert len(result["reason"]) <= 90
    assert len(result["next_step"]) <= 90
    assert len(result["change_when"]) <= 80
    assert "23.50%" in result["reason"]
    assert "核对风险参数" in result["next_step"]
    assert "5 个交易日" in result["review_when"]


def test_brief_uses_event_link_when_an_actionable_evidence_item_exists() -> None:
    result = build_decision_brief(
        execution(event_reference={"id": 12, "title": "正式披露"}),
        {"status": "ready", "inputs": {}, "position_review": {},
         "quote": {}},
        holding_status="flat", candidate="positive",
        lead={"event_id": 12, "missing_proof": ["合同金额"], "fact_time_verified": True},
        horizon=1,
    )
    assert result["destination"] == "event"
    assert result["event_id"] == 12
    assert result["next_step"] == "核对：合同金额"
    assert result["quantity_note"] == "股数上限见测算；下单前核对报价"


def test_brief_does_not_turn_short_position_into_long_action() -> None:
    result = build_decision_brief(
        execution(next_action="复核空头持仓，当前多头数量模型不适用"),
        risk(), holding_status="short", candidate="positive", lead=None, horizon=20,
    )
    assert result["destination"] == "holdings"
    assert "空头" in result["reason"]
    assert "借券" in result["next_step"]
    assert "20 个交易日" in result["review_when"]
