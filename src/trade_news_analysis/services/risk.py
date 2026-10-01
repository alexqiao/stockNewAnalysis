"""Risk capacity from account facts and editable assumptions, never model scores."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Security
from ..risk_models import PortfolioRiskProfile, SecurityRiskProfile
from .holdings import HOLDING_FACT_FIELDS, effective_facts, latest_snapshot, read_holdings
from .market_research import get_market_research, number, parse_time, quote_session_age, utc
from .risk_defaults import RISK_DEFAULTS, benchmark_inputs, sector_key

PORTFOLIO_FIELDS = ("total_value", "available_cash", "currency")
RISK_FIELDS = (
    "current_weight",
    "current_quantity",
    "average_cost",
    "max_weight",
    "risk_budget_pct",
    "stop_price",
    "sector_limit_pct",
    "sector_current_weight",
    "lot_size",
    "max_participation_pct",
    "fee_bps",
    "slippage_bps",
    "benchmark_market",
    "benchmark_symbol",
    "benchmark_currency",
    "benchmark_label",
)
FRACTION_FIELDS = {
    "current_weight",
    "max_weight",
    "risk_budget_pct",
    "sector_limit_pct",
    "sector_current_weight",
    "max_participation_pct",
}
FIELD_LABELS = {
    "total_value": "组合总额",
    "available_cash": "可用现金",
    "currency": "组合币种",
    "current_weight": "当前持仓比例",
    "current_quantity": "当前实际股数",
    "average_cost": "持仓成本",
    "max_weight": "单股最大仓位",
    "risk_budget_pct": "每笔风险预算",
    "stop_price": "失效价格",
    "sector_limit_pct": "行业仓位上限",
    "sector_current_weight": "完整组合的当前行业仓位",
    "lot_size": "每手股数",
    "max_participation_pct": "单日成交量参与上限",
    "fee_bps": "单边交易费率（基点）",
    "slippage_bps": "单边滑点（基点）",
}


@dataclass
class RiskInputsData:
    profiles: dict[int, SecurityRiskProfile]
    portfolio: PortfolioRiskProfile | None
    holdings: dict[str, Any]
    market: dict[int, dict[str, Any]]
    securities: dict[int, Security]


def load_risk_inputs(
    session: Session, securities: Sequence[Security], holdings: dict[str, Any],
    market: dict[int, dict[str, Any]],
) -> RiskInputsData:
    ids = {security.id for security in securities}
    profiles = {row.security_id: row for row in session.scalars(
        select(SecurityRiskProfile).where(SecurityRiskProfile.security_id.in_(ids))
    )}
    members = {security.id: security for security in securities}
    missing = {row["security_id"] for row in holdings["positions"]
               if row["security_id"] is not None} - members.keys()
    if missing:
        members.update((row.id, row) for row in session.scalars(
            select(Security).where(Security.id.in_(missing))
        ))
    return RiskInputsData(profiles, session.get(PortfolioRiskProfile, 1), holdings, market, members)


def _profile_dict(profile: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(profile, field) if profile is not None else None for field in fields}


def save_portfolio_risk(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    if latest_snapshot(session) is not None:
        raise ValueError("组合资产由 IBKR 同步，请在自选页面刷新真实持仓")
    if set(payload) - set(PORTFOLIO_FIELDS):
        raise ValueError("未知组合风险字段")
    profile = session.get(PortfolioRiskProfile, 1)
    if profile is None:
        profile = PortfolioRiskProfile(id=1)
        session.add(profile)
    values = {**_profile_dict(profile, PORTFOLIO_FIELDS), **payload}
    for field in ("total_value", "available_cash"):
        if values[field] is not None:
            parsed = number(values[field])
            if parsed is None or parsed < 0 or (field == "total_value" and parsed == 0):
                raise ValueError(f"{FIELD_LABELS[field]}必须是有效非负数，组合总额需大于零")
            values[field] = parsed
    if values["currency"] is not None:
        currency = str(values["currency"]).strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("组合币种需使用三位货币代码")
        values["currency"] = currency
    if values["total_value"] is not None and values["available_cash"] is not None:
        if values["available_cash"] > values["total_value"]:
            raise ValueError("可用现金不能超过组合总额")
    for field, value in values.items():
        setattr(profile, field, value)
    profile.updated_at = datetime.now(UTC)
    session.flush()
    return _profile_dict(profile, PORTFOLIO_FIELDS)


def save_security_risk(
    session: Session, security_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券不存在")
    if set(payload) - set(RISK_FIELDS):
        raise ValueError("未知证券风险字段")
    broker_active = latest_snapshot(session) is not None
    if broker_active and set(payload) & HOLDING_FACT_FIELDS:
        raise ValueError("实际持仓由 IBKR 同步，数量、成本和比例不能手动覆盖")
    profile = session.scalar(
        select(SecurityRiskProfile).where(SecurityRiskProfile.security_id == security_id)
    )
    if profile is None:
        profile = SecurityRiskProfile(security_id=security_id)
        session.add(profile)
    values = {**_profile_dict(profile, RISK_FIELDS), **payload}
    for field, value in values.items():
        if (
            value is None or field.startswith("benchmark_")
            or broker_active and field in HOLDING_FACT_FIELDS
        ):
            continue
        parsed = number(value)
        if parsed is None or parsed < 0:
            raise ValueError(f"{FIELD_LABELS[field]}必须是有效非负数")
        if field in FRACTION_FIELDS and parsed > 1:
            raise ValueError(f"{FIELD_LABELS[field]}使用 0–1 比例")
        if field in {"average_cost", "stop_price", "lot_size"} and parsed <= 0:
            raise ValueError(f"{FIELD_LABELS[field]}必须大于零")
        if field == "lot_size" and not parsed.is_integer():
            raise ValueError("每手股数必须为正整数")
        if field in {"fee_bps", "slippage_bps"} and parsed >= 10000:
            raise ValueError("成本与滑点需小于 10000 基点")
        values[field] = int(parsed) if field == "lot_size" else parsed
    for field in ("benchmark_symbol", "benchmark_currency", "benchmark_market", "benchmark_label"):
        if values[field] is not None:
            values[field] = str(values[field]).strip() or None
    if values["benchmark_market"] not in {None, "A", "HK", "US"}:
        raise ValueError("行业基准市场仅支持 A、HK、US")
    if values["benchmark_symbol"] and not values["benchmark_market"]:
        values["benchmark_market"] = security.market
    if values["benchmark_currency"]:
        values["benchmark_currency"] = values["benchmark_currency"].upper()
    if (
        not broker_active and values["current_weight"] is not None
        and values["sector_current_weight"] is not None
    ):
        if values["current_weight"] > values["sector_current_weight"]:
            raise ValueError("单股仓位不能超过所属行业的完整仓位")
    if (
        not broker_active and values["current_weight"] == 0
        and (values["current_quantity"] or 0) != 0
    ):
        raise ValueError("空仓比例与实际股数不一致")
    for field, value in values.items():
        setattr(profile, field, value)
    profile.updated_at = datetime.now(UTC)
    session.flush()
    return get_risk_inputs(session, security_id)


def get_risk_inputs(
    session: Session, security_id: int, now: datetime | None = None,
    *, preloaded: RiskInputsData | None = None,
) -> dict[str, Any]:
    profile = preloaded.profiles.get(security_id) if preloaded is not None else session.scalar(
        select(SecurityRiskProfile).where(SecurityRiskProfile.security_id == security_id)
    )
    result: dict[str, Any] = {
        "security_id": security_id,
        "portfolio": _profile_dict(
            preloaded.portfolio if preloaded is not None else session.get(PortfolioRiskProfile, 1),
            PORTFOLIO_FIELDS,
        ),
        "security": _profile_dict(profile, RISK_FIELDS),
        "units": {"weights": "fraction_0_to_1", "costs": "basis_points_per_side"},
    }
    holdings = preloaded.holdings if preloaded is not None else read_holdings(session, now)
    result["holdings_source"] = {
        key: holdings[key] for key in (
            "source", "active", "snapshot_id", "account_label", "captured_at", "stale",
            "blockers", "using_cached", "warnings", "note", "last_sync",
        )
    }
    if holdings["active"]:
        result["portfolio"] = {key: holdings["portfolio"][key] for key in PORTFOLIO_FIELDS}
        facts = effective_facts(holdings, security_id)
        result["security"].update({key: facts[key] for key in HOLDING_FACT_FIELDS})
        result["holdings_source"]["holding_status"] = facts["holding_status"]
    security = (preloaded.securities.get(security_id) if preloaded is not None
                else session.get(Security, security_id))
    if security is not None:
        _apply_defaults(session, security, result, holdings, now, preloaded=preloaded)
    return result


def _apply_defaults(
    session: Session, security: Security, result: dict[str, Any],
    holdings: dict[str, Any], now: datetime | None,
    *, preloaded: RiskInputsData | None = None,
) -> None:
    values = result["security"]
    sources = {
        key: {"kind": "manual" if value is not None else "missing",
              "note": "已保存，可修改" if value is not None else "资料不足，暂未自动填入"}
        for key, value in values.items()
    }
    result["field_sources"] = sources
    result["defaults_blockers"] = []

    def fill(key: str, value: Any, kind: str, note: str) -> None:
        if values[key] is None:
            values[key] = value
            sources[key] = {"kind": kind, "note": note}

    for key, (value, note) in RISK_DEFAULTS.items():
        fill(key, value, "default", note)
    for key, value in benchmark_inputs(security, values).items():
        fill(key, value, "default" if value is not None else "missing",
             "默认行业近似对照，可修改" if value is not None else "未识别行业基准，可选填")
    if values["lot_size"] is None:
        metadata = security.provider_data or {}
        lot = number(metadata.get("board_lot") or metadata.get("lot_size"))
        if lot is not None and lot > 0 and lot.is_integer():
            fill("lot_size", int(lot), "automatic", "来自已有证券资料，可修改")
        elif security.market == "US":
            fill("lot_size", 1, "default", "默认按 1 股测算，不使用碎股")
        elif security.market == "A" and security.symbol.startswith(("0", "3", "6")) \
                and not security.symbol.startswith(("688", "689")):
            fill("lot_size", 100, "default", "默认按普通 A 股 100 股一手测算，可修改")
        else:
            sources["lot_size"]["note"] = "该市场交易单位因证券而异，需补充证券资料或手动填写"
    if values["stop_price"] is None:
        market = (preloaded.market[security.id] if preloaded is not None
                  else get_market_research(session, security.id, now=now))
        quote = market.get("quote") or {}
        current = utc(now or datetime.now(UTC))
        candidates = []
        price, timestamp = number(quote.get("price")), parse_time(quote.get("as_of"))
        if (price is not None and price > 0 and timestamp is not None and timestamp <= current
                and quote.get("valid") and quote.get("price_basis") == "raw"
                and quote.get("currency") == security.currency):
            candidates.append((timestamp, price, "最近完整日线"))
        for position in holdings["positions"]:
            price = number(position["market_price"])
            timestamp = parse_time(holdings["captured_at"])
            if (position["security_id"] == security.id and price is not None and price > 0
                    and timestamp is not None and timestamp <= current
                    and position["currency"] == security.currency):
                candidates.append((timestamp, price, "IBKR 持仓报价"))
        if candidates:
            timestamp, price, label = max(candidates, key=lambda item: item[0])
            fill("stop_price", float(f"{price * 0.9:.6g}"), "default",
                 f"默认取{label} {price:g} 的 90%；随报价更新，手动修改后固定")
    if holdings["active"]:
        for key in HOLDING_FACT_FIELDS:
            sources[key] = {"kind": "broker", "note": "IBKR 同步，只读"}
        if values["sector_current_weight"] is None:
            weight, complete = _sector_weight(
                session, security, holdings["positions"],
                securities=preloaded.securities if preloaded is not None else None,
            )
            fill("sector_current_weight", weight, "automatic" if complete else "estimate",
                 "按同次持仓快照及已有行业分类汇总，可修改" if complete else
                 "已填已识别同业仓位；部分持仓分类或估值缺失，请核对完整行业仓位后修改")
            if not complete:
                result["defaults_blockers"].append("自动行业仓位覆盖不完整，请核对并保存完整行业仓位")


def _sector_weight(
    session: Session, security: Security, positions: list[dict[str, Any]],
    *, securities: dict[int, Security] | None = None,
) -> tuple[float | None, bool]:
    if securities is None:
        securities = {row.id: row for row in session.scalars(
            select(Security).where(Security.id.in_([p["security_id"] for p in positions
                                                   if p["security_id"] is not None]))
        )}
    sector = sector_key(security)
    weight = 0.0
    complete = True
    for position in positions:
        if position["quantity"] == 0:
            continue
        member = securities.get(position["security_id"])
        group = sector_key(member) if member else None
        if not group or not sector or position["quantity"] < 0:
            complete = False
        if position["security_id"] == security.id or (sector is not None and group == sector):
            part = number(position["weight"])
            if part is None:
                return None, False
            weight += max(0, part)
    # The form and the long-only model cannot represent leveraged sector exposures.
    if weight > 1:
        return None, False
    return weight, complete


def build_risk_plan(
    session: Session,
    security_id: int,
    quote: dict[str, Any] | None = None,
    horizon: int = 5,
    now: datetime | None = None,
    *, market_data: dict[str, Any] | None = None,
    risk_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if horizon not in {1, 5, 20}:
        raise ValueError("horizon 必须是 1、5 或 20")
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券不存在")
    current = utc(now or datetime.now(UTC))
    market = market_data if market_data is not None else get_market_research(
        session, security_id, now=current
    )
    observation = quote or market.get("quote") or {}
    inputs = risk_inputs if risk_inputs is not None else get_risk_inputs(
        session, security_id, now=current
    )
    portfolio, risk = inputs["portfolio"], inputs["security"]
    blockers = [*inputs["holdings_source"]["blockers"], *inputs.get("defaults_blockers", [])]
    if (risk.get("current_quantity") or 0) < 0:
        blockers.append("真实持仓为空头，当前多头数量模型不适用")
    if portfolio["total_value"] is not None and portfolio["total_value"] <= 0:
        blockers.append("账户净值必须大于零才能进行数量测算")
    if (risk.get("current_weight") or 0) > 1:
        blockers.append("真实仓位超过净值，当前非融资数量模型不适用")
    if (
        risk.get("current_weight") is not None and risk.get("sector_current_weight") is not None
        and risk["current_weight"] > risk["sector_current_weight"]
    ):
        blockers.append("当前单股仓位超过行业仓位，请更新完整组合的行业仓位")
    required = (
        "current_weight",
        "max_weight",
        "risk_budget_pct",
        "stop_price",
        "sector_limit_pct",
        "sector_current_weight",
        "lot_size",
        "max_participation_pct",
        "fee_bps",
        "slippage_bps",
    )
    for field in PORTFOLIO_FIELDS:
        if portfolio[field] is None:
            blockers.append(
                f"券商尚未提供完整的{FIELD_LABELS[field]}，请同步持仓补充数据"
                if inputs["holdings_source"]["active"] else f"请填写{FIELD_LABELS[field]}"
            )
    for field in required:
        if risk[field] is None:
            blockers.append(f"请填写{FIELD_LABELS[field]}")
    if (risk.get("current_weight") or 0) > 0:
        for field in ("current_quantity", "average_cost"):
            if risk[field] is None:
                blockers.append(f"请填写{FIELD_LABELS[field]}")
    if (
        portfolio["currency"] != security.currency
        or observation.get("currency") != security.currency
    ):
        blockers.append("组合、证券与行情币种需一致；尚未接入可验证汇率换算")
    price = number(observation.get("price"))
    observed_at = parse_time(observation.get("as_of"))
    quote_age = quote_session_age(security.market, observed_at, current) if observed_at else None
    if price is None or price <= 0 or not observation.get("valid"):
        blockers.append("需要有效且可核对来源的报价")
    if observed_at is None or observed_at > current or quote_age is None or quote_age > 1:
        blockers.append("报价时间缺失、异常或过期")
    if observation.get("price_basis") != "raw":
        blockers.append("可交易数量需使用未复权实际报价")
    if market.get("status") != "ready":
        blockers.extend(market.get("blockers") or ["行情及公司行动口径未核实"])
    liquidity = market.get("liquidity") or {}
    average_volume = number(liquidity.get("average_volume_20"))
    if not liquidity.get("sizing_volume_ready") or not average_volume or average_volume <= 0:
        blockers.append("需具备单位明确且排除公司行动干扰的 20 日量能数据")
    holdings_note = inputs["holdings_source"].get("note")
    result: dict[str, Any] = {
        "status": "blocked",
        "security_id": security_id,
        "horizon": horizon,
        "inputs": inputs,
        "quote": observation,
        "position_review": _position_review(
            inputs, security, observation,
            quote_usable=bool(
                market.get("status") == "ready" and price is not None and price > 0
                and observation.get("valid") and observation.get("price_basis") == "raw"
                and observation.get("currency") == security.currency
                and observed_at is not None and observed_at <= current
                and quote_age is not None and quote_age <= 1
            ),
        ),
        "blockers": list(dict.fromkeys(blockers)),
        "max_buy_quantity": None,
        "required_reduce_quantity": None,
        "target_quantity_cap": None,
        "costs": {
            "fee_bps": risk["fee_bps"],
            "slippage_bps": risk["slippage_bps"],
            "basis": "per_side",
        },
        "note": (
            (f"{holdings_note} " if holdings_note else "")
            + "这是用户风险约束下的数量上限，基于最近完整日线；"
            "下单前需核对实时价格和实际可成交量。"
        ),
    }
    if blockers or price is None or average_volume is None:
        return result
    total = float(portfolio["total_value"])
    quantity = float(risk["current_quantity"] or 0)
    lot = int(risk["lot_size"])
    current_weight = float(risk["current_weight"])
    implied_weight = quantity * price / total
    if abs(implied_weight - current_weight) > max(0.005, current_weight * 0.1):
        result["blockers"].append("实际股数与当前仓位比例相差较大，请按同一报价确认持仓")
        return result
    costs = (float(risk["fee_bps"]) + float(risk["slippage_bps"])) / 10000

    def floor_lot(value: float) -> int:
        return max(0, math.floor(value / lot) * lot)

    liquidity_cap = floor_lot(average_volume * float(risk["max_participation_pct"]))
    stop = float(risk["stop_price"])
    if stop >= price:
        target = 0
        loss_per_share = None
    else:
        loss_per_share = price - stop + 2 * costs * price
        risk_cap = floor_lot(total * float(risk["risk_budget_pct"]) / loss_per_share)
        weight_cap = floor_lot(total * float(risk["max_weight"]) / price)
        sector_delta = float(risk["sector_limit_pct"]) - float(risk["sector_current_weight"])
        sector_cap = floor_lot(max(0, quantity + total * sector_delta / price))
        target = min(risk_cap, weight_cap, sector_cap)
    cash_cap = floor_lot(float(portfolio["available_cash"]) / (price * (1 + costs)))
    buy = min(floor_lot(max(0, target - quantity)), cash_cap, liquidity_cap)
    reduction = min(quantity, math.ceil(max(0, quantity - target) / lot) * lot)
    result.update(
        status="stop_triggered" if stop >= price else "ready",
        target_quantity_cap=target,
        max_buy_quantity=buy,
        required_reduce_quantity=reduction,
        max_reduce_quantity_now=min(reduction, liquidity_cap),
        daily_liquidity_quantity_cap=liquidity_cap,
        loss_per_share=loss_per_share,
        loss_budget_amount=total * float(risk["risk_budget_pct"]),
        current_position_value=quantity * price,
        unrealized_pnl=(price - float(risk["average_cost"])) * quantity if quantity else 0,
        stop_triggered=stop >= price,
    )
    return result


def _position_review(
    inputs: dict[str, Any], security: Security, quote: dict[str, Any], *, quote_usable: bool,
) -> dict[str, Any]:
    """Assess known position limits even when funds or sector gaps prevent sizing."""
    values, portfolio = inputs["security"], inputs["portfolio"]
    sources = inputs.get("field_sources") or {}
    result: dict[str, Any] = {
        "status": "partial", "alerts": [], "observations": [], "blockers": [],
        "loss_to_stop_pct": None, "quote_usable": quote_usable,
        "captured_at": inputs["holdings_source"].get("captured_at"),
    }
    quantity = number(values.get("current_quantity"))
    weight = number(values.get("current_weight"))
    if inputs["holdings_source"].get("blockers"):
        result.update(status="unavailable", blockers=inputs["holdings_source"]["blockers"])
        return result
    if quantity is not None and quantity < 0:
        result.update(status="unsupported", blockers=["空头持仓不适用当前多头风险评估"])
        return result
    if quantity == 0 and weight == 0:
        result["status"] = "flat"
        return result
    if (quantity == 0 and weight is not None and weight > 0
            or quantity is not None and quantity > 0 and weight == 0):
        result.update(status="unavailable", blockers=["实际股数与持仓比例不一致，请先核对持仓"])
        return result

    def basis(field: str) -> str:
        return "默认" if sources.get(field, {}).get("kind") == "default" else "已设置的"

    def alert(code: str, title: str, detail: str, fields: tuple[str, ...]) -> None:
        result["alerts"].append({
            "code": code, "title": title, "detail": detail,
            "default_fields": [field for field in fields
                               if sources.get(field, {}).get("kind") == "default"],
        })

    limit = number(values.get("max_weight"))
    if weight is not None and weight >= 0 and limit is not None and 0 < limit <= 1:
        result["observations"].append(
            f"当前仓位 {weight:.2%}，{basis('max_weight')}单股上限 {limit:.2%}。"
        )
        if weight > limit + 1e-9:
            alert("concentration", "仓位超过当前单股上限",
                  f"当前仓位 {weight:.2%}，超过{basis('max_weight')}上限 {limit:.2%}，"
                  f"相差 {(weight - limit) * 100:.2f} 个百分点；"
                  "若沿用该上限，优先评估降低集中度。", ("max_weight",))
    else:
        result["blockers"].append("持仓比例或单股上限未知")
    sector, sector_limit = number(values.get("sector_current_weight")), number(
        values.get("sector_limit_pct")
    )
    if sources.get("sector_current_weight", {}).get("kind") == "estimate":
        result["blockers"].append("行业敞口尚未完整核实")
    elif sector is not None and sector_limit is not None and sector > sector_limit + 1e-9:
        alert("sector", "行业仓位超过当前上限",
              f"当前行业仓位 {sector:.2%}，{basis('sector_limit_pct')}上限 {sector_limit:.2%}；"
              "先评估降低行业整体敞口，再决定调整哪只股票。", ("sector_limit_pct",))
    elif sector is None:
        result["blockers"].append("行业敞口未知")
    stop, price = number(values.get("stop_price")), number(quote.get("price"))
    if not quote_usable or price is None or stop is None or stop <= 0:
        result["blockers"].append("需有效同币种报价和失效价才能检查退出条件")
    elif quantity is not None and quantity > 0:
        result["observations"].append(
            f"最近完整日线 {price:g} {security.currency}，{basis('stop_price')}失效价 {stop:g}。"
        )
        if price <= stop:
            alert("stop", "最近报价已触及当前失效价",
                  f"报价 {price:g} {security.currency} 已不高于{basis('stop_price')}失效价 "
                  f"{stop:g}；核对实时价格后，优先评估退出。", ("stop_price",))
        else:
            total = number(portfolio.get("total_value"))
            budget = number(values.get("risk_budget_pct"))
            fee, slippage = number(values.get("fee_bps")), number(values.get("slippage_bps"))
            if (total is not None and total > 0 and budget is not None and 0 < budget <= 1
                    and fee is not None and 0 <= fee < 10000
                    and slippage is not None and 0 <= slippage < 10000
                    and portfolio.get("currency") == security.currency and weight is not None
                    and abs(quantity * price / total - weight) <= max(0.005, abs(weight) * 0.1)):
                loss = quantity * (price - stop + 2 * price * (fee + slippage) / 10000) / total
                result["loss_to_stop_pct"] = loss
                if loss > budget + 1e-9:
                    alert("loss_budget", "失效价情景损失超过当前风险预算",
                          f"按{basis('stop_price')}失效价 {stop:g} 及费用估算，"
                          f"情景损失占净值 {loss:.2%}，"
                          f"高于{basis('risk_budget_pct')}预算 {budget:.2%}；"
                          "先评估降低风险。失效价不保证实际成交价格。",
                          ("risk_budget_pct", "stop_price", "fee_bps", "slippage_bps"))
            else:
                result["blockers"].append("同口径净值、持仓或费用资料不足，情景损失尚未计算")
    result["alerts"].sort(key=lambda item: {"stop": 0, "concentration": 1,
                                          "loss_budget": 2, "sector": 3}[item["code"]])
    result["status"] = "attention" if result["alerts"] else (
        "partial" if result["blockers"] else "checked"
    )
    return result
