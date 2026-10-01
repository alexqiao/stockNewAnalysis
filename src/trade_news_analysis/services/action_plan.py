"""Turn existing research and data gaps into conditional, source-linked tasks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from .scoring import SIGNAL_TTL_MULTIPLIER, trading_sessions_since


def _strings(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if (
        isinstance(value, list)
    ) else []


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_action_plan(
    action: Mapping[str, Any],
    signal: Mapping[str, Any] | None,
    event_checks: Sequence[Mapping[str, Any]],
    social: Mapping[str, Any] | None,
    *,
    horizon: int,
    now: datetime,
) -> dict[str, Any]:
    """Research statements remain unverified conditions, never observed triggers."""
    if horizon not in {1, 5, 20}:
        raise ValueError("行动评估周期只能为 1、5 或 20 个交易日")
    tasks: list[dict[str, Any]] = []
    blockers = _strings(action.get("blockers")) + _strings(action.get("next_steps"))
    risk_action = action.get("code") in {"reduce", "sell_candidate", "avoid"}

    def add(
        kind: str, title: str, detail: str, when: str, on_pass: str, on_fail: str,
        *, priority: int, source_kind: str = "", source_id: int | None = None,
        source_label: str = "", reference: str = "",
    ) -> None:
        tasks.append({
            "kind": kind, "title": title, "detail": detail, "when": when,
            "on_pass": on_pass, "on_fail": on_fail, "priority": priority,
            "source_kind": source_kind, "source_id": source_id,
            "source_label": source_label, "reference": reference,
            "status": "pending", "status_label": "待核实",
        })

    if action.get("holding_status") == "unknown":
        add(
            "holding", "填写是否持仓", "在自选股中选择未持仓或已持仓，以切换建仓与退出情景。",
            "本次判断前", "按实际持仓显示对应条件。", "继续同时展示两种情景，不推定已有仓位。",
            priority=0, source_kind="watchlist", source_label="填写持仓状态",
        )
    price_gaps = list(dict.fromkeys(
        text for text in blockers if any(word in text for word in ("报价", "刷新失败"))
    ))
    if price_gaps:
        add(
            "quote", "更新报价，确认价格时间和币种", "；".join(price_gaps),
            "下一次交易决策前", "使用新报价重算估值与行动条件。",
            "价格仍不可用时，不据旧报价确定买卖价位；负面事件仍需核验。",
            priority=10, source_kind="valuation", source_label="刷新基础数据或确认人工价格",
        )
    if any("新闻信号" in text and any(word in text for word in ("超过", "时间"))
           for text in blockers):
        add(
            "signal", "更新已过期的事件判断", "新闻判断的时间已不满足当前行动条件。",
            "下一次交易决策前", "使用更新后的证据、方向和分歧重新判断。",
            "保留为历史研究，不继续沿用旧行动结论。",
            priority=15, source_kind="status", source_label="查看采集与分析状态",
        )
    if any("冲突" in text or "矛盾" in text for text in blockers):
        add(
            "conflict", "核对多空分歧的具体来源",
            "区分同一事实的不同解释、不同期限的影响和重复转载。",
            "新增仓位前", "证据更新后重算各周期；仍需满足价格与风险条件。",
            "分歧未解决则暂缓新增仓位，不把多空相抵解释为风险消失。",
            priority=20,
        )

    components = (signal.get("components") or {}).get("events", []) if signal else []
    contributions = {
        item.get("event_id"): item.get("contribution", 0)
        for item in components if isinstance(item, Mapping) and not item.get("expired")
    } if isinstance(components, list) else {}
    eligible = []
    for event in event_checks:
        occurred_at = _timestamp(event.get("occurred_at"))
        event_id = event.get("event_id")
        if (
            not isinstance(event_id, int) or isinstance(event_id, bool) or event_id <= 0
            or event.get("status") not in {"complete", "partial"} or occurred_at is None
            or occurred_at > now + timedelta(minutes=5)
            or trading_sessions_since(occurred_at, now) > horizon * SIGNAL_TTL_MULTIPLIER
        ):
            continue
        contribution = contributions.get(event_id, 0)
        weight = abs(float(contribution)) if isinstance(contribution, int | float) else 0.0
        eligible.append((event, occurred_at, weight))
    eligible.sort(key=lambda row: (row[2], row[1], row[0]["event_id"]), reverse=True)
    seen_events: set[int] = set()
    for event, _date, _weight in eligible:
        event_id = int(event["event_id"])
        if event_id in seen_events:
            continue
        source: dict[str, Any] = {
            "source_kind": "event", "source_id": event_id,
            "source_label": "查看事件与原文", "reference": str(event.get("title") or "待核实事件"),
        }
        proof = _strings(event.get("missing_proof"))
        catalysts = _strings(event.get("catalysts"))
        falsifiers = _strings(event.get("falsifiers"))
        if not (proof or catalysts or falsifiers):
            continue
        seen_events.add(event_id)
        direction = (event.get("directions") or {}).get(str(horizon), "neutral")
        if proof:
            add(
                "evidence", "核对这条事件缺少的一手依据", "；".join(proof[:2]),
                "下一次交易决策前，先核对原始披露", "更新该事件的证据，再重新分析证券影响。",
                "未核实的主张继续列为假设，不用其支持新增仓位。",
                priority=5 if risk_action and direction == "bearish" else 25, **source,
            )
        if catalysts:
            add(
                "catalyst", "核对预计事件是否发生及结果", "；".join(catalysts[:2]),
                f"相关披露发布后；本次 {horizon} 个交易日评估窗口结束时复查",
                "记录实际发生时间、结果和原文；与原先假设比较后重新判断。事件发生不等于股价会涨。",
                "未发生、延期或结果不支持原假设时，重新评估依赖该事件的行动；日期未知时保留待核实。",
                priority=30, **source,
            )
        if falsifiers:
            consequence = {
                "bullish": "若证伪条件得到确认，该利好假设失效；已持仓时重新评估减仓或退出。",
                "bearish": "若证伪条件得到确认，该利空假设失效；重新评估是否仍需降低风险。",
            }.get(direction, "若证伪条件得到确认，撤回该研究假设并重新分析，不直接推导买卖。")
            add(
                "invalidation", "检查什么情况会推翻判断", "；".join(falsifiers[:2]),
                "一手披露出现时立即复查，不等到窗口结束", consequence,
                "暂未证伪不代表已经证实，继续跟踪原始证据。",
                priority=8 if risk_action and direction == "bearish" else 35, **source,
            )
        if len(seen_events) == 2:
            break
    if not seen_events:
        add(
            "catalyst_gap", f"确认未来 {horizon} 个交易日内的可验证催化",
            "当前没有日期有效且可关联的事件核验项。先查公司公告或 IR 日历；未披露日期须保持未知。",
            "下一次交易决策前", "记录披露日期、观察指标和原始链接，再重做事件判断。",
            "没有具体催化时保留观察，不用旧新闻或博主情绪补出买入理由。",
            priority=25,
        )
    valuation_gaps = list(dict.fromkeys(
        text for text in blockers
        if any(word in text for word in ("PE", "盈利", "估值", "预测年度"))
    ))
    if valuation_gaps:
        add(
            "valuation", "核对估值依据与适用方法", "；".join(valuation_gaps),
            "需要使用估值决定价格是否合适时", "保留可追溯的盈利假设与估值区间，重新评估。",
            "估值保持未验证；亏损企业或基金不硬套 PE，年度区间不作短期止损价。",
            priority=40, source_kind="valuation", source_label="核对估值输入",
        )
    for post in (social or {}).get("posts") or []:
        if not isinstance(post, Mapping):
            continue
        needs = _strings(post.get("verification_needs"))
        post_id = post.get("id")
        if not needs or not isinstance(post_id, int) or isinstance(post_id, bool) or post_id <= 0:
            continue
        add(
            "social", "把博主主张与原始披露对照", "；".join(needs[:2]),
            "把该线索作为交易依据前", "有独立证据后更新事件分析，观点数量不代替事实。",
            "保留为未验证线索，不作为买卖触发。",
            priority=50, source_kind="post", source_id=post_id,
            source_label="查看博主原文与核验事项", reference=f"@{post.get('author', '')}",
        )
        break
    tasks.sort(key=lambda task: task["priority"])
    return {
        "horizon": horizon, "tasks": tasks,
        "review_when": f"下一次交易决策前复查；出现新公告时立即重评；第 {horizon} 个交易日复盘。",
        "calendar_note": "具体催化日期尚未核实，请以公司已确认的披露日程为准。",
        "quantity_note": "缺少仓位比例、组合敞口和单笔风险预算，暂不能计算买卖数量。",
    }
