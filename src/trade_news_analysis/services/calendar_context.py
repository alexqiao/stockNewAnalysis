"""Connect known upcoming releases to horizon-specific review tasks."""

from collections.abc import Sequence
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..research_data_models import ResearchCalendarRevision
from .market_research import calendar_for_market, utc


def add_calendar_tasks(
    session: Session,
    security_id: int,
    market: str,
    plan: dict[str, Any],
    now: datetime,
    *, records: Sequence[ResearchCalendarRevision] | None = None,
) -> dict[str, Any]:
    horizon = int(plan.get("horizon") or 5)
    try:
        calendar = calendar_for_market(market)
        day = now.astimezone(calendar.tz).date()
        start = calendar.date_to_session(pd.Timestamp(day), direction="next")
        end = calendar.sessions_window(start, horizon - 1)[-1].date()
    except (ValueError, KeyError, TypeError):
        return {**plan, "calendar_note": "交易日历覆盖不足，无法确认本次复核窗口结束日"}
    if records is None:
        records = session.scalars(
            select(ResearchCalendarRevision)
            .where(
                or_(
                    ResearchCalendarRevision.security_id == security_id,
                    ResearchCalendarRevision.security_id.is_(None),
                ),
                ResearchCalendarRevision.observed_at <= now,
                ResearchCalendarRevision.available_at <= now,
            )
            .order_by(ResearchCalendarRevision.revision.desc(), ResearchCalendarRevision.id.desc())
        ).all()
    latest: dict[str, ResearchCalendarRevision] = {}
    for row in records:
        latest.setdefault(row.event_key, row)
    relevant = []
    for row in latest.values():
        if row.status in {"cancelled", "released", "reported", "completed"}:
            continue
        if any(
            row.details.get(key) is not None
            for key in ("epsActual", "revenueActual", "actual_date")
        ):
            continue
        if day <= row.scheduled_date <= end and (
            not row.scheduled_at or utc(row.scheduled_at) >= now
        ):
            relevant.append(row)
    tasks = list(plan.get("tasks") or [])
    for row in sorted(relevant, key=lambda item: item.scheduled_date)[:6]:
        due = (
            utc(row.scheduled_at)
            if row.scheduled_at
            else datetime.combine(
                row.scheduled_date, time(23, 59), ZoneInfo(row.timezone)
            ).astimezone(UTC)
        )
        tasks.append(
            {
                "kind": "scheduled_release",
                "title": f"发布后复核：{row.title}",
                "detail": ("核对公告是否按时发布、实际指标与提前记录的预期是否同口径；"
                           "再检查对该标的的传导。"),
                "when": f"{row.scheduled_at or row.scheduled_date} · {row.timezone} · {row.status}",
                "on_pass": "以原文和可比预期更新事件判断，保留旧版本",
                "on_fail": "记录延期或数据缺口，不把预计事件当成已发生催化",
                "source_kind": "research_calendar",
                "source_id": row.id,
                "source_label": "核对日历与预期",
                "reference": row.title,
                "status": "pending",
                "status_label": "待核实",
                "priority": 12,
                "review_due_at": due.isoformat(),
            }
        )
    return {
        **plan,
        "tasks": tasks,
        "calendar_note": (f"本次观察窗口截至 {end}，按 {market} 交易所日历；"
                          "仅有日期的事件在当日复核，不推定发布分钟。"),
    }
