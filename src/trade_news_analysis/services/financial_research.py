"""Point-in-time financial periods and comparable, pre-release expectations."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Security
from ..research_data_models import (
    ResearchCalendarRevision,
    ResearchExpectation,
    ResearchFinancialFact,
)

_ADDITIVE = {
    "revenue",
    "revenues",
    "revenuefromcontractwithcustomerexcludingassessedtax",
    "salesrevenuenet",
    "netincomeloss",
    "profitloss",
    "netincome",
    "operatingincomeloss",
    "netcashprovidedbyusedinoperatingactivities",
    "paymentstoacquirepropertyplantandequipment",
    "grossprofit",
    "costofrevenue",
    "costofgoodsandservicessold",
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _kind(fact: ResearchFinancialFact) -> str:
    if fact.period_start is None:
        return "instant_or_unspecified"
    days = (fact.period_end - fact.period_start).days + 1
    if 80 <= days <= 100:
        return "quarter"
    if 330 <= days <= 380:
        return "annual"
    if 150 <= days <= 200 or 240 <= days <= 290:
        return "ytd"
    return "unknown"


def _fact(fact: ResearchFinancialFact) -> dict[str, Any]:
    return {
        "fact_id": fact.id,
        "value": fact.value,
        "unit": fact.unit,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat(),
        "period_kind": _kind(fact),
        "accession": fact.accession,
        "source_url": fact.source_url,
        "available_at": _utc(fact.available_at).isoformat(),
        "observed_at": _utc(fact.observed_at).isoformat(),
    }


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "value": None, "reason": reason}


def _ttm(facts: list[ResearchFinancialFact], concept: str) -> dict[str, Any]:
    leaf = concept.rsplit(":", 1)[-1].lower().replace("_", "")
    if leaf not in _ADDITIVE:
        return _unknown("该指标未确认可跨季度相加；存量、比例和每股指标不自动计算 TTM")
    quarters = sorted(
        (item for item in facts if _kind(item) == "quarter"),
        key=lambda item: item.period_end,
        reverse=True,
    )
    current_end = max(item.period_end for item in facts)
    for last in quarters:
        if last.period_end != current_end:
            continue
        chain = [last]
        while len(chain) < 4:
            start = chain[-1].period_start
            assert start is not None
            previous = next(
                (item for item in quarters if item.period_end == start - timedelta(days=1)), None
            )
            if previous is None:
                break
            chain.append(previous)
        if len(chain) == 4:
            first_start = chain[-1].period_start
            assert first_start is not None
            days = (last.period_end - first_start).days + 1
            if 330 <= days <= 380:
                return {
                    "status": "available",
                    "value": sum(item.value for item in chain),
                    "method": "four_contiguous_quarters",
                    "period_start": first_start.isoformat(),
                    "period_end": last.period_end.isoformat(),
                    "components": [_fact(item) for item in reversed(chain)],
                }
    annuals = [item for item in facts if _kind(item) == "annual"]
    ytds = sorted(
        (item for item in facts if _kind(item) in {"quarter", "ytd"}),
        key=lambda item: item.period_end,
        reverse=True,
    )
    for current in ytds:
        if current.period_end != current_end:
            continue
        assert current.period_start is not None
        for annual in annuals:
            if annual.period_end != current.period_start - timedelta(days=1):
                continue
            for prior in ytds:
                if prior.period_start != annual.period_start or prior.period_start is None:
                    continue
                if prior.period_end >= annual.period_end:
                    continue
                current_days = (current.period_end - current.period_start).days
                prior_days = (prior.period_end - prior.period_start).days
                if abs(current_days - prior_days) > 7:
                    continue
                start = prior.period_end + timedelta(days=1)
                if not 330 <= (current.period_end - start).days + 1 <= 380:
                    continue
                return {
                    "status": "available",
                    "value": annual.value + current.value - prior.value,
                    "method": "annual_plus_current_ytd_minus_prior_ytd",
                    "period_start": start.isoformat(),
                    "period_end": current.period_end.isoformat(),
                    "components": [_fact(annual), _fact(current), _fact(prior)],
                }
    return _unknown("缺少四个连续且不重叠的季度，或可比全年及同期累计期间")


def _financial_metrics(facts: list[ResearchFinancialFact]) -> list[dict[str, Any]]:
    versions: dict[tuple[Any, ...], ResearchFinancialFact] = {}
    for fact in sorted(
        facts,
        key=lambda item: (_utc(item.available_at), _utc(item.observed_at), item.id),
        reverse=True,
    ):
        if not math.isfinite(fact.value) or (
            fact.period_start and fact.period_start > fact.period_end
        ):
            continue
        key = (fact.source, fact.concept, fact.unit, fact.period_start, fact.period_end)
        versions.setdefault(key, fact)
    groups: dict[tuple[str, str, str], list[ResearchFinancialFact]] = defaultdict(list)
    for fact in versions.values():
        groups[(fact.source, fact.concept, fact.unit)].append(fact)
    metrics = []
    for (source, concept, unit), group in sorted(groups.items()):
        group.sort(
            key=lambda item: (item.period_end, _utc(item.available_at), item.id), reverse=True
        )
        quarters = [item for item in group if _kind(item) == "quarter"]
        quarter = (
            {"status": "available", **_fact(quarters[0])}
            if quarters
            else _unknown("没有合法起止日期的单季度事实；未使用 FY/FP 标签推断")
        )
        metrics.append(
            {
                "source": source,
                "concept": concept,
                "unit": unit,
                "latest": _fact(group[0]),
                "quarter": quarter,
                "ttm": _ttm(group, concept),
                "periods_count": len(group),
            }
        )
    return metrics


def _period(details: dict[str, Any]) -> str:
    explicit = details.get("financial_period")
    if isinstance(explicit, str) and explicit:
        return explicit
    year, quarter = details.get("year"), details.get("quarter")
    if isinstance(year, int) and isinstance(quarter, int) and 1 <= quarter <= 4:
        return f"{year}Q{quarter}"
    return ""


def _boundary(event: ResearchCalendarRevision, metric: str | None = None) -> datetime:
    actual_published = event.details.get(f"{metric}_published_at") if metric else None
    actual_published = actual_published or event.details.get("actual_published_at")
    if isinstance(actual_published, str):
        try:
            parsed = datetime.fromisoformat(actual_published.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                return parsed.astimezone(UTC)
        except ValueError:
            pass
    if event.scheduled_at is not None:
        return _utc(event.scheduled_at)
    return datetime.combine(event.scheduled_date, time.min, ZoneInfo(event.timezone)).astimezone(
        UTC
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _known_unit(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = " ".join(value.casefold().split())
    tokens = set(re.findall(r"[^\W_]+", normalized))
    placeholders = {
        "unknown", "unspecified", "unverified", "undefined", "missing", "none", "null",
        "na", "tbd", "未知", "不详", "未确认", "未核实", "未提供", "待确认",
    }
    return bool(tokens) and not tokens.intersection(placeholders) and not any(
        marker in normalized.replace("_", " ")
        for marker in ("n/a", "n.a.", "not available", "not reported", "not specified", "not known")
    )


def _surprises(
    calendars: list[ResearchCalendarRevision],
    expectations: list[ResearchExpectation],
) -> list[dict[str, Any]]:
    by_id = {event.id: event for event in calendars}
    groups: dict[str, list[ResearchCalendarRevision]] = defaultdict(list)
    for event in calendars:
        groups[event.event_key].append(event)
    result = []
    for event_key, history in groups.items():
        event = max(history, key=lambda item: item.revision)
        if event.event_type != "earnings" or event.status == "cancelled":
            continue
        period = _period(event.details)
        for metric, actual_key, unit_key in (
            ("eps", "epsActual", "eps_unit"),
            ("revenue", "revenueActual", "revenue_unit"),
        ):
            actual = _number(event.details.get(actual_key))
            if actual is None:
                continue
            unit = event.details.get(unit_key)
            item: dict[str, Any] = {
                "event_key": event_key,
                "calendar_revision_id": event.id,
                "metric": metric,
                "financial_period": period or None,
                "unit": unit,
                "actual": actual,
                "expected": None,
                "difference": None,
                "surprise_pct": None,
                "status": "unknown",
                "reason": "没有可比且在发布前存档的预期",
                "source_url": event.details.get(f"{metric}_source_url") or event.source_url,
                "actual_published_at": event.details.get(f"{metric}_published_at")
                or event.details.get("actual_published_at"),
            }
            if not period or not _known_unit(unit):
                item["reason"] = "实际值缺少明确财务期间或报告单位，不能推定为证券报价币种"
                result.append(item)
                continue
            actual_records = [
                record for record in history if _number(record.details.get(actual_key)) is not None
            ]
            first_actual_known = min(
                max(_utc(record.available_at), _utc(record.observed_at))
                for record in actual_records
            )
            first_actual_boundary = min(_boundary(record, metric) for record in actual_records)
            eligible = []
            for expected in expectations:
                origin = by_id.get(expected.calendar_revision_id)
                if (
                    expected.event_key != event_key
                    or expected.metric.lower() != metric
                    or expected.financial_period != period
                    or not _known_unit(expected.unit)
                    or expected.unit != unit
                    or not expected.is_pre_release
                    or origin is None
                    or not math.isfinite(expected.value)
                    or (_period(origin.details) and _period(origin.details) != period)
                ):
                    continue
                known_at = max(_utc(expected.observed_at), _utc(expected.available_at))
                if known_at < max(_utc(origin.observed_at), _utc(origin.available_at)):
                    continue
                boundary = min(
                    _boundary(event, metric), _boundary(origin, metric), first_actual_known,
                    first_actual_boundary,
                )
                if known_at >= boundary:
                    continue
                # A revised calendar must not make post-release data look pre-release.
                if any(
                    _number(origin.details.get(key)) is not None
                    for key in ("epsActual", "revenueActual")
                ):
                    continue
                eligible.append(expected)
            if eligible:
                expected = max(
                    eligible,
                    key=lambda row: (_utc(row.observed_at), _utc(row.available_at), row.id),
                )
                delta = actual - expected.value
                item.update(
                    {
                        "status": "available",
                        "expected": expected.value,
                        "difference": delta,
                        "surprise_pct": delta / abs(expected.value) * 100
                        if expected.value != 0
                        else None,
                        "reason": "预期为零，仅比较绝对差值"
                        if expected.value == 0
                        else "同口径发布前预期",
                        "expectation_id": expected.id,
                        "estimate_kind": expected.estimate_kind,
                        "expectation_observed_at": _utc(expected.observed_at).isoformat(),
                        "expectation_source_url": expected.source_url,
                    }
                )
            result.append(item)
    return result


def get_financial_research(
    session: Session,
    security_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券不存在")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("查询时间必须包含时区")
    current = current.astimezone(UTC)
    facts = list(
        session.scalars(
            select(ResearchFinancialFact).where(
                ResearchFinancialFact.security_id == security_id,
                ResearchFinancialFact.observed_at <= current,
                ResearchFinancialFact.available_at <= current,
                ResearchFinancialFact.period_end <= current.date(),
            )
        ).all()
    )
    calendars = list(
        session.scalars(
            select(ResearchCalendarRevision).where(
                ResearchCalendarRevision.security_id == security_id,
                ResearchCalendarRevision.observed_at <= current,
                ResearchCalendarRevision.available_at <= current,
            )
        ).all()
    )
    expectations = list(
        session.scalars(
            select(ResearchExpectation).where(
                ResearchExpectation.security_id == security_id,
                ResearchExpectation.observed_at <= current,
                ResearchExpectation.available_at <= current,
            )
        ).all()
    )
    metrics = _financial_metrics(facts)
    surprises = _surprises(calendars, expectations)
    gaps = []
    if not metrics:
        gaps.append("暂无当时可见的财务事实")
    if not any(item["ttm"]["status"] == "available" for item in metrics):
        gaps.append("缺少满足期间连续性与指标可加性的 TTM 输入")
    if not any(item["status"] == "available" for item in surprises):
        gaps.append("暂无同期间、同单位且发布前存档的可比预期")
    return {
        "security_id": security_id,
        "as_of": current.isoformat(),
        "metrics": metrics,
        "surprises": surprises,
        "gaps": gaps,
    }
