"""Bounded presentation of existing decisions; full evidence remains in execution."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .market_research import number

BRIEF_LIMITS = {"action": 40, "reason": 90, "next_step": 90, "change_when": 80}


def _whole(value: Any, limit: int, fallback: str) -> str:
    text = " ".join(str(value or "").split())
    # Cutting a condition can remove a negation or its qualifying clause.
    return text if text and len(text) <= limit else fallback


def build_decision_brief(
    execution: Mapping[str, Any], risk: Mapping[str, Any], *,
    holding_status: str, candidate: str, lead: Mapping[str, Any] | None, horizon: int,
) -> dict[str, Any]:
    inputs = risk.get("inputs") or {}
    values = inputs.get("security") or {}
    sources = inputs.get("field_sources") or {}
    review = risk.get("position_review") or {}
    alerts = review.get("alerts") or []
    reference = execution.get("event_reference")
    destination = "event" if reference and reference.get("id") else "calendar"
    proof = (lead or {}).get("missing_proof") or (lead or {}).get("catalysts") or []
    evidence_step = _whole(
        f"核对：{proof[0]}" if proof else "", 76,
        "打开关联事件，逐项核对原文证据与披露日期。" if lead else
        "打开研究工作台，检查下一项公司公告或财报安排。",
    )
    reason = {
        "positive": "出现正向事件候选；仍需核对执行条件。",
        "negative": "出现负向事件候选；优先核对原文和持仓风险。",
        "observe": "当前新闻未达到明确方向门槛，缺少新增仓位依据。",
    }[candidate]
    next_step = evidence_step
    change = ("一手证据形成正向候选，且资金、报价和风险条件齐备后，再评估新增仓位。"
              if candidate != "negative" else "负面事实被澄清或经营假设变化时，重评方向。")

    def basis(field: str) -> str:
        return "默认" if sources.get(field, {}).get("kind") == "default" else "设置"

    if holding_status == "short":
        reason = "当前为空头；多头风险和数量规则不适用。"
        next_step = "核对借券、保证金和回补条件，单独评估空头风险。"
        change = "空头风险或真实持仓变化时重新评估。"
        destination = "holdings"
    elif review.get("status") == "unavailable":
        reason = _whole("；".join(review.get("blockers") or []), 90,
                        "持仓数据尚不可用于当前判断；展开查看缺口。")
        next_step = ("在自选页同步真实持仓，成功后重新评估仓位。"
                     if (inputs.get("holdings_source") or {}).get("active") else
                     "在研究工作台核对实际股数与持仓比例。")
        change = "持仓数据恢复有效且一致后，重新检查仓位上限与失效价。"
        destination = "holdings" if (inputs.get("holdings_source") or {}).get("active") \
            else "research"
    elif holding_status == "unknown":
        reason = "持仓状态未确认，无法区分建仓与持仓调整。"
        next_step = "在自选页同步持仓，或填写当前是否持仓。"
        change = "持仓状态确认后，重新生成对应行动。"
        destination = "holdings"
    elif holding_status == "long" and alerts:
        alert = alerts[0]
        reason = _whole(alert.get("detail"), 90, "持仓触及当前风险参数；展开核对具体数值。")
        code = alert.get("code")
        weight, limit = number(values.get("current_weight")), number(values.get("max_weight"))
        loss, budget = number(review.get("loss_to_stop_pct")), number(values.get("risk_budget_pct"))
        stop = number(values.get("stop_price"))
        price = number((risk.get("quote") or {}).get("price"))
        if code == "concentration" and weight is not None and limit is not None:
            reason = (f"仓位 {weight:.2%}，高于{basis('max_weight')}上限 {limit:.2%}，"
                      f"相差 {(weight - limit) * 100:.2f} 个百分点。")
        elif code == "loss_budget" and loss is not None and budget is not None:
            stop_basis = (
                f"{basis('stop_price')}失效价 {stop:g}" if stop is not None else "当前失效价"
            )
            reason = (f"按{stop_basis}及费用估算，情景损失 {loss:.2%}，"
                      f"超过{basis('risk_budget_pct')}预算 {budget:.2%}；非实际亏损保证。")
        elif code == "stop" and price is not None and stop is not None:
            reason = f"最近报价 {price:g}，已不高于{basis('stop_price')}失效价 {stop:g}。"
        next_step = ("先核对实时报价；若仍触及失效价，优先评估退出。" if code == "stop" else
                     "先核对风险参数；若沿用这些上限，评估降低仓位风险。")
        change = "风险恢复至所用上限内，再结合有效催化评估是否新增仓位。"
        destination = "research"
    else:
        # Show the next resolvable gate, rather than a repeated checklist of every gate.
        blockers = execution.get("blockers") or []
        if blockers and candidate != "observe":
            reason += " " + _whole(blockers[0], 55, "尚有执行条件未满足，展开核对。")
        if lead and not lead.get("fact_time_verified"):
            next_step = "打开关联事件，先确认原公告首次披露日期，再核对其中的首项证据。"
        elif candidate != "observe" and risk.get("blockers"):
            next_step = _whole(f"先解决数量测算缺口：{risk['blockers'][0]}", 90,
                               "打开风险测算，先处理列出的第一项数据缺口。")
            destination = "research"
        elif execution.get("ready"):
            next_step = "打开数量测算，核对实时报价与股数上限后人工评估。"
            destination = "research"
        if holding_status == "long":
            stop = number(values.get("stop_price"))
            if review.get("quote_usable") and stop is not None:
                change = (f"价格触及{basis('stop_price')}失效价 {stop:g} 时评估退出；"
                          "新增仓位须等正向证据及执行条件齐备。")

    quantity = ("股数待测算" if risk.get("status") not in {"ready", "stop_triggered"}
                else "股数上限见测算；下单前核对报价")
    result = {
        "action": _whole(execution.get("next_action"), 40, "先核对当前行动条件"),
        "reason": _whole(reason, 90, "当前资料存在待核对条件，完整依据见展开详情。"),
        "next_step": _whole(next_step, 90, "打开完整核验步骤，先处理排序第一的待办。"),
        "change_when": _whole(change, 80, "当前证据或持仓风险变化时重新评估。"),
        "review_when": {1: "下一交易日前复核", 5: "事件披露或 5 个交易日后复核",
                        20: "假设变化或 20 个交易日后复核"}[horizon],
        "quantity_note": quantity,
        "holdings_note": (inputs.get("holdings_source") or {}).get("note"),
        "destination": destination,
        "event_id": reference.get("id") if destination == "event" and reference else None,
        "additional_risks": max(0, len(alerts) - 1) if holding_status == "long" else 0,
    }
    return result
