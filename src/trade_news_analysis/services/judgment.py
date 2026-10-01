"""Combine event-driven signals and PE valuation into an auditable watchlist view."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .action_plan import build_action_plan
from .scoring import trading_sessions_since

ACTION_POLICY_VERSION = "2026-09-16.v2"
MIN_ACTION_CONFIDENCE = 0.5
MIN_ACTION_SCORE = 20.0
MAX_ACTION_CONFLICT = 0.35
SIGNAL_MAX_AGE_WORKDAYS = 1
PRICE_MAX_AGE_WORKDAYS = 3
ACTION_POLICY_NOTE = (
    "行动条件为尚未回测校准的启发式规则：置信度至少 50%（不是胜率）、"
    "决策分绝对值至少 20、冲突度低于 35%；信号最多 1 个工作日、"
    "报价最多 3 个工作日，工作日近似不包含交易所节假日。"
    "PE 区间是年度盈利假设情景，不是短期目标价或止损价。"
)

NEWS_LABELS = {
    "bullish": "新闻偏多",
    "bearish": "新闻偏空",
    "neutral": "新闻未形成方向",
    "mixed": "新闻证据冲突",
    "unavailable": "暂无新闻信号",
}

COMBINED_MATRIX = {
    ("bullish", "below_range"): (
        "supportive",
        "催化与估值共振",
        "重点验证",
        "新闻催化和估值空间方向一致，后续重点验证盈利能否按假设兑现",
    ),
    ("bullish", "within_range"): (
        "supportive",
        "新闻偏多，估值合理",
        "持续跟踪",
        "新闻提供正向催化，当前估值仍在假设区间内",
    ),
    ("bullish", "above_range"): (
        "caution",
        "催化偏多，估值承压",
        "谨慎验证",
        "新闻方向积极，但当前价格已经高于 PE 假设区间，需防范预期透支",
    ),
    ("bearish", "below_range"): (
        "caution",
        "估值偏低，新闻承压",
        "等待拐点",
        "估值提供一定缓冲，但负面新闻可能继续压制盈利预期",
    ),
    ("bearish", "within_range"): (
        "risk",
        "新闻承压，估值无缓冲",
        "降低优先级",
        "新闻信号偏空，当前估值也没有提供明显安全边际",
    ),
    ("bearish", "above_range"): (
        "risk",
        "新闻与估值双重承压",
        "高风险观察",
        "负面新闻与偏高估值相互强化，需等待证据和盈利预期改善",
    ),
    ("neutral", "below_range"): (
        "balanced",
        "估值有空间，催化不足",
        "等待催化",
        "PE 假设显示估值空间，但新闻尚未确认兑现路径",
    ),
    ("neutral", "within_range"): (
        "balanced",
        "暂未形成方向优势",
        "一般观察",
        "新闻缺少明确方向，当前估值也处于假设区间中部",
    ),
    ("neutral", "above_range"): (
        "caution",
        "估值偏高，催化不足",
        "谨慎观察",
        "新闻尚未形成正向催化，当前估值已经高于假设区间",
    ),
}


def _number(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool) and isfinite(value):
        return float(value)
    return 0.0


def _news_state(signal: Mapping[str, Any] | None) -> str:
    if signal is None:
        return "unavailable"
    direction = str(signal.get("direction") or "neutral")
    conflict = _number(signal.get("conflict"))
    if conflict >= 0.5:
        return "mixed"
    return direction if direction in {"bullish", "bearish", "neutral"} else "neutral"


def _news_detail(signal: Mapping[str, Any] | None, state: str) -> str:
    if signal is None:
        return "尚无可用的事件新闻信号"
    horizon = int(_number(signal.get("horizon")))
    score = _number(signal.get("decision_score", signal.get("score")))
    confidence = _number(signal.get("confidence")) * 100
    conflict = _number(signal.get("conflict")) * 100
    prefix = NEWS_LABELS[state]
    return (
        f"{horizon} 日{prefix}：决策分 {score:+.1f}，"
        f"置信度 {confidence:.0f}%，冲突度 {conflict:.0f}%"
    )


def _valuation_detail(pe_summary: Mapping[str, Any]) -> str:
    status = str(pe_summary.get("status") or "needs_data")
    valuation_status = str(pe_summary.get("valuation_status") or "unavailable")
    if status != "ready" or valuation_status == "unavailable":
        return {
            "needs_input": "PE 基础数据已就绪，但盈利增速和估值区间尚未填写完整",
            "not_applicable": "预测 EPS 非正，当前 PE 方法不适用",
        }.get(status, "PE 基础数据或盈利假设不足")

    year = pe_summary.get("valuation_year")
    implied_pe = pe_summary.get("current_implied_pe")
    pe_low = pe_summary.get("pe_low")
    pe_high = pe_summary.get("pe_high")
    label = str(pe_summary.get("valuation_label") or "暂无法判断")
    if all(value is not None for value in (implied_pe, pe_low, pe_high)):
        return (
            f"基于 {year or '预测年度'} 年盈利，当前隐含 PE "
            f"{_number(implied_pe):.1f}x，对比 {_number(pe_low):.1f}x–"
            f"{_number(pe_high):.1f}x "
            f"假设区间：{label}"
        )
    return f"基于当前 PE 假设：{label}"


def _key_events(
    signal: Mapping[str, Any] | None, event_titles: Mapping[int, str]
) -> list[str]:
    if signal is None:
        return []
    allowed = {int(value) for value in signal.get("evidence_event_ids") or []}
    if not allowed:
        return []
    raw_events = (signal.get("components") or {}).get("events")
    ranked_ids: list[int] = []
    if isinstance(raw_events, list):
        ranked = sorted(
            (item for item in raw_events if isinstance(item, dict)),
            key=lambda item: abs(_number(item.get("contribution"))),
            reverse=True,
        )
        ranked_ids = [
            int(item["event_id"])
            for item in ranked
            if item.get("event_id") is not None
            and not item.get("expired")
            and int(item["event_id"]) in allowed
        ]
    ranked_ids.extend(sorted(allowed))
    result: list[str] = []
    for event_id in ranked_ids:
        title = event_titles.get(event_id)
        if title and title not in result:
            result.append(title)
        if len(result) == 2:
            break
    return result


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _freshness_issue(value: Any, now: datetime, maximum: int, label: str) -> str | None:
    timestamp = _timestamp(value)
    if timestamp is None:
        return f"{label}时间缺失，需刷新或重新确认"
    if timestamp > now + timedelta(minutes=5):
        return f"{label}时间异常，需核对后刷新"
    if trading_sessions_since(timestamp, now) > maximum:
        return f"{label}已超过 {maximum} 个工作日，需刷新"
    return None


def _signal_blockers(signal: Mapping[str, Any] | None, now: datetime) -> list[str]:
    if signal is None:
        return ["尚无新闻信号，需补充有效事件证据"]
    blockers = []
    freshness = _freshness_issue(
        signal.get("as_of"), now, SIGNAL_MAX_AGE_WORKDAYS, "新闻信号"
    )
    if freshness:
        blockers.append(freshness)
    if not signal.get("evidence_event_ids"):
        blockers.append("缺少当前有效的事件证据")
    direction = signal.get("direction")
    score = _number(signal.get("decision_score", signal.get("score")))
    confidence = _number(signal.get("confidence"))
    conflict_value = signal.get("conflict")
    conflict_valid = (
        isinstance(conflict_value, int | float)
        and not isinstance(conflict_value, bool)
        and isfinite(conflict_value)
        and 0 <= conflict_value <= 1
    )
    if not conflict_valid:
        blockers.append("新闻冲突度数据缺失或异常")
    elif _number(conflict_value) >= MAX_ACTION_CONFLICT:
        blockers.append("多空证据冲突，需先核实相互矛盾的事件")
    if not MIN_ACTION_CONFIDENCE <= confidence <= 1:
        blockers.append("新闻置信度未达到 50% 的行动门槛")
    if abs(score) < MIN_ACTION_SCORE or abs(score) > 100:
        blockers.append("决策分绝对值未达到 20 或数据异常")
    if direction not in {"bullish", "bearish"}:
        blockers.append("新闻尚未形成明确方向")
    elif (direction == "bullish" and score <= 0) or (direction == "bearish" and score >= 0):
        blockers.append("新闻方向与决策分不一致，需重新计算")
    return blockers


def _valuation_blockers(pe_summary: Mapping[str, Any], now: datetime) -> list[str]:
    blockers = []
    if pe_summary.get("status") == "not_applicable":
        blockers.append("PE 方法不适用，需补充其他估值依据")
    elif pe_summary.get("status") != "ready" or pe_summary.get("valuation_status") not in {
        "below_range", "within_range", "above_range"
    }:
        blockers.append("PE 盈利和估值假设不完整")
    if _number(pe_summary.get("current_price")) <= 0:
        blockers.append("缺少有效当前报价")
    freshness = _freshness_issue(
        pe_summary.get("price_as_of"), now, PRICE_MAX_AGE_WORKDAYS, "报价"
    )
    if freshness:
        blockers.append(freshness)
    if (
        pe_summary.get("price_provenance") != "manual"
        and pe_summary.get("source_status") == "error"
    ):
        blockers.append("自动数据刷新失败，需核对报价来源")
    year = pe_summary.get("valuation_year")
    if isinstance(year, int) and year < now.year:
        blockers.append("第一预测年度已过去，需更新盈利基准和估值假设")
    return blockers


def _pending_action_explanation(
    holding_status: str,
    signal: Mapping[str, Any] | None,
    pe_summary: Mapping[str, Any],
    signal_blockers: list[str],
    valuation_blockers: list[str],
    now: datetime,
) -> tuple[str, str]:
    """Explain the first resolvable gap while keeping every gate unchanged."""
    price_issues = []
    if _number(pe_summary.get("current_price")) <= 0:
        price_issues.append("缺少有效当前报价")
    price_freshness = _freshness_issue(
        pe_summary.get("price_as_of"), now, PRICE_MAX_AGE_WORKDAYS, "报价"
    )
    if price_freshness:
        price_issues.append(price_freshness)
    if (
        pe_summary.get("price_provenance") != "manual"
        and pe_summary.get("source_status") == "error"
    ):
        price_issues.append("自动报价来源最近刷新失败")
    if price_issues:
        return "先更新报价", (
            f"{'；'.join(price_issues)}。确认最新价格后再判断是否调整仓位；"
            "报价缺口本身不构成卖出理由。"
        )

    if signal is None:
        return "等待可验证催化", (
            "当前没有可用的个股新闻信号，需补充与公司收入、成本或风险直接相关的事件证据。"
        )
    signal_freshness = _freshness_issue(
        signal.get("as_of"), now, SIGNAL_MAX_AGE_WORKDAYS, "新闻信号"
    )
    if signal_freshness:
        return "补当前事件依据", (
            f"{signal_freshness}。重新采集并计算当前事件影响后，再判断原有方向是否仍成立。"
        )
    if not signal.get("evidence_event_ids"):
        return "等待可验证催化", (
            "最新信号缺少仍有效的事件依据，需确认具体催化及原始来源后再评估仓位变化。"
        )
    if MAX_ACTION_CONFLICT <= _number(signal.get("conflict")) <= 1:
        pause = "加仓" if holding_status == "long" else "建仓"
        return "先核对分歧", (
            f"当前有效事件的多空冲突度为 {_number(signal.get('conflict')):.0%}，"
            f"已达到 35% 的暂停门槛；先核对相互矛盾的证据，暂缓{pause}。"
        )
    if valuation_blockers:
        return "补合适估值依据", (
            f"{'；'.join(valuation_blockers)}。补齐可用于当前公司的盈利与估值依据，"
            "是否已经计价还需核对市场预期及事件前后行情。"
        )
    if pe_summary.get("valuation_status") == "above_range":
        return "核对估值是否透支", (
            "当前报价高于 PE 假设区间上沿，需验证盈利能否支撑现价；"
            "先核对估值假设及催化兑现情况。"
        )
    pause = "加仓" if holding_status == "long" else "建仓"
    return f"暂缓{pause}，等待明确催化", (
        f"{'；'.join(signal_blockers) or '当前行动条件尚未全部满足'}。"
        "等待可验证事件形成足够明确的方向，再评估仓位变化。"
    )


def _action_scenario(
    holding_status: str,
    signal: Mapping[str, Any] | None,
    pe_summary: Mapping[str, Any],
    signals_by_horizon: Mapping[Any, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    signal_blockers = _signal_blockers(signal, now)
    valuation_blockers = _valuation_blockers(pe_summary, now)
    valuation = pe_summary.get("valuation_status")
    direction = signal.get("direction") if signal else None
    blockers = signal_blockers + valuation_blockers
    next_steps: list[str] = []
    code = "review" if holding_status == "long" else "wait"
    label, reason = _pending_action_explanation(
        holding_status, signal, pe_summary, signal_blockers, valuation_blockers, now
    )

    if not signal_blockers and direction == "bearish":
        blockers = []
        if holding_status == "flat":
            code, label = "avoid", "暂不买入"
            reason = "有效新闻形成较强负面信号，当前不满足建仓条件。"
        elif not valuation_blockers and valuation == "above_range":
            code, label = "sell_candidate", "卖出候选"
            reason = "已持仓，较强负面新闻与偏高估值同时出现，优先核实退出条件。"
        else:
            code, label = "reduce", "减仓候选"
            reason = "已持仓且有效负面新闻达到风控门槛，优先复核并降低风险敞口。"
        next_steps.extend(valuation_blockers)
        next_steps.append("核对负面事件是否已兑现，以及原持仓逻辑是否失效")
    elif not blockers and direction == "bullish":
        if valuation == "above_range":
            blockers.append("当前报价高于 PE 假设区间，新闻利好缺少估值缓冲")
        elif holding_status == "long":
            code, label = "hold", "持有观察"
            reason = "已有持仓，新闻达到正向门槛且估值未超出假设区间，继续验证催化兑现。"
            next_steps.append("跟踪催化兑现和持仓逻辑的失效条件")
        else:
            opposing_horizons = [
                str(other.get("horizon") or horizon)
                for horizon, other in (signals_by_horizon or {}).items()
                if isinstance(other, Mapping)
                and other.get("direction") == "bearish"
                and not _signal_blockers(other, now)
            ]
            if opposing_horizons:
                label = "等待周期方向一致"
                blockers.append(
                    f"其他周期（{'、'.join(opposing_horizons)} 日）出现有效强负面信号，暂缓新增仓位"
                )
                reason = "当前周期利好与其他周期的有效负面信号冲突，等待方向一致。"
            else:
                code, label = "buy_candidate", "买入候选"
                reason = "空仓，新闻达到正向门槛且估值未超出假设区间，可进入建仓核验。"
                next_steps.append("核对一手证据、最新可成交价格和个人可承受仓位后再决定是否建仓")
    next_steps.extend(blockers)
    return {
        "code": code,
        "label": label,
        "reason": reason,
        "blockers": blockers,
        "next_steps": list(dict.fromkeys(next_steps)),
        "holding_status": holding_status,
    }


def _build_action(
    signal: Mapping[str, Any] | None,
    pe_summary: Mapping[str, Any],
    holding_status: str,
    signals_by_horizon: Mapping[Any, Any] | None,
    social_context: Mapping[str, Any] | None,
    now: datetime,
    event_checks: Sequence[Mapping[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    scenarios = {
        state: _action_scenario(state, signal, pe_summary, signals_by_horizon, now)
        for state in ("flat", "long")
    }
    action: dict[str, Any]
    if holding_status == "short":
        action = {
            "code": "review", "label": "复核空头持仓",
            "reason": "真实持仓为空头；当前行动规则仅适用于股票多头。",
            "blockers": ["空头持仓不适用当前多头行动规则"],
            "next_steps": ["核对券商空头持仓及保证金要求"], "holding_status": "short",
        }
    elif holding_status in scenarios:
        action = dict(scenarios[holding_status])
    else:
        holding_status = "unknown"
        action = {
            "code": "review",
            "label": "先填写持仓",
            "reason": "是否已有持仓会改变行动，先选择空仓或已持仓；下方保留两种情景供核对。",
            "blockers": ["持仓状态未填写"],
            "next_steps": ["在自选股中填写空仓或已持仓"],
            "holding_status": holding_status,
        }
    if social_context and social_context.get("post_count"):
        social_step = "核对相关 X 线索原文与一手证据；社交观点不直接增加行动分数"
        if social_context.get("conflicts"):
            social_step += "，观点分歧需与一手证据核对"
        action["next_steps"] = [*action["next_steps"], social_step]
        action["next_steps"].extend(
            str(item) for item in social_context.get("verification_needs") or []
        )
    return {
        **action,
        "flat_action": scenarios["flat"],
        "long_action": scenarios["long"],
        "entry_conditions": [
            "持仓状态为明确空仓，当前有效新闻达到正向行动门槛",
            "报价和估值假设可用，报价不高于 PE 假设区间上沿",
            "其他周期没有仍有效的强负面信号，并核实具体催化和一手证据",
        ],
        "exit_conditions": [
            "原持仓逻辑被一手证据证伪，或关键催化未能按预期兑现时重新评估退出",
            "有效强负面新闻优先触发减仓复核；同时估值偏高时进入卖出候选",
            "PE 年度情景区间不作为所选新闻周期的止损价，具体风险预算需单独确认",
        ],
        "policy_version": ACTION_POLICY_VERSION,
        "policy_note": ACTION_POLICY_NOTE,
        "as_of": now,
        "plan": build_action_plan(
            action, signal, event_checks, social_context, horizon=horizon, now=now
        ),
    }


def build_watchlist_judgment(
    signal: Mapping[str, Any] | None,
    pe_summary: Mapping[str, Any],
    event_titles: Mapping[int, str] | None = None,
    macro_context: Mapping[str, Any] | None = None,
    *,
    holding_status: str = "unknown",
    signals_by_horizon: Mapping[Any, Any] | None = None,
    social_context: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    event_checks: Sequence[Mapping[str, Any]] = (),
    horizon: int | None = None,
) -> dict[str, Any]:
    """Return a concise synthesis without adding an LLM call to page rendering."""
    news_state = _news_state(signal)
    valuation_status = str(pe_summary.get("valuation_status") or "unavailable")
    valuation_ready = (
        pe_summary.get("status") == "ready"
        and valuation_status in {"below_range", "within_range", "above_range"}
    )

    if news_state == "mixed":
        status, label, stance, conclusion = (
            "caution",
            "新闻证据冲突",
            "等待验证",
            "多空新闻相互抵消，PE 结论不足以消除方向不确定性",
        )
    elif valuation_ready and news_state != "unavailable":
        status, label, stance, conclusion = COMBINED_MATRIX[(news_state, valuation_status)]
    elif valuation_ready:
        status, label, stance, conclusion = COMBINED_MATRIX[("neutral", valuation_status)]
        conclusion = f"{conclusion}；目前缺少可用新闻事件，不能确认估值差是否存在催化"
    else:
        status = "incomplete"
        stance = "补齐数据"
        label = {
            "bullish": "新闻偏多，PE 估值待补",
            "bearish": "新闻承压，PE 估值待补",
            "neutral": "方向不明，PE 估值待补",
            "unavailable": "新闻与估值数据不足",
        }.get(news_state, "证据冲突，PE 估值待补")
        conclusion = (
            "年度估值依据待补；是否已经计价还需市场预期和事件前后行情，当前尚未验证"
        )

    news_detail = _news_detail(signal, news_state)
    valuation_detail = _valuation_detail(pe_summary)
    return {
        "status": status,
        "label": label,
        "stance": stance,
        "summary": f"{news_detail}；{valuation_detail}。{conclusion}。",
        "news": {"state": news_state, "label": NEWS_LABELS[news_state]},
        "valuation": {
            "status": valuation_status,
            "label": pe_summary.get("valuation_label") or "PE 估值待补",
        },
        "key_events": _key_events(signal, event_titles or {}),
        "macro": dict(macro_context) if macro_context else None,
        "social": dict(social_context) if social_context else None,
        "action": _build_action(
            signal,
            pe_summary,
            holding_status,
            signals_by_horizon,
            social_context,
            _timestamp(now) or datetime.now(UTC),
            event_checks,
            horizon or int((signal or {}).get("horizon") or 5),
        ),
    }
