"""Immutable action inputs and cost-aware, descriptive forward calibration."""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Security
from ..risk_models import ActionDecisionSnapshot, ActionEvaluationResult, ActionValidationState
from .history_repository import HistoryRepository
from .market_research import adjusted_window_valid, calendar_for_market, closed_bars, number, utc
from .providers import MarketDataProvider, build_market_data_provider
from .scoring import evidence_rule_version
from .validation_history import read_validation_history

RECORDING_VERSION = "execution-actions-v2"
LEGACY_RECORDING_VERSION = "legacy-v1"
ACTION_EVALUATION_VERSION = "dated-history-v1"
VALIDATION_LABELS = {
    "unassessed": "尚未检查", "queued": "已安排复查", "not_mature": "交易窗口尚未成熟",
    "source_failed": "行情来源不可用", "history_missing": "历史行情缺失",
    "adjustment_unverified": "复权口径未核实", "currency_mismatch": "行情币种不匹配",
    "volume_unknown": "成交量缺失", "price_invalid": "行情价格无效",
    "calendar_unavailable": "交易日历范围不可用", "complete": "已完成验证",
    "suspension_limit": "停牌超出本批检查范围",
}


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def record_action_snapshot(
    session: Session,
    security_id: int,
    horizon: int,
    judgment: dict[str, Any],
    *,
    signal: dict[str, Any] | None = None,
    market_research: dict[str, Any] | None = None,
    risk_plan: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if horizon not in {1, 5, 20}:
        raise ValueError("行动周期必须为 1、5、20")
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券不存在")
    action = judgment.get("action") or {}
    has_strategy = judgment.get("strategy") is not None
    strategy = judgment.get("strategy") or {}
    candidate = strategy.get("candidate") or {}
    execution = strategy.get("execution") or {}
    direction = (
        {"positive": "bullish", "negative": "bearish"}.get(str(candidate.get("code")), "neutral")
        if has_strategy
        else str((signal or {}).get("direction") or "neutral")
    )
    ready = (
        execution.get("ready") is True
        if has_strategy
        else action.get("code") in {"buy_candidate", "hold", "reduce", "sell_candidate"}
    )
    action_code = str(
        (execution.get("action_code") if has_strategy else action.get("code")) or "review"
    )
    policy = str(strategy.get("policy_version") or action.get("policy_version") or "unversioned")
    market = market_research or {}
    risk = risk_plan or {}
    evidence_ids = sorted((signal or {}).get("evidence_event_ids") or [])
    evidence_version = evidence_rule_version((signal or {}).get("components"))
    identity = {
        "security_id": security_id,
        "horizon": horizon,
        "policy_version": policy,
        "recording_version": RECORDING_VERSION,
        "evidence_rule_version": evidence_version,
        "action_code": action_code,
        "candidate": candidate,
        "execution_ready": ready,
        "signal": signal,
        "quote": market.get("quote"),
        "horizons": market.get("horizons"),
        "benchmark": market.get("benchmark"),
        "risk_inputs": risk.get("inputs"),
        "risk_costs": risk.get("costs"),
        "valuation": strategy.get("valuation"),
        "pricing": strategy.get("pricing"),
    }
    dedupe_key = _digest(identity)
    previous = session.scalar(
        select(ActionDecisionSnapshot).where(ActionDecisionSnapshot.dedupe_key == dedupe_key)
    )
    if previous is not None:
        return {"id": previous.id, "created": False, "dedupe_key": dedupe_key}
    inputs = _json_value(
        {
            "recording_version": RECORDING_VERSION,
            "evidence_rule_version": evidence_version,
            "judgment": judgment,
            "signal": signal,
            "market_research": market,
            "risk_plan": risk,
            "execution_ready": ready,
            "candidate_code": candidate.get("code"),
            "security": {
                "symbol": security.symbol,
                "market": security.market,
                "currency": security.currency,
                "timezone": security.timezone,
            },
            "evidence_episode_key": _digest(
                {
                    "security_id": security_id,
                    "evidence_ids": evidence_ids,
                    "signal_id": None if evidence_ids else (signal or {}).get("id"),
                }
            ),
        }
    )
    snapshot = ActionDecisionSnapshot(
        security_id=security_id,
        horizon=horizon,
        as_of=utc(now or datetime.now(UTC)),
        policy_version=policy,
        market=security.market,
        action_code=action_code,
        direction=direction,
        confidence=number((signal or {}).get("confidence")),
        dedupe_key=dedupe_key,
        inputs=inputs,
    )
    session.add(snapshot)
    session.flush()
    return {"id": snapshot.id, "created": True, "dedupe_key": dedupe_key}


def _evaluate_window(
    snapshot: ActionDecisionSnapshot,
    frame: pd.DataFrame,
    security: Security,
    now: datetime,
    benchmark_frame: pd.DataFrame | None,
) -> dict[str, Any] | None:
    bars = closed_bars(frame, security.market, security.timezone, now)
    if not bars:
        return None
    calendar = calendar_for_market(security.market)
    snapshot_date = pd.Timestamp(utc(snapshot.as_of).astimezone(calendar.tz).date())
    if snapshot_date < calendar.first_session or snapshot_date > calendar.last_session:
        return None
    schedule = calendar.schedule
    eligible = schedule[
        (schedule["open"] > pd.Timestamp(utc(snapshot.as_of)))
        & (schedule["close"] <= pd.Timestamp(utc(now)))
    ]
    by_date = {bar["date"]: bar for bar in bars}
    window = []
    for date in eligible.index:
        bar = by_date.get(date.date().isoformat())
        # Missing history is not evidence of suspension and must never shift entry forward.
        if bar is None or bar.get("volume") is None:
            return None
        if bar["volume"] == 0:
            continue
        if bar["volume"] < 0:
            return None
        window.append(bar)
        if len(window) == snapshot.horizon:
            break
    if len(window) != snapshot.horizon:
        return None
    metadata = dict(frame.attrs)
    if not adjusted_window_valid(window, metadata):
        return None
    if metadata.get("currency") != security.currency:
        return None
    opening, closing = window[0]["adj_open"], window[-1]["adj_close"]
    gross_return = (closing / opening - 1) * 100
    peaks = opening
    drawdown = 0.0
    for bar in window:
        peaks = max(peaks, bar["adj_close"])
        drawdown = min(drawdown, (bar["adj_close"] / peaks - 1) * 100)
    adverse = min((bar["adj_low"] / opening - 1) * 100 for bar in window)
    costs = (snapshot.inputs.get("risk_plan") or {}).get("costs") or {}
    fee, slippage = number(costs.get("fee_bps")), number(costs.get("slippage_bps"))
    cost_rate = (
        (fee + slippage) / 10000
        if fee is not None
        and slippage is not None
        and fee >= 0
        and slippage >= 0
        and fee + slippage < 10000
        else None
    )
    net = (
        (closing / opening * (1 - cost_rate) / (1 + cost_rate) - 1) * 100
        if cost_rate is not None
        else None
    )
    avoided = ((1 - cost_rate) - closing / opening) * 100 if cost_rate is not None else None
    benefit = (
        net
        if snapshot.direction == "bullish"
        else avoided
        if snapshot.direction == "bearish"
        else None
    )
    industry_return = None
    matched = []
    if benchmark_frame is not None:
        benchmark_bars = closed_bars(benchmark_frame, security.market, security.timezone, now)
        indexed = {bar["date"]: bar for bar in benchmark_bars}
        matched = [indexed[bar["date"]] for bar in window if bar["date"] in indexed]
        benchmark_meta = dict(benchmark_frame.attrs)
        if (
            len(matched) == len(window)
            and adjusted_window_valid(matched, benchmark_meta)
            and benchmark_meta.get("analysis_price_basis") == metadata.get("analysis_price_basis")
            and benchmark_meta.get("currency") == metadata.get("currency")
        ):
            industry_return = (matched[-1]["adj_close"] / matched[0]["adj_open"] - 1) * 100
    return {
        "status": "complete",
        "evaluation_version": ACTION_EVALUATION_VERSION,
        "horizon": snapshot.horizon,
        "entry_date": window[0]["date"],
        "exit_date": window[-1]["date"],
        "entry_raw_price": window[0]["open"],
        "exit_raw_price": window[-1]["close"],
        "absolute_return_pct": gross_return,
        "net_long_return_pct": net,
        "avoided_loss_after_exit_cost_pct": avoided,
        "directional_benefit_pct": benefit,
        "costs_known": cost_rate is not None,
        "industry_return_pct": industry_return,
        "industry_excess_return_pct": gross_return - industry_return
        if industry_return is not None
        else None,
        "max_drawdown_close_pct": drawdown,
        "max_adverse_excursion_pct": adverse,
        "price_basis": metadata.get("analysis_price_basis"),
        "source": metadata.get("source"),
        "source_version": metadata.get("source_version"),
        "market_metadata": _json_value(metadata),
        "benchmark_metadata": _json_value(dict(benchmark_frame.attrs))
        if benchmark_frame is not None else None,
        "window_bars": _json_value(window),
        "benchmark_window_bars": _json_value(matched),
        "execution_ready_at_recording": snapshot.inputs.get("execution_ready") is True,
        "direction_correct_gross": (
            gross_return > 0
            if snapshot.direction == "bullish"
            else gross_return < 0
            if snapshot.direction == "bearish"
            else None
        ),
        "costs": costs,
        "method": "下一个可交易日开盘至第 N 个实际交易日收盘，使用核实后的复权 OHLC",
        "interpretation": (
            "这是固定窗口对照观察，不是实际成交账本；正向观察扣双边费用与滑点，"
            "负向观察比较开盘退出后的现金与继续持有，非做空收益。"
            "方向收益衡量事件候选方向，不代表所记录减仓、退出等动作的胜率。"
        ),
    }


def _schedule(snapshot: ActionDecisionSnapshot, security: Security) -> pd.DataFrame:
    calendar = calendar_for_market(security.market)
    day = pd.Timestamp(utc(snapshot.as_of).astimezone(calendar.tz).date())
    if day < calendar.first_session or day > calendar.last_session:
        raise ValueError("交易日历超出覆盖范围")
    schedule = calendar.schedule
    return schedule[schedule["open"] > pd.Timestamp(utc(snapshot.as_of))]


def _window_problem(
    snapshot: ActionDecisionSnapshot, security: Security, frame: pd.DataFrame,
    schedule: pd.DataFrame, current: datetime,
) -> dict[str, Any]:
    if frame.index.has_duplicates:
        return {"status": "price_invalid", "missing_fields": ["unique_trade_date"]}
    eligible = schedule[schedule["close"] <= pd.Timestamp(current)]
    indexed = {bar["date"]: bar for bar in closed_bars(
        frame, security.market, security.timezone, current,
    )}
    selected = []
    missing = []
    unknown_volume = []
    for day in eligible.index:
        key = day.date().isoformat()
        bar = indexed.get(key)
        if bar is None:
            missing.append(key)
        elif bar.get("volume") is None or bar["volume"] < 0:
            unknown_volume.append(key)
        elif bar["volume"] == 0:
            continue
        else:
            selected.append(bar)
        if len(selected) + len(missing) + len(unknown_volume) >= snapshot.horizon:
            break
    if missing:
        return {"status": "history_missing", "missing_dates": missing}
    if unknown_volume:
        return {"status": "volume_unknown", "missing_dates": unknown_volume,
                "missing_fields": ["Volume"]}
    if len(selected) < snapshot.horizon:
        return {"status": "not_mature"}
    if frame.attrs.get("currency") != security.currency:
        return {"status": "currency_mismatch", "missing_fields": ["currency"]}
    if not all(bar.get("valid") for bar in selected):
        return {"status": "price_invalid", "missing_fields": ["OHLC"]}
    if not adjusted_window_valid(selected, dict(frame.attrs)):
        return {"status": "adjustment_unverified", "missing_fields": ["adjusted_OHLC"]}
    return {"status": "ready"}


def retry_action_validation(
    session: Session, snapshot_id: int, now: datetime | None = None,
) -> dict[str, Any]:
    """Explicit retry reopens backoff; no source calls occur in this mutation."""
    if session.get(ActionDecisionSnapshot, snapshot_id) is None:
        raise LookupError("行动快照不存在")
    if session.scalar(select(ActionEvaluationResult.id).where(
        ActionEvaluationResult.snapshot_id == snapshot_id,
    )) is not None:
        raise ValueError("已有不可变验证结果，无需重试")
    state = session.get(ActionValidationState, snapshot_id)
    if state is None:
        state = ActionValidationState(snapshot_id=snapshot_id)
        session.add(state)
    state.status, state.reason = "queued", VALIDATION_LABELS["queued"]
    state.next_attempt_at = utc(now or datetime.now(UTC))
    session.flush()
    return {"snapshot_id": snapshot_id, "status": state.status,
            "next_attempt_at": state.next_attempt_at}


def evaluate_action_snapshots(
    session: Session,
    settings: Settings,
    now: datetime | None = None,
    provider: MarketDataProvider | None = None,
    batch_size: int = 500,
) -> dict[str, Any]:
    if not 1 <= batch_size <= 500:
        raise ValueError("每批验证数量须为 1–500")
    current = utc(now or datetime.now(UTC))
    data_provider = provider or build_market_data_provider(settings)
    selected = list(session.execute(
        select(ActionDecisionSnapshot, ActionValidationState)
        .outerjoin(ActionEvaluationResult,
                   ActionEvaluationResult.snapshot_id == ActionDecisionSnapshot.id)
        .outerjoin(ActionValidationState,
                   ActionValidationState.snapshot_id == ActionDecisionSnapshot.id)
        .where(ActionEvaluationResult.id.is_(None), or_(
            ActionValidationState.snapshot_id.is_(None),
            ActionValidationState.next_attempt_at <= current,
        ))
        .order_by(ActionDecisionSnapshot.id).limit(batch_size)
    ))
    repository = HistoryRepository(session, data_provider, allow_fetch=True)
    completed = 0
    pending = []
    outcomes = []
    updates = []
    for snapshot, old_state in selected:
        security = session.get(Security, snapshot.security_id)
        if security is None:
            continue
        values: dict[str, Any] = {
            "last_attempt_at": current, "attempts": (old_state.attempts if old_state else 0) + 1,
            "missing_dates": [], "missing_fields": [], "due_at": None,
        }
        payload = None
        try:
            schedule = _schedule(snapshot, security)
            calendar = calendar_for_market(security.market)
            if pd.Timestamp(current.astimezone(calendar.tz).date()) > calendar.last_session:
                raise ValueError("当前时间超出交易日历范围")
            if len(schedule) < snapshot.horizon:
                raise ValueError("交易日历无法覆盖验证窗口")
            values["due_at"] = schedule.iloc[snapshot.horizon - 1]["close"].to_pydatetime()
            values["window_start"] = schedule.index[0].date()
            values["window_end"] = schedule.index[snapshot.horizon - 1].date()
            if utc(values["due_at"]) > current:
                values["status"] = "not_mature"
            else:
                closed = schedule[schedule["close"] <= pd.Timestamp(current)]
                try:
                    frame, window_end, limit_reason = read_validation_history(
                        repository, security, closed, snapshot.horizon,
                    )
                    values["window_end"] = window_end
                    if limit_reason:
                        values["status"] = limit_reason
                    else:
                        values.update(_window_problem(snapshot, security, frame, schedule, current))
                except Exception:
                    values["status"] = "source_failed"
        except ValueError:
            values["status"] = "calendar_unavailable"
        except Exception:
            values["status"] = "source_failed"
        benchmark = (snapshot.inputs.get("market_research") or {}).get("benchmark") or {}
        benchmark_frame = None
        if values["status"] == "ready":
            if benchmark.get("market") == security.market and benchmark.get("symbol"):
                try:
                    benchmark_frame = repository.history(
                        repository.security(security.market, str(benchmark["symbol"])),
                        values["window_start"], values["window_end"],
                    )
                except Exception:
                    pass
            payload = _evaluate_window(snapshot, frame, security, current, benchmark_frame)
            if payload is None:
                values["status"] = "price_invalid"
        if payload is None:
            status = values["status"]
            if status == "not_mature":
                future = schedule[schedule["close"] > pd.Timestamp(current)]
                next_close = future.iloc[0]["close"].to_pydatetime() if not future.empty else None
                values["next_attempt_at"] = max(
                    utc(values["due_at"]), utc(next_close),
                ) if next_close else current + timedelta(days=1)
            else:
                delays = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6),
                          timedelta(days=1), timedelta(days=7))
                values["next_attempt_at"] = current + delays[min(values["attempts"] - 1, 4)]
            values["reason"] = VALIDATION_LABELS[status]
            pending.append({"snapshot_id": snapshot.id, **_json_value(values)})
        else:
            values.update(status="complete", reason=VALIDATION_LABELS["complete"],
                          next_attempt_at=None, window_start=datetime.fromisoformat(
                              payload["entry_date"]).date(), window_end=datetime.fromisoformat(
                              payload["exit_date"]).date())
            outcomes.append(ActionEvaluationResult(
                snapshot_id=snapshot.id, evaluated_at=current, payload=payload,
            ))
            completed += 1
        updates.append((snapshot.id, old_state, values))
    # Stage all writes until the last provider request, including state updates.
    versions = repository.persist(session, current)
    for outcome in outcomes:
        version = versions.get(outcome.payload["market_metadata"].get("source_version"))
        if version:
            outcome.payload["source_version"] = version
            outcome.payload["market_metadata"]["source_version"] = version
        version = versions.get(str(
            (outcome.payload.get("benchmark_metadata") or {}).get("source_version") or ""
        ))
        if version and outcome.payload.get("benchmark_metadata"):
            outcome.payload["benchmark_metadata"]["source_version"] = version
    for identifier, state, values in updates:
        if state is None:
            state = ActionValidationState(snapshot_id=identifier)
            session.add(state)
        for field, value in values.items():
            setattr(state, field, value)
    session.add_all(outcomes)
    session.flush()
    return {"completed": completed, "pending": len(pending), "pending_details": pending,
            "processed": len(selected), "batch_limit": batch_size}


def _wilson(successes: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.96
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def calibration_report(session: Session, min_samples: int = 30) -> dict[str, Any]:
    if min_samples < 2:
        raise ValueError("最少样本门槛不能小于 2")
    grouped: dict[
        tuple[str, int, str, bool, str, str, str, str, str],
        list[tuple[ActionDecisionSnapshot, ActionEvaluationResult]],
    ] = defaultdict(list)
    for snapshot, outcome in session.execute(
        select(ActionDecisionSnapshot, ActionEvaluationResult)
        .join(
            ActionEvaluationResult, ActionEvaluationResult.snapshot_id == ActionDecisionSnapshot.id
        )
        .order_by(ActionDecisionSnapshot.as_of, ActionDecisionSnapshot.id)
    ):
        key = (
            snapshot.market,
            snapshot.horizon,
            snapshot.policy_version,
            snapshot.inputs.get("execution_ready") is True,
            snapshot.direction,
            str(snapshot.inputs.get("recording_version") or LEGACY_RECORDING_VERSION),
            str(outcome.payload.get("evaluation_version") or "legacy-v1"),
            snapshot.action_code,
            str(snapshot.inputs.get("evidence_rule_version") or "legacy-v1"),
        )
        grouped[key].append((snapshot, outcome))
    groups = []
    for key, records in grouped.items():
        unique: dict[str, tuple[ActionDecisionSnapshot, ActionEvaluationResult]] = {}
        for snapshot, outcome in records:
            episode = snapshot.inputs.get("evidence_episode_key") or str(snapshot.id)
            unique.setdefault(episode, (snapshot, outcome))
        samples = list(unique.values())
        directional = [
            (snapshot, outcome) for snapshot, outcome in samples if snapshot.direction != "neutral"
        ]
        n = len(directional)
        successes = sum(
            outcome.payload.get("direction_correct_gross") is True for _, outcome in directional
        )
        benefits = [
            value
            for _, outcome in directional
            if (value := number(outcome.payload.get("directional_benefit_pct"))) is not None
        ]
        returns = [float(outcome.payload["absolute_return_pct"]) for _, outcome in directional]
        drawdowns = [float(outcome.payload["max_drawdown_close_pct"]) for _, outcome in directional]
        industry_excess = [
            value
            for _, outcome in directional
            if (value := number(outcome.payload.get("industry_excess_return_pct"))) is not None
        ]
        reliability = []
        for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
            bucket = [
                (snapshot, outcome)
                for snapshot, outcome in directional
                if snapshot.confidence is not None
                and lower <= snapshot.confidence
                and (
                    snapshot.confidence < lower + 0.2 or (lower == 0.8 and snapshot.confidence <= 1)
                )
            ]
            hits = sum(
                outcome.payload.get("direction_correct_gross") is True for _, outcome in bucket
            )
            reliability.append(
                {
                    "confidence_range": [lower, min(1, lower + 0.2)],
                    "sample_count": len(bucket),
                    "empirical_direction_accuracy": hits / len(bucket)
                    if len(bucket) >= min_samples
                    else None,
                    "interval_95": _wilson(hits, len(bucket))
                    if len(bucket) >= min_samples
                    else None,
                    "status": "descriptive_only"
                    if len(bucket) >= min_samples
                    else "insufficient_samples",
                }
            )
        enough = n >= min_samples
        groups.append(
            {
                "market": key[0],
                "horizon": key[1],
                "policy_version": key[2],
                "execution_ready": key[3],
                "direction": key[4],
                "recording_version": key[5],
                "evaluation_version": key[6],
                "action_code": key[7],
                "evidence_rule_version": key[8],
                "record_count": len(records),
                "independent_sample_count": n,
                "duplicates_excluded": len(records) - len(samples),
                "status": "descriptive_only" if enough else "insufficient_samples",
                "direction_accuracy": successes / n if enough else None,
                "direction_accuracy_interval_95": _wilson(successes, n) if enough else None,
                "cost_known_sample_count": len(benefits),
                "mean_directional_benefit_pct": sum(benefits) / len(benefits)
                if len(benefits) >= min_samples
                else None,
                "absolute_return_percentiles": {
                    str(q): float(pd.Series(returns).quantile(q)) for q in (0.1, 0.5, 0.9)
                }
                if enough
                else None,
                "worst_close_drawdown_pct": min(drawdowns) if enough else None,
                "industry_excess_sample_count": len(industry_excess),
                "industry_excess_return_percentiles": {
                    str(q): float(pd.Series(industry_excess).quantile(q)) for q in (0.1, 0.5, 0.9)
                }
                if len(industry_excess) >= min_samples
                else None,
                "reliability": reliability,
                "calibrated_probability": None,
            }
        )
    total = session.scalar(select(func.count()).select_from(ActionDecisionSnapshot)) or 0
    completed = session.scalar(select(func.count()).select_from(ActionEvaluationResult)) or 0
    pending_query = (
        select(ActionDecisionSnapshot, ActionValidationState)
        .outerjoin(ActionEvaluationResult,
                   ActionEvaluationResult.snapshot_id == ActionDecisionSnapshot.id)
        .outerjoin(ActionValidationState,
                   ActionValidationState.snapshot_id == ActionDecisionSnapshot.id)
        .where(ActionEvaluationResult.id.is_(None))
    )
    status_column = func.coalesce(ActionValidationState.status, "unassessed")
    reason_counts = session.execute(pending_query.with_only_columns(
        status_column, func.count(ActionDecisionSnapshot.id),
    ).group_by(status_column)).all()
    samples = []
    for snapshot, state in session.execute(pending_query.order_by(
        ActionDecisionSnapshot.id,
    ).limit(50)):
        samples.append(_json_value({
            "snapshot_id": snapshot.id, "security_id": snapshot.security_id,
            "market": snapshot.market, "horizon": snapshot.horizon,
            "action_code": snapshot.action_code, "recorded_at": snapshot.as_of,
            "evidence_rule_version": str(
                snapshot.inputs.get("evidence_rule_version") or "legacy-v1"
            ),
            "status": state.status if state else "unassessed",
            "reason": state.reason if state else VALIDATION_LABELS["unassessed"],
            **{field: getattr(state, field) if state else None for field in (
                "due_at", "window_start", "window_end", "missing_dates", "missing_fields",
                "attempts", "last_attempt_at", "next_attempt_at",
            )},
        }))
    return {
        "total_snapshot_count": total,
        "completed_count": completed,
        "pending_count": total - completed,
        "pending_by_reason": [
            {"status": status, "label": VALIDATION_LABELS.get(status, status), "count": count}
            for status, count in reason_counts
        ],
        "pending_samples": samples,
        "pending_sample_limit": 50,
        "minimum_samples": min_samples,
        "groups": groups,
        "calibration_status": "descriptive_only",
        "note": (
            "按市场、周期、规则版本、行动记录口径、事件方向和执行就绪状态分组；"
            "同一事件组只取首次观察，旧记录与新版执行动作分开统计；"
            "成本未知不按零处理。样本门槛只控制展示，不构成策略有效性证明；"
            "方向命中率衡量事件方向，不代表减仓或退出动作的胜率。"
            "置信度不是胜率，当前仅作前向描述与分组校准诊断，未调权重或交易阈值。"
        ),
    }
