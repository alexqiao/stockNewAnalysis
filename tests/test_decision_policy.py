from datetime import UTC, datetime, timedelta

from trade_news_analysis.services.decision_policy import build_decision_layers, extend_action_plan

NOW = datetime(2026, 9, 16, 16, tzinfo=UTC)


def signal(direction: str = "bullish") -> dict:
    return {
        "as_of": NOW,
        "direction": direction,
        "confidence": 0.8,
        "conflict": 0.1,
        "decision_score": 40 if direction == "bullish" else -40,
        "evidence_event_ids": [1],
    }


def test_event_candidate_does_not_depend_on_annual_pe() -> None:
    checks = [{"event_id": 1, "fact_time_verified": True, "first_disclosed_at": NOW}]
    result = build_decision_layers(
        signal(),
        {"status": "not_applicable"},
        checks,
        {"status": "ready"},
        {"status": "ready", "max_buy_quantity": 100},
        holding_status="flat",
        horizon=5,
        now=NOW,
    )
    assert result["candidate"]["code"] == "positive"
    assert result["execution"]["ready"] is True
    assert result["pricing"]["market_neglect"] is None


def test_new_report_with_unknown_fact_time_cannot_be_execution_ready() -> None:
    result = build_decision_layers(
        signal(),
        {},
        [{"event_id": 1, "title": "旧订单重发"}],
        {"status": "ready"},
        {"status": "ready", "max_buy_quantity": 100},
        holding_status="flat",
        horizon=5,
        now=NOW,
    )
    assert result["candidate"]["code"] == "positive"
    assert not result["execution"]["ready"]
    assert "首次披露" in result["execution"]["blockers"][0]


def test_missing_event_records_and_risk_inputs_block_readiness() -> None:
    result = build_decision_layers(
        signal(),
        {},
        [],
        {},
        {},
        holding_status="unknown",
        horizon=5,
        now=NOW,
    )
    assert not result["execution"]["ready"]
    assert len(result["execution"]["blockers"]) >= 4


def test_stop_trigger_overrides_positive_entry_and_remains_specific() -> None:
    result = build_decision_layers(
        signal(),
        {},
        [{"event_id": 1, "fact_time_verified": True, "first_disclosed_at": NOW}],
        {"status": "ready"},
        {"status": "stop_triggered"},
        holding_status="long",
        horizon=5,
        now=NOW,
    )
    assert not result["execution"]["ready"]
    assert "退出" in result["execution"]["next_action"]


def test_bearish_risk_is_clear_for_existing_and_flat_positions() -> None:
    for holding, action in [("long", "评估减仓 / 退出条件"), ("flat", "暂不建仓")]:
        result = build_decision_layers(
            signal("bearish"),
            {},
            [],
            {},
            {},
            holding_status=holding,
            horizon=20,
            now=NOW,
        )
        assert result["execution"]["next_action"] == action


def test_risk_reduction_prevents_positive_addition() -> None:
    result = build_decision_layers(
        signal(),
        {},
        [{"event_id": 1, "fact_time_verified": True, "first_disclosed_at": NOW}],
        {"status": "ready"},
        {"status": "ready", "max_buy_quantity": 0, "required_reduce_quantity": 500},
        holding_status="long",
        horizon=5,
        now=NOW,
    )
    assert not result["execution"]["ready"]
    assert "减仓" in result["execution"]["next_action"]


def test_expired_research_is_not_reintroduced_as_first_disclosure_task() -> None:
    result = extend_action_plan(
        {"horizon": 5, "tasks": []},
        {"execution": {}},
        [{"event_id": 1, "occurred_at": NOW - timedelta(days=180), "status": "complete"}],
        {},
        now=NOW,
    )
    assert result["tasks"] == []


def test_position_risk_is_actionable_without_manufacturing_event_or_quantity() -> None:
    risk = {"status": "blocked", "blockers": ["现金未知"], "position_review": {
        "status": "attention", "alerts": [{"code": "concentration",
            "detail": "当前仓位 25%，超过默认上限 20%；若沿用该上限，评估降低集中度。"}],
    }}
    result = build_decision_layers(None, {}, [], {}, risk,
                                   holding_status="long", horizon=5, now=NOW)
    assert result["candidate"]["code"] == "observe"
    assert not result["execution"]["ready"]
    assert result["execution"]["action_code"] == "reduce"
    assert "默认上限" in result["execution"]["reason"]
    assert "暂停加仓" in result["execution"]["next_action"]
    plan = extend_action_plan({"tasks": [{"kind": "evidence", "priority": 25}]},
                              result, [], risk, now=NOW)
    assert [task["kind"] for task in plan["tasks"]] == [
        "position_risk", "risk_budget", "evidence",
    ]
    assert "现金未知" in plan["quantity_note"]


def test_neutral_direction_distinguishes_positions_and_recheck_horizons() -> None:
    focuses = set()
    for horizon in (1, 5, 20):
        for holding in ("flat", "long", "unknown", "short"):
            result = build_decision_layers(None, {}, [], {}, {},
                holding_status=holding, horizon=horizon, now=NOW)
            execution = result["execution"]
            assert not execution["ready"]
            assert "保留观察，等待可验证事件" not in execution["next_action"]
            expected = {"flat": "暂不建仓", "long": "暂缓加仓",
                        "unknown": "先确认持仓", "short": "空头"}[holding]
            assert expected in execution["next_action"]
            if holding == "short":
                assert "不提供空头加仓" in execution["conditions"]["add"]
            if holding == "flat":
                assert "无需减仓" in execution["conditions"]["reduce"]
            focuses.add(execution["horizon_focus"])
    assert len(focuses) == 3


def test_conditional_actions_reference_current_evidence_and_ignore_expired_checks() -> None:
    checks = [
        {"event_id": 1, "occurred_at": NOW - timedelta(days=180), "status": "complete",
         "missing_proof": ["旧事件数据"], "falsifiers": ["旧风险"]},
        {"event_id": 2, "occurred_at": NOW, "status": "complete", "title": "新增合同",
         "missing_proof": ["合同正式披露金额"], "falsifiers": ["客户取消合同"]},
    ]
    result = build_decision_layers(None, {}, checks, {}, {},
                                   holding_status="long", horizon=5, now=NOW)
    execution = result["execution"]
    assert "合同正式披露金额" in execution["conditions"]["add"]
    assert "客户取消合同" in execution["conditions"]["review"]
    assert "旧事件" not in str(execution)
    assert execution["event_reference"]["id"] == 2
    assert not execution["ready"]


def test_bullish_revision_of_neutral_thesis_is_not_a_sell_trigger() -> None:
    result = build_decision_layers(None, {}, [{
        "event_id": 2, "occurred_at": NOW, "status": "complete",
        "falsifiers": ["若净息差显著扩大，则中性判断上修"],
    }], {}, {}, holding_status="long", horizon=5, now=NOW)
    conditions = result["execution"]["conditions"]
    assert "中性判断上修" in conditions["review"]
    assert "净息差" not in conditions["reduce"]


def test_unavailable_holdings_override_entry_direction() -> None:
    result = build_decision_layers(signal(), {}, [], {"status": "ready"}, {
        "status": "blocked", "position_review": {
            "status": "unavailable", "blockers": ["持仓快照时间异常"], "alerts": [],
        }}, holding_status="long", horizon=5, now=NOW)
    assert not result["execution"]["ready"]
    assert result["execution"]["action_code"] == "review"
    assert "先核对真实持仓" in result["execution"]["next_action"]
    assert "持仓快照时间异常" in result["execution"]["reason"]


def test_partial_event_can_supply_successful_security_conditions() -> None:
    result = build_decision_layers(
        signal(), {},
        [{"event_id": 1, "status": "partial", "title": "已验证本股",
          "fact_time_verified": True, "first_disclosed_at": NOW,
          "missing_proof": ["核实收入金额"]}],
        {"status": "ready"}, {"status": "ready", "max_buy_quantity": 100},
        holding_status="flat", horizon=5, now=NOW,
    )
    assert result["execution"]["event_reference"] == {"id": 1, "title": "已验证本股"}
    assert "核实收入金额" in result["execution"]["conditions"]["add"]
