"""Observed price response around verified disclosure time, never a pricing verdict."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .market_research import number, parse_time


def event_price_reaction(
    first_disclosed_at: datetime | None,
    verified: bool,
    market: Mapping[str, Any],
    horizon: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "unknown",
        "return_pct": None,
        "industry_excess_return_pct": None,
        "sessions_observed": 0,
        "baseline_date": None,
        "last_date": None,
        "note": "披露前最近收盘至披露后的已完成收盘；不代表可成交收益或消息是否充分定价",
    }
    disclosed = parse_time(first_disclosed_at)
    if not verified or disclosed is None:
        result["reason"] = "首次披露时间尚未核实"
        return result
    if (market.get("metadata") or {}).get("adjustment_status") != "verified":
        result["reason"] = "复权口径未核实"
        return result
    bars = [bar for bar in market.get("bars") or [] if bar.get("valid")]
    before = [
        bar for bar in bars if (stamp := parse_time(bar.get("observed_at"))) and stamp < disclosed
    ]
    after = [
        bar for bar in bars if (stamp := parse_time(bar.get("observed_at"))) and stamp >= disclosed
    ]
    if not before or not after:
        result["reason"] = "缺少披露前报价或披露后完整交易日"
        return result
    baseline, last = before[-1], after[:horizon][-1]
    start, end = number(baseline.get("adj_close")), number(last.get("adj_close"))
    if start is None or end is None or start <= 0 or end <= 0:
        result["reason"] = "可比价格缺失"
        return result
    change = (end / start - 1) * 100
    result.update(
        status="observed",
        return_pct=change,
        baseline_date=baseline["date"],
        last_date=last["date"],
        sessions_observed=min(len(after), horizon),
    )
    bm_meta = market.get("benchmark_metadata") or {}
    meta = market.get("metadata") or {}
    if (
        bm_meta.get("adjustment_status") == "verified"
        and bm_meta.get("currency") == meta.get("currency")
        and bm_meta.get("analysis_price_basis") == meta.get("analysis_price_basis")
    ):
        indexed = {bar["date"]: bar for bar in market.get("benchmark_bars") or []}
        b_start = number(indexed.get(baseline["date"], {}).get("adj_close"))
        b_end = number(indexed.get(last["date"], {}).get("adj_close"))
        if b_start and b_end and b_start > 0 and b_end > 0:
            result["industry_excess_return_pct"] = change - (b_end / b_start - 1) * 100
    return result
