"""Combine event-driven signals and PE valuation into an auditable watchlist view."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _news_state(signal: Mapping[str, Any] | None) -> str:
    if signal is None:
        return "unavailable"
    direction = str(signal.get("direction") or "neutral")
    conflict = _number(signal.get("conflict"))
    if direction in {"bullish", "bearish"} and conflict >= 0.5:
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
            and (not allowed or int(item["event_id"]) in allowed)
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


def build_watchlist_judgment(
    signal: Mapping[str, Any] | None,
    pe_summary: Mapping[str, Any],
    event_titles: Mapping[int, str] | None = None,
    macro_context: Mapping[str, Any] | None = None,
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
        conclusion = "先补齐盈利预测和 PE 区间，再判断新闻催化是否已反映在价格中"

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
    }
