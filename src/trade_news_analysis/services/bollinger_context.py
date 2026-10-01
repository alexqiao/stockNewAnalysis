"""Attach local holding facts and freshness limits to technical references."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..daily_bar_models import DailyBar
from ..models import Security, Watchlist
from ..risk_models import SecurityRiskProfile
from .bollinger_reference import BollingerParameters, analyze_bollinger
from .daily_bars import get_daily_bars
from .holdings import read_holdings, utc
from .market_research import number
from .risk import get_risk_inputs


def get_bollinger_reference(
    session: Session, security: Security, parameters: BollingerParameters,
    adjustment: str = "total_return_adjusted", *, now: datetime | None = None,
) -> dict[str, Any]:
    current = utc(now or datetime.now(UTC))
    # The chart viewport must never change squeeze/first-pullback history.
    daily = get_daily_bars(session, security, now=current)
    bars = daily["bars"]
    adjusted = bool(bars) and all(
        (value := number(bar.get(f"adj_{field}"))) is not None and value > 0
        for bar in bars for field in ("open", "high", "low", "close")
    )
    basis = adjustment if adjustment == "split_adjusted" or adjusted else "split_adjusted"
    factor = bars[-1]["adj_close"] / bars[-1]["close"] if (
        bars and basis == "total_return_adjusted"
    ) else 1.0
    holdings = read_holdings(session, current)
    inputs = get_risk_inputs(session, security.id, now=current)
    profile = session.scalar(select(SecurityRiskProfile).where(
        SecurityRiskProfile.security_id == security.id,
    ))
    watch = session.scalar(select(Watchlist).where(Watchlist.security_id == security.id))
    risk = inputs["security"]
    holding_gaps = list(holdings["blockers"])
    status = inputs["holdings_source"].get("holding_status") if holdings["active"] else (
        watch.holding_status if watch else "unknown"
    )
    quantity, weight = number(risk.get("current_quantity")), number(risk.get("current_weight"))
    cost = number(risk.get("average_cost"))
    if status == "unknown":
        holding_gaps.append("尚未确认持仓状态；先在自选或持仓页确认")
    if status == "short":
        holding_gaps.append("当前为空头持仓，本参考只覆盖多头体系")
    if status == "long" and (quantity is None or quantity <= 0 or weight == 0):
        holding_gaps.append("多头状态与实际股数或持仓比例不一致，请先核对")
    if status == "flat" and ((quantity or 0) != 0 or (weight or 0) != 0):
        holding_gaps.append("空仓状态与实际股数或持仓比例不一致，请先核对")
    broker_position = next((p for p in holdings["positions"]
                            if p["security_id"] == security.id), None)
    if broker_position and broker_position["currency"] != security.currency:
        holding_gaps.append("持仓与证券币种不一致，无法比较成本与止损")
    data_gaps = []
    if daily["stale"]:
        data_gaps.append("行情尚未覆盖最近应收盘交易日，或最近采集失败；先刷新行情")
    if daily["currency"] != security.currency:
        data_gaps.append("行情与证券币种不一致，暂停操作参考")
    if not security.active:
        data_gaps.append("证券已停用，仅供查看历史形态")
    explicit_stop = number(profile.stop_price) if profile else None
    stop_basis_unverified = bool(explicit_stop and session.scalar(select(DailyBar.id).where(
        DailyBar.security_id == security.id, DailyBar.source == "yfinance",
        DailyBar.stock_splits > 0,
    ).limit(1)))
    if stop_basis_unverified:
        holding_gaps.append(
            "历史存在拆股，已保存止损缺少独立的口径确认记录；"
            "本面板仅保留技术形态，不自动判断该止损是否触发或应上移"
        )
        explicit_stop = None
    position = {
        "status": status or "unknown", "reliable": not holding_gaps,
        "average_cost": cost * factor if cost and cost > 0 else None,
        # Risk defaults contain a moving price * 0.9 estimate, not a recorded stop.
        "current_stop": explicit_stop * factor if explicit_stop and explicit_stop > 0 else None,
    }
    result = analyze_bollinger(bars, parameters, adjustment, position)
    result["technical_action"] = dict(result["action"])
    result["holding_source"] = "券商同步" if holdings["active"] else "手动确认"
    result["holdings_source"] = inputs["holdings_source"]
    result["recorded_stop"] = {
        "price": number(profile.stop_price) if profile else None,
        "currency": security.currency, "basis_verified": not stop_basis_unverified,
    }
    result["data"] = {key: daily[key] for key in (
        "currency", "source", "latest_trade_date", "stale", "sync_status", "last_success_at",
    )}
    risk_gaps = _risk_limits(
        inputs, bars[-1]["close"] if bars else None, explicit_stop, security.currency,
    )
    gaps = list(dict.fromkeys([*data_gaps, *holding_gaps, *risk_gaps]))
    result["guardrails"] = {"blocked": bool(gaps), "reasons": gaps}
    result["execution_ready"] = False
    result["warnings"].extend(gaps)
    result["warnings"].append(
        "技术候选不包含可买股数；实际操作前仍需核对账户资金、仓位上限、风险预算及实时价格。"
    )
    result["warnings"].append(
        "止损参考不会自动保存；下一次建议以明确保存的止损为下限，未采纳的参考值不构成持久追踪记录。"
    )
    if not holdings["active"]:
        result["warnings"].append("手动持仓按已保存输入计算，系统无法独立核实其时效，请先核对。")
    if result["price_basis"] == "total_return_adjusted":
        result["warnings"].append(
            "参考价为含分红调整价格；成本和已记录止损按最新复权因子换算后比较，"
            "不作为交易委托价。显示的浮盈仅为价格相对成本变化，不含分红、费用或汇兑。"
        )
    if data_gaps or holding_gaps:
        result["action"] = {
            "code": "blocked", "label": "先更新或核对资料",
            "reason": "；".join([*data_gaps, *holding_gaps]),
        }
    elif risk_gaps and result["action"]["code"] != "stop_triggered":
        result["action"] = {
            "code": "blocked", "label": "先复核持仓风险", "reason": "；".join(risk_gaps),
        }
    elif status == "long" and result["action"]["code"] == "entry_reference":
        result["action"] = {
            "code": "wait", "label": "持仓观察，暂不新增",
            "reason": "已持有多头；中轨形态不代表已获准加仓，首版仅展示加仓待满足条件。",
        }
    return result


def _risk_limits(
    inputs: dict[str, Any], price: float | None, stop: float | None, currency: str,
) -> list[str]:
    """Respect known limits without inventing order quantities or currency conversion."""
    values, portfolio = inputs["security"], inputs["portfolio"]
    reasons = []
    for current_key, limit_key, label in (
        ("current_weight", "max_weight", "单股"),
        ("sector_current_weight", "sector_limit_pct", "行业"),
    ):
        current, limit = number(values.get(current_key)), number(values.get(limit_key))
        if current is not None and limit is not None and current >= limit > 0:
            source = inputs.get("field_sources", {}).get(limit_key, {}).get("kind")
            basis = "默认" if source == "default" else "已设置的"
            reasons.append(f"当前{label}仓位已达到{basis}风险上限，先复核敞口")
    total, quantity = number(portfolio.get("total_value")), number(values.get("current_quantity"))
    weight, budget = number(values.get("current_weight")), number(values.get("risk_budget_pct"))
    fee, slippage = number(values.get("fee_bps")), number(values.get("slippage_bps"))
    if (price and price > 0 and stop and 0 < stop < price and weight is not None
            and weight > 0 and budget is not None and budget > 0
            and total is not None and total > 0 and quantity is not None and quantity > 0
            and portfolio.get("currency") == currency
            and abs(quantity * price / total - weight) <= max(0.005, abs(weight) * 0.1)
            and fee is not None and slippage is not None):
        loss = quantity * (price - stop + 2 * price * (fee + slippage) / 10000) / total
        if loss > budget:
            reasons.append("按已记录止损及费用估算，持仓风险超过账户风险预算，先评估降低风险")
    if (total is not None and total <= 0) or (quantity is not None and quantity < 0):
        reasons.append("当前账户净值或空头持仓不适用多头风险参考")
    return reasons
