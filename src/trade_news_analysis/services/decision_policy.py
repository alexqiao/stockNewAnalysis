"""Separate event research, annual valuation and execution readiness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .decision_brief import build_decision_brief
from .judgment import _signal_blockers, _timestamp
from .market_research import number, quote_session_age
from .scoring import SIGNAL_TTL_MULTIPLIER, trading_sessions_since

POLICY_VERSION = "position-actions-2026-09-19.v2"


def _current_checks(
    checks: Sequence[Mapping[str, Any]], horizon: int, current: datetime, market_name: str | None,
) -> list[Mapping[str, Any]]:
    eligible = []
    for check in checks:
        stamp = _timestamp(check.get("first_disclosed_at") or check.get("occurred_at"))
        if check.get("status") not in {"complete", "partial"} or stamp is None:
            continue
        age = (quote_session_age(market_name, stamp, current) if market_name
               else trading_sessions_since(stamp, current))
        if stamp <= current + timedelta(minutes=5) and age is not None \
                and age <= horizon * SIGNAL_TTL_MULTIPLIER:
            eligible.append((stamp, check))
    eligible.sort(key=lambda row: row[0], reverse=True)
    return [check for _, check in eligible]


def build_decision_layers(
    signal: Mapping[str, Any] | None,
    pe: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
    market: Mapping[str, Any],
    risk: Mapping[str, Any],
    *,
    holding_status: str,
    horizon: int,
    now: datetime | None = None,
    market_name: str | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    event_gaps = _signal_blockers(signal, current)
    if market_name and signal and (stamp := _timestamp(signal.get("as_of"))):
        age = quote_session_age(market_name, stamp, current)
        event_gaps = [gap for gap in event_gaps if not gap.startswith("新闻信号已超过")]
        if age is None:
            event_gaps.append("交易日历不可用，无法确认新闻信号时效")
        elif age > 1:
            event_gaps.append("新闻信号已超过 1 个已收盘交易日，需更新")
    direction = str((signal or {}).get("direction", "neutral"))
    qualified = not event_gaps
    candidate = (
        "positive"
        if qualified and direction == "bullish"
        else ("negative" if qualified and direction == "bearish" else "observe")
    )
    labels = {"positive": "正向事件候选", "negative": "负向事件候选", "observe": "等待有效催化"}
    ids = set((signal or {}).get("evidence_event_ids") or [])
    relevant = [check for check in checks if check.get("event_id") in ids]
    timing_gaps = [
        f"确认《{check.get('title', '事件')}》的首次披露时间，排除旧消息重发"
        for check in relevant
        if not check.get("fact_time_verified") or not _timestamp(check.get("first_disclosed_at"))
    ]
    if ids and len(relevant) != len(ids):
        timing_gaps.append("补齐当前事件的原始资料与首次披露依据")
    price_gaps = list(market.get("blockers") or [])
    if market.get("status") != "ready" and not price_gaps:
        price_gaps.append("刷新量价与交易日数据")
    risk_gaps = list(risk.get("blockers") or [])
    if risk.get("status") not in {"ready", "stop_triggered"} and not risk_gaps:
        risk_gaps.append("填写仓位、风险预算与判断失效价")
    if risk.get("status") == "stop_triggered":
        risk_gaps.append("价格已触及失效条件，停止新增仓位并评估退出")
    reduction = number(risk.get("required_reduce_quantity"))
    buy_quantity = number(risk.get("max_buy_quantity"))
    if reduction is not None and reduction > 0:
        risk_gaps.append("当前仓位超过风险上限，先处理需减少的持仓")
    elif (
        candidate == "positive"
        and risk.get("status") == "ready"
        and (buy_quantity is None or buy_quantity <= 0)
    ):
        risk_gaps.append("新增股数上限为零或未确定，当前不能新增仓位")
    if holding_status == "unknown":
        risk_gaps.insert(0, "填写当前是否持仓")
    if holding_status == "short":
        risk_gaps.insert(0, "空头持仓不适用当前多头行动规则")
    risk_inputs = (risk.get("inputs") or {}).get("security") or {}
    quantity = number(risk_inputs.get("current_quantity"))
    weight = number(risk_inputs.get("current_weight"))
    if (
        holding_status == "flat"
        and ((quantity or 0) > 0 or (weight or 0) > 0)
        or holding_status == "long"
        and (quantity == 0 or weight == 0)
    ):
        risk_gaps.append("自选股持仓状态与实际股数或比例不一致，请统一确认")
    blockers = list(dict.fromkeys([*event_gaps, *timing_gaps, *price_gaps, *risk_gaps]))
    review = risk.get("position_review") or {}
    alerts = list(review.get("alerts") or []) if holding_status == "long" else []
    if review.get("status") in {"unavailable", "unsupported"}:
        blockers = list(dict.fromkeys([*blockers, *(review.get("blockers") or [])]))
    if alerts:
        blockers.append("持仓已触及当前风险参数，先处理持仓风险再评估新增仓位")
    ready = candidate != "observe" and not blockers
    action_code = "review"
    if holding_status == "short":
        next_action = "复核空头持仓，当前多头数量模型不适用"
    elif review.get("status") == "unavailable":
        next_action = "先核对真实持仓，暂缓仓位调整判断"
    elif holding_status == "unknown":
        next_action = "先确认持仓，再选择建仓或持仓方案"
    elif alerts:
        action_code = "reduce"
        next_action = (
            "已触及当前失效价，优先评估退出" if alerts[0]["code"] == "stop"
            else "暂停加仓，按当前风险上限评估减仓"
        )
    elif risk.get("status") == "stop_triggered" and holding_status == "long":
        next_action = "已触及失效价格，评估退出"
        action_code = "reduce"
    elif reduction is not None and reduction > 0:
        next_action = "仓位超出风险预算，先评估减仓"
        action_code = "reduce"
    elif candidate == "negative":
        next_action = "评估减仓 / 退出条件" if holding_status == "long" else "暂不建仓"
        action_code = "reduce" if holding_status == "long" else "avoid"
    elif candidate == "positive":
        next_action = "检查加仓条件" if holding_status == "long" else "检查建仓条件"
        action_code = "buy_candidate" if ready else "review"
    elif holding_status == "long":
        next_action = "暂缓加仓，优先管理现有持仓"
    else:
        next_action = "暂不建仓，先验证催化与入场条件"
    observations = list(review.get("observations") or [])
    if alerts:
        reason = " ".join(alert["detail"] for alert in alerts[:2])
    elif review.get("status") == "unavailable":
        reason = "；".join(review.get("blockers") or ["持仓数据尚未核对"])
    else:
        reason = " ".join(observations[:2])
        reason += {
            "positive": " 已有正向事件候选；是否新增仓位还需结合事件时效、价格和数量上限。",
            "negative": " 当前负向事件达到研究门槛，先核对持仓风险和退出条件。",
            "observe": " 当前新闻未达到明确方向门槛，尚不足以支持新增仓位。",
        }[candidate]
    current_checks = _current_checks(checks, horizon, current, market_name)
    lead = next((check for check in current_checks if check.get("missing_proof")
                 or check.get("catalysts") or check.get("falsifiers")), None)
    proof = (lead or {}).get("missing_proof") or (lead or {}).get("catalysts") or []
    invalidation = (lead or {}).get("falsifiers") or []
    conditions = {
        "add": (
            "先满足当前仓位和损失预算上限；"
            + (f"核实“{proof[0]}”；" if proof else "取得与本股相关、时间明确的一手催化证据；")
            + "重新评估形成正向候选，且行情、资金与数量上限均可用后，再考虑"
            + ("加仓。" if holding_status == "long" else "建仓。")
        ),
        "reduce": (
            "若沿用当前风险参数，先评估把风险降至上限以内；核对实时价格后决定数量。"
            if alerts else "触及当前失效价、仓位超限，或负面事实核实后，重新评估减仓或退出。"
        ),
        "recheck": {
            1: "下一交易日前复核仓位、失效价与有效报价；价格触及失效条件时立即复查。",
            5: "未来 5 个交易日跟踪首要催化及官方披露；事件发生、延期或窗口结束时重评。",
            20: "未来 20 个交易日复核经营假设与行业相对表现；假设被证伪时提前重评。",
        }[horizon],
    }
    if invalidation:
        conditions["review"] = f"{invalidation[0]}。出现时重新评估方向。"
    if holding_status == "flat":
        conditions["reduce"] = (
            "尚无多头持仓，无需减仓；出现负向事实或入场条件失效时继续暂缓建仓。"
        )
    elif holding_status == "short":
        reason = "真实持仓为空头，当前数量和退出规则仅支持多头，需单独评估空头风险。"
        conditions = {"add": "当前模型不提供空头加仓或转多方案。",
                      "reduce": "结合借券、保证金和空头风险单独评估回补条件。",
                      "recheck": "下一次调整仓位前核对真实持仓和空头风险。"}
    focus = {1: "先核对仓位风险与报价", 5: "跟踪催化，检查加减仓条件",
             20: "复核经营假设与行业表现"}[horizon]
    strategy: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "horizon": horizon,
        "candidate": {"code": candidate, "label": labels[candidate], "blockers": event_gaps},
        "execution": {
            "ready": ready,
            "label": "条件齐备，可人工评估" if ready else f"执行前待解决 {len(blockers)} 项",
            "next_action": next_action,
            "action_code": action_code,
            "reason": reason.strip(),
            "conditions": conditions,
            "horizon_focus": focus,
            "quote_as_of": (risk.get("quote") or {}).get("as_of"),
            "event_reference": ({"id": lead.get("event_id"), "title": lead.get("title")}
                                if lead else None),
            "blockers": blockers,
        },
        "valuation": {
            "label": pe.get("valuation_label", "年度估值待补充"),
            "status": pe.get("valuation_status", "unavailable"),
            "role": "年度盈利情景，仅作独立背景；不作为短期目标价或低估证明",
        },
        "pricing": {
            "market_neglect": None,
            "novelty_unpriced": None,
            "status": "unknown",
            "reason": "价格反应与发布前预期需逐项核对；涨跌本身不能证明尚未或已经充分定价",
        },
        "method_note": (
            "事件候选沿用既有方向门槛；模型置信度不是胜率。"
            "旧评分中的市场忽视与未定价维度保留作对照，尚未经历史样本校准。"
        ),
    }
    strategy["execution"]["brief"] = build_decision_brief(
        strategy["execution"], risk, holding_status=holding_status,
        candidate=candidate, lead=lead, horizon=horizon,
    )
    return strategy


def extend_action_plan(
    plan: dict[str, Any],
    strategy: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
    risk: Mapping[str, Any],
    *,
    now: datetime | None = None,
    market_name: str | None = None,
) -> dict[str, Any]:
    tasks = list(plan.get("tasks") or [])
    current = now or datetime.now(UTC)
    horizon = int(plan.get("horizon") or 5)
    eligible = _current_checks(checks, horizon, current, market_name)
    review = risk.get("position_review") or {}
    if review.get("alerts") and strategy["execution"].get("action_code") == "reduce":
        execution = strategy["execution"]
        tasks.append({
            "kind": "position_risk", "title": execution["next_action"],
            "detail": execution.get("reason", "核对当前持仓风险"),
            "when": "下一次调整仓位前；触及失效价时立即复核", "priority": -10,
            "on_pass": "若沿用当前参数，核对实时报价和可成交数量后评估降低风险。",
            "on_fail": "数据过期时先同步；参数不适用时修改参数并重新测算。",
            "source_kind": "research", "source_id": None, "source_label": "查看持仓与风险参数",
            "status": "pending", "status_label": "待处理",
        })
    for check in eligible[:2]:
        if not check.get("fact_time_verified"):
            tasks.append(
                {
                    "kind": "first_disclosure",
                    "title": "确认这条事实首次披露于何时",
                    "detail": "打开原始公告，核对事实首次披露时间及财务期间，排除旧消息重新传播。",
                    "reference": check.get("title"),
                    "when": "使用短期催化判断之前",
                    "on_pass": "按首次披露时间重新计算事件有效期",
                    "on_fail": "保留观察，暂停短期执行判断",
                    "source_kind": "event",
                    "source_id": check.get("event_id"),
                    "source_label": "核对事件原始时间",
                    "priority": 2,
                    "status": "pending",
                    "status_label": "待核实",
                }
            )
    if risk.get("blockers"):
        tasks.append(
            {
                "kind": "risk_budget",
                "title": "处理数量测算的数据缺口",
                "detail": "；".join(risk["blockers"]),
                "when": "决定新增、减仓或退出数量之前",
                "on_pass": "按可用资金、损失上限和流动性测算股数约束",
                "on_fail": "不生成无依据的仓位百分比或交易数量",
                "source_kind": "research",
                "source_id": None,
                "source_label": "查看持仓与测算缺口",
                "priority": 3,
                "status": "pending",
                "status_label": "待填写",
            }
        )
    tasks.sort(key=lambda task: task.get("priority", 50))
    quantity_note = (
        "当前约束下的数量已计算；下单前仍需核对实时报价和实际可成交量。"
        if risk.get("status") in {"ready", "stop_triggered"} else
        "股数尚未计算：" + "；".join((risk.get("blockers") or ["持仓与行情条件不足"])[:2])
    )
    return {**plan, "tasks": tasks, "quantity_note": quantity_note,
            "execution_readiness": strategy["execution"]}
