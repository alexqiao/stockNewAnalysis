"""Deterministic, close-confirmed Bollinger references; no orders or return simulation."""

from __future__ import annotations

import math
from datetime import date, datetime
from statistics import mean, pstdev
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class BollingerParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    bb_period: Literal[20] = 20
    bb_std: Literal[2] = 2
    atr_period: Literal[14] = 14
    pivot_left: Literal[2] = 2
    pivot_right: Literal[2] = 2
    volume_lookback: Literal[20] = 20
    contraction_bars: Literal[3] = 3
    squeeze_lookback: int = Field(default=120, ge=20, le=500)
    squeeze_percentile: float = Field(default=20, ge=0, le=100)
    squeeze_days: int = Field(default=3, ge=1, le=20)
    breakout_min_pct: float = Field(default=1, ge=0, le=20)
    expansion_ratio: float = Field(default=1.1, ge=1, le=5)
    slope_lookback: int = Field(default=5, ge=1, le=20)
    slope_min_pct: float = Field(default=0.5, ge=0, le=20)
    lower_slope_lookback: int = Field(default=5, ge=1, le=20)
    lower_slope_min_pct: float = Field(default=0.5, ge=0.01, le=20)
    touch_tolerance_pct: float = Field(default=0.5, ge=0.01, le=5)
    pullback_max_bars: int = Field(default=20, ge=1, le=120)
    max_stop_pct: float = Field(default=8, gt=0, le=50)
    profit_min_pct: float = Field(default=10, ge=0, le=100)
    gap_min_pct: float = Field(default=0.5, ge=0, le=20)
    volume_max_ratio: float = Field(default=0.8, gt=0, le=1)
    contraction_max_ratio: float = Field(default=0.7, gt=0, le=2)
    stop_atr_buffer: float = Field(default=0.25, ge=0, le=3)
    support_cluster_pct: float = Field(default=0.5, ge=0, le=5)


SETUP_LABELS = {
    "watch": "观察布林形态",
    "squeeze": "缩口已形成",
    "waiting_pullback": "等待首次回踩",
    "pullback_confirmed": "首次回踩已确认",
    "pullback_failed": "首次回踩未通过",
    "expired": "形态观察窗口已过期",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _positive(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _check(key: str, label: str, passed: bool | None, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "passed": passed, "detail": detail}


def _empty(parameters: BollingerParameters, adjustment: str) -> dict[str, Any]:
    return {
        "status": "insufficient",
        "price_basis": adjustment,
        "as_of": None,
        "action": {"code": "wait", "label": "等待数据", "reason": "缺少完整日线"},
        "setup": {
            "state": "watch",
            "label": SETUP_LABELS["watch"],
            "breakout_date": None,
            "first_pullback_date": None,
        },
        "checks": [
            _check(
                "lower_band_rising",
                "下轨上行（加仓待条件）",
                None,
                "下轨数据不足或无效，方向尚未核实",
            )
        ],
        "lower_band": {
            "direction": "unknown",
            "label": "下轨方向待核实",
            "slope_pct": None,
            "lookback": parameters.lower_slope_lookback,
            "threshold_pct": parameters.lower_slope_min_pct,
            "reason": "需要最新及回看起点的有效正数下轨，缺失时不推定方向",
        },
        "levels": {
            "upper": None,
            "middle": None,
            "lower": None,
            "entry_reference": None,
            "initial_stop": None,
            "initial_risk_pct": None,
            "supports": [],
            "trailing_stop": None,
            "current_stop": None,
        },
        "position": {"status": "unknown", "reliable": False, "profit_pct": None},
        "add_on": {
            "enabled": False,
            "conditions": [
                "核实已有持仓、成本与实际成交阶段",
                f"可靠成本计算的浮盈至少{parameters.profit_min_pct:g}%",
                "下轨上行条件尚未核实",
                "首次加仓仍需具体时机佐证与实际建仓批次，首版不启用加仓指令",
                "持续加仓须出现新一轮缩口周期",
                "加仓与止损调整后的总风险不得扩大，并受账户风险上限约束",
                "20/20/20/40为计划仓位分批比例，不是账户资金比例；首版不生成加仓指令",
            ],
            "tranches": [20, 20, 20, 40],
        },
        "parameters": parameters.model_dump(),
        "definitions": [
            "仅使用已收盘日线；信号收盘确认，最早供下一交易日参考，不代表已成交。",
            "布林为20日收盘均线±2倍总体标准差；带宽=(上轨-下轨)/中轨。",
            "缩口比较前一日至更早的带宽分位，至少具备完整回看窗口。",
            "突破须收盘穿越当日上轨、较前收盘涨幅达标且带宽扩张；跌破下轨取消缩口。",
            "缩口到突破、突破到首次回踩分别受pullback_max_bars观察窗口限制；过期需重新形成缩口。",
            "回踩对照前一收盘中轨；上行斜率也只用前一收盘已知数据。首次触碰即消费。",
            "回踩收盘须重返当前中轨且距中轨不超过触碰容差；突破当日不计回踩。",
            "回踩日开盘直接跌过中轨容差区，或最低价触及初始止损，均不输出买点参考。",
            "跳空开盘高于前一日最高价、前3根TR收敛、缩量三者至少满足一项；RSI背离未启用。",
            "ATR14使用Wilder平滑，初值为前14根真实波幅平均；均量使用当日前20根。",
            "摆动低点严格低于左右各2根最低价，右侧两根收盘后才确认。",
            "初始止损冻结为突破前最新已确认低点与前一日下轨的较低者，减ATR缓冲。",
            "支撑仅取当前价格下方已确认低点，按价格从近到远聚类；第二档为第二近支撑。",
            "止损触发仅比较可靠持仓已记录止损与最新收盘，不推断历史盘中触发或成交价格。",
            "追踪参考仅在可靠多头达到盈利门槛时提供，已有止损只上调不下调。",
            "下轨方向比较最新已收盘下轨与回看起点的百分比变化；上行观察支撑、下行不抄底、走平观望。",
            "下轨方向是加仓待条件，不是支撑强度或成功概率，不改变中轨首次回踩资格及已有持仓动作。",
            "历史复权可能被来源修订；固定输入按时间顺序计算，不是历史时点回测。",
        ],
        "warnings": [],
        "events": [],
    }


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _prepare(
    raw: list[dict[str, Any]],
    adjustment: str,
) -> tuple[list[dict[str, Any]], str, list[str]]:
    warnings = []
    if adjustment not in {"split_adjusted", "total_return_adjusted"}:
        raise ValueError("价格口径不支持")
    use_adjusted = adjustment == "total_return_adjusted"
    if (
        use_adjusted
        and raw
        and any(
            _positive(bar.get(f"adj_{field}")) is None
            for bar in raw
            for field in ("open", "high", "low", "close")
        )
    ):
        use_adjusted = False
        warnings.append("复权OHLC不完整，整段统一使用拆股调整价格；未混合两种口径。")
    basis = "total_return_adjusted" if use_adjusted else "split_adjusted"
    prefix = "adj_" if use_adjusted else ""
    rows: list[dict[str, Any]] = []
    previous_date = ""
    for bar in raw:
        stamp = _date(bar.get("date"))
        if stamp <= previous_date:
            raise ValueError("日线必须按交易日期严格递增且不能重复")
        previous_date = stamp
        item: dict[str, Any] = {"date": stamp}
        for field in ("open", "high", "low", "close"):
            value = _positive(bar.get(prefix + field))
            if value is None:
                raise ValueError("OHLC缺失或无效，暂停形态参考")
            item[field] = value
        if item["low"] > min(item["open"], item["close"]) or item["high"] < max(
            item["open"], item["close"]
        ):
            raise ValueError("OHLC高低价矛盾，暂停形态参考")
        volume = _number(bar.get("volume"))
        item["volume"] = volume if volume is not None and volume >= 0 else None
        rows.append(item)
    if any(bar["volume"] is None for bar in rows):
        warnings.append("部分成交量缺失，相关缩量条件标记为未知。")
    return rows, basis, warnings


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile / 100
    left = int(index)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (index - left)


def _lower_band_reference(
    result: dict[str, Any],
    rows: list[dict[str, Any]],
    p: BollingerParameters,
) -> None:
    if len(rows) <= p.lower_slope_lookback:
        return
    latest = _positive(rows[-1]["lower"])
    previous = _positive(rows[-1 - p.lower_slope_lookback]["lower"])
    if latest is None or previous is None:
        return
    slope = (latest / previous - 1) * 100
    if slope >= p.lower_slope_min_pct:
        direction, label = "rising", "下轨朝上：观察支撑"
        reason = "下轨上行达到阈值；仅满足加仓待条件之一，仍需盈利门槛与首次加仓时机佐证"
        condition = "下轨上行条件已满足；仅记录形态方向，不保证支撑有效"
    elif slope <= -p.lower_slope_min_pct:
        direction, label = "falling", "下轨朝下：不抄底"
        reason = "下轨下降达到阈值，不据此抄底或满足加仓方向条件；不单独改变已有持仓动作"
        condition = "下轨上行条件未满足：当前下轨朝下，不以低价为由抄底加仓"
    else:
        direction, label = "flat", "下轨走平：观望"
        reason = "下轨变化未越过方向阈值，保持观望；走平不表示已有多头必须卖出"
        condition = "下轨上行条件未满足：当前下轨走平，等待方向明确"
    result["lower_band"].update(
        direction=direction,
        label=label,
        slope_pct=slope,
        reason=reason,
    )
    result["checks"] = [
        _check(
            "lower_band_rising",
            "下轨上行（加仓待条件）",
            direction == "rising",
            reason,
        )
    ]
    result["add_on"]["conditions"][2] = condition


def _indicators(rows: list[dict[str, Any]], p: BollingerParameters) -> None:
    closes: list[float] = []
    ranges: list[float] = []
    atr: float | None = None
    for i, bar in enumerate(rows):
        closes.append(bar["close"])
        previous = rows[i - 1]["close"] if i else bar["close"]
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - previous), abs(bar["low"] - previous))
        ranges.append(tr)
        if len(ranges) == p.atr_period:
            atr = mean(ranges)
        elif atr is not None:
            atr = (atr * (p.atr_period - 1) + tr) / p.atr_period
        bar.update(tr=tr, atr=atr, middle=None, upper=None, lower=None, width=None)
        if len(closes) >= p.bb_period:
            window = closes[-p.bb_period :]
            middle, std = mean(window), pstdev(window)
            bar.update(
                middle=middle,
                upper=middle + p.bb_std * std,
                lower=middle - p.bb_std * std,
                width=2 * p.bb_std * std / middle,
            )


def _supports(
    pivots: list[dict[str, Any]],
    close: float,
    cluster_pct: float,
) -> list[dict[str, Any]]:
    supports: list[dict[str, Any]] = []
    for pivot in sorted(pivots, key=lambda item: (item["price"], item["date"]), reverse=True):
        if pivot["price"] >= close:
            continue
        if (
            supports
            and (supports[-1]["price"] - pivot["price"]) / supports[-1]["price"] * 100
            <= cluster_pct
        ):
            continue
        supports.append(dict(pivot))
    return supports


def _confirmations(
    rows: list[dict[str, Any]],
    i: int,
    p: BollingerParameters,
) -> tuple[bool, list[dict[str, Any]]]:
    bar, previous = rows[i], rows[i - 1]
    gap = (bar["open"] / previous["high"] - 1) * 100 >= p.gap_min_pct
    prior_tr = [item["tr"] for item in rows[max(0, i - p.contraction_bars) : i]]
    contract = bool(
        previous["atr"]
        and len(prior_tr) == p.contraction_bars
        and mean(prior_tr) <= previous["atr"] * p.contraction_max_ratio
    )
    volumes = [item["volume"] for item in rows[max(0, i - p.volume_lookback) : i]]
    volume_known = bool(
        len(volumes) == p.volume_lookback
        and all(value is not None for value in volumes)
        and bar["volume"] is not None
        and mean(volumes) > 0
    )
    volume = bar["volume"] <= mean(volumes) * p.volume_max_ratio if volume_known else None
    checks = [
        _check("gap", "跳空开盘", gap, f"开盘较前一日最高价高至少{p.gap_min_pct:g}%"),
        _check(
            "contraction",
            "波幅收敛",
            contract,
            f"此前3根平均TR不超过此前ATR14的{p.contraction_max_ratio:g}倍",
        ),
        _check(
            "volume",
            "缩量",
            volume,
            f"当日量不超过此前20根均量的{p.volume_max_ratio:g}倍；缺量不能推定缩量",
        ),
    ]
    return gap or contract or volume is True, checks


def analyze_bollinger(
    bars: list[dict[str, Any]],
    parameters: BollingerParameters | dict[str, Any] | None = None,
    adjustment: str = "total_return_adjusted",
    position: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay confirmed bars without database access, networking or future-row inspection."""
    try:
        p = (
            parameters
            if isinstance(parameters, BollingerParameters)
            else BollingerParameters.model_validate(parameters or {})
        )
    except ValidationError:
        result = _empty(BollingerParameters(), adjustment)
        result.update(status="invalid_data", warnings=["布林参数无效，请核对允许范围。"])
        result["action"] = {"code": "blocked", "label": "参数无效", "reason": result["warnings"][0]}
        return result
    result = _empty(p, adjustment)
    try:
        rows, basis, warnings = _prepare(bars, adjustment)
    except (ValueError, TypeError, AttributeError):
        result.update(status="invalid_data", warnings=["日线日期、顺序或OHLC无效，暂停形态参考。"])
        result["action"] = {
            "code": "blocked",
            "label": "行情不可用",
            "reason": result["warnings"][0],
        }
        return result
    result.update(price_basis=basis, warnings=warnings)
    if not rows:
        return result
    result["as_of"] = rows[-1]["date"]
    _indicators(rows, p)
    latest = rows[-1]
    for field in ("upper", "middle", "lower"):
        result["levels"][field] = latest[field]
    _lower_band_reference(result, rows, p)
    needed = p.bb_period + p.squeeze_lookback + p.squeeze_days - 1
    if len(rows) < needed:
        result["action"] = {
            "code": "wait",
            "label": "历史不足",
            "reason": f"当前{len(rows)}根，需要至少{needed}根完成缩口暖机",
        }
        _position_reference(result, position or {}, latest["close"], p)
        return result

    result["status"] = "ready"
    state = "watch"
    squeeze_run = 0
    squeeze_index: int | None = None
    breakout_index: int | None = None
    first_pullback: str | None = None
    initial_stop: float | None = None
    pivots: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    last_signal = False
    for i, bar in enumerate(rows):
        # Right-hand evidence confirms a pivot today; never back-date its availability.
        pivot_index = i - p.pivot_right
        if pivot_index >= p.pivot_left:
            candidate = rows[pivot_index]
            neighbors = (
                rows[pivot_index - p.pivot_left : pivot_index] + rows[pivot_index + 1 : i + 1]
            )
            if all(candidate["low"] < item["low"] for item in neighbors):
                pivots.append(
                    {
                        "price": candidate["low"],
                        "date": candidate["date"],
                        "confirmed_at": bar["date"],
                    }
                )
        if bar["width"] is None:
            continue
        previous = rows[i - 1]
        widths = [item["width"] for item in rows[max(0, i - p.squeeze_lookback) : i]]
        squeezed = bool(
            len(widths) == p.squeeze_lookback
            and all(value is not None for value in widths)
            and bar["width"] <= _percentile(widths, p.squeeze_percentile)
        )
        squeeze_preexisting = state == "squeeze"
        squeeze_run = squeeze_run + 1 if squeezed else 0
        # A fresh squeeze can start another setup only after the previous attempt ended.
        if squeeze_run == p.squeeze_days and state != "waiting_pullback":
            state, breakout_index, first_pullback, initial_stop = "squeeze", None, None, None
            squeeze_index = i
            result["events"].append({"date": bar["date"], "code": "squeeze", "label": "缩口成立"})
        elif state == "squeeze" and squeeze_run >= p.squeeze_days:
            squeeze_index = i
        last_signal = False
        old_middle = previous["middle"]
        slope_index = i - 1 - p.slope_lookback
        slope = None
        if old_middle and slope_index >= 0 and rows[slope_index]["middle"]:
            slope = (old_middle / rows[slope_index]["middle"] - 1) * 100
        rising = slope is not None and slope >= p.slope_min_pct
        near_middle = bool(
            bar["middle"] <= bar["close"] <= bar["middle"] * (1 + p.touch_tolerance_pct / 100)
        )
        confirms, extra_checks = _confirmations(rows, i, p)
        if (
            state == "squeeze"
            and squeeze_index is not None
            and i - squeeze_index > p.pullback_max_bars
        ):
            state = "expired"
            result["events"].append(
                {"date": bar["date"], "code": "squeeze_expired", "label": "缩口后的突破等待超时"}
            )
        if state == "squeeze" and squeeze_preexisting and previous["upper"] is not None:
            expanded = (
                bar["width"] > previous["width"]
                and bar["width"] >= previous["width"] * p.expansion_ratio
            )
            breakout = (
                bar["close"] > bar["upper"]
                and previous["close"] <= previous["upper"]
                and (bar["close"] / previous["close"] - 1) * 100 >= p.breakout_min_pct
                and expanded
            )
            if breakout:
                state, breakout_index, first_pullback = "waiting_pullback", i, None
                known = [item for item in pivots if item["confirmed_at"] < bar["date"]]
                if known and previous["atr"] is not None:
                    candidate_stop = (
                        min(known[-1]["price"], previous["lower"])
                        - p.stop_atr_buffer * previous["atr"]
                    )
                    initial_stop = candidate_stop if candidate_stop > 0 else None
                result["events"].append(
                    {
                        "date": bar["date"],
                        "code": "breakout",
                        "label": "收盘向上突破",
                        "initial_stop": initial_stop,
                    }
                )
            elif bar["close"] < bar["lower"]:
                state = "watch"
                result["events"].append(
                    {
                        "date": bar["date"],
                        "code": "squeeze_invalidated",
                        "label": "向下跌破，取消缩口观察",
                    }
                )
        risk = (bar["close"] - initial_stop) / bar["close"] * 100 if initial_stop else None
        risk_ok = risk is not None and 0 < risk <= p.max_stop_pct
        entry_path_clear = bool(
            initial_stop is not None
            and bar["low"] > initial_stop
            and old_middle is not None
            and bar["open"] >= old_middle * (1 - p.touch_tolerance_pct / 100)
        )
        if state == "waiting_pullback" and breakout_index is not None and i > breakout_index:
            if i - breakout_index > p.pullback_max_bars:
                state = "expired"
                result["events"].append(
                    {"date": bar["date"], "code": "expired", "label": "首次回踩等待超时"}
                )
            elif old_middle is not None and bar["low"] <= old_middle * (
                1 + p.touch_tolerance_pct / 100
            ):
                first_pullback = bar["date"]
                # Even a gap below the whole middle zone consumes this first opportunity.
                touched = bar["high"] >= old_middle * (1 - p.touch_tolerance_pct / 100)
                tradable = bar["volume"] is not None and bar["volume"] > 0
                last_signal = bool(
                    touched
                    and rising
                    and near_middle
                    and confirms
                    and risk_ok
                    and tradable
                    and entry_path_clear
                )
                state = "pullback_confirmed" if last_signal else "pullback_failed"
                result["events"].append(
                    {
                        "date": bar["date"],
                        "code": state,
                        "label": SETUP_LABELS[state],
                        "reference_middle": old_middle,
                        "initial_stop": initial_stop,
                    }
                )
        checks = [
            _check("squeeze", "缩口前置", state != "watch", "突破前需满足连续缩口"),
            _check(
                "breakout",
                "收盘突破",
                breakout_index is not None,
                "收盘上穿上轨、涨幅与带宽扩张达标",
            ),
            _check(
                "rising_middle",
                "中轨上行",
                rising,
                f"前一收盘中轨较{p.slope_lookback}根前至少上升{p.slope_min_pct:g}%",
            ),
            _check(
                "first_pullback",
                "首次回踩",
                first_pullback == bar["date"],
                "突破后第一次触碰即消费；后续触碰不重新标为首次",
            ),
            _check(
                "near_middle",
                "不追涨",
                near_middle,
                f"收盘重返当前中轨且距离不超过{p.touch_tolerance_pct:g}%",
            ),
            *extra_checks,
            _check("confirmation", "辅助确认至少一项", confirms, "跳空、收敛、缩量至少满足一项"),
            _check(
                "initial_risk",
                "初始止损距离",
                risk_ok,
                f"需突破前已确认低点，距离大于0且不超过{p.max_stop_pct:g}%",
            ),
            _check(
                "entry_path",
                "无跳空跌穿或同日触止损",
                entry_path_clear,
                "开盘未跌过中轨容差区，且最低价未触及初始止损；不推断日内成交顺序",
            ),
            _check(
                "volume_available",
                "当日有效成交量",
                bool(bar["volume"] and bar["volume"] > 0),
                "缺量或零成交量不输出买点参考",
            ),
        ]
    result["setup"].update(
        state=state,
        label=SETUP_LABELS[state],
        first_pullback_date=first_pullback,
        breakout_date=rows[breakout_index]["date"] if breakout_index is not None else None,
    )
    result["checks"] = [*checks, *result["checks"]]
    supports = _supports(pivots, latest["close"], p.support_cluster_pct)
    result["levels"].update(
        entry_reference=latest["middle"],
        initial_stop=initial_stop,
        supports=supports,
        initial_risk_pct=(latest["close"] - initial_stop) / latest["close"] * 100
        if initial_stop
        else None,
    )
    result["action"] = (
        {
            "code": "entry_reference",
            "label": "中轨买点参考",
            "reason": "最新收盘确认首次回踩且辅助条件、止损距离达标，供下一交易日核对",
        }
        if last_signal
        else {"code": "wait", "label": "等待条件", "reason": SETUP_LABELS[state]}
    )
    if not last_signal and first_pullback:
        result["action"]["reason"] = (
            f"首次回踩已于{first_pullback}评估，当前不是首次买点，等待新一轮缩口"
        )
        if initial_stop is not None and latest["close"] <= initial_stop:
            result["action"]["reason"] += "；该轮参考止损已失守，不代表实际持仓已止损"
    _position_reference(result, position or {}, latest["close"], p)
    return result


def _position_reference(
    result: dict[str, Any],
    position: dict[str, Any],
    close: float,
    p: BollingerParameters,
) -> None:
    status = position.get("status", "unknown")
    if status not in {"long", "flat", "unknown", "short"}:
        status = "unknown"
    reliable = position.get("reliable") is True
    cost, current_stop = (
        _positive(position.get("average_cost")),
        _positive(position.get("current_stop")),
    )
    profit = (close / cost - 1) * 100 if reliable and status == "long" and cost else None
    result["position"] = {"status": status, "reliable": reliable, "profit_pct": profit}
    result["levels"]["current_stop"] = current_stop
    if not reliable or status != "long":
        if status == "short":
            result["action"] = {
                "code": "blocked",
                "label": "当前持仓不适用",
                "reason": "首版仅提供多头中轨买点与止损参考",
            }
        if not reliable:
            result["warnings"].append("持仓状态或成本未核实，不输出持仓止损触发或盈利追踪建议。")
        return
    if current_stop and close <= current_stop:
        result["action"] = {
            "code": "stop_triggered",
            "label": "收盘已触及记录止损",
            "reason": "最新收盘不高于已记录止损；请核对实际持仓与委托，未推定成交价格",
        }
        return
    if result["action"]["code"] == "entry_reference":
        result["action"] = {
            "code": "wait",
            "label": "已有持仓，核对风险",
            "reason": "出现中轨回踩形态；首版不据此自动给出加仓指令",
        }
    supports = result["levels"]["supports"]
    profit_ok = profit is not None and profit >= p.profit_min_pct
    result["checks"].extend(
        [
            _check(
                "profit", "持仓盈利门槛", profit_ok, f"可靠成本计算的盈利至少{p.profit_min_pct:g}%"
            ),
            _check(
                "second_support",
                "第二档支撑",
                len(supports) >= 2,
                "当前价下方至少存在两档已确认摆动低点支撑",
            ),
        ]
    )
    if profit_ok and len(supports) >= 2:
        proposed = supports[1]["price"]
        trailing = max(current_stop, proposed) if current_stop else proposed
        result["levels"]["trailing_stop"] = trailing
        result["action"] = {
            "code": "trailing_reference",
            "label": "盈利追踪止损参考",
            "reason": (
                "第二档支撑已确认；参考止损不低于已记录值，尚未执行修改"
                if current_stop
                else "第二档支撑可作止损参考；尚无已记录止损，未执行设置"
            ),
        }
