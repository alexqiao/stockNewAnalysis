"""Persist source-backed research inputs without rewriting their observation time."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import SessionFactory
from ..models import Security
from ..research_data_models import (
    ResearchCalendarRevision,
    ResearchDisclosure,
    ResearchExpectation,
    ResearchFinancialFact,
    ResearchSourceState,
)

OFFICIAL_HOSTS = {
    "www.sec.gov",
    "sec.gov",
    "data.sec.gov",
    "www.cninfo.com.cn",
    "static.cninfo.com.cn",
    "www.hkexnews.hk",
    "www1.hkexnews.hk",
    "www2.hkexnews.hk",
    "www.hkex.com.hk",
    "www.sse.com.cn",
    "www.szse.cn",
    "www.bse.cn",
    "www.bls.gov",
    "www.federalreserve.gov",
}


def _aware(value: datetime, field: str = "时间") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}必须包含时区")
    return value.astimezone(UTC)


def _stored(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def validate_official_url(url: str, market: str | None = None) -> str:
    del market
    try:
        parsed = urlsplit(url.strip())
        valid = (
            parsed.scheme == "https"
            and parsed.hostname in OFFICIAL_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("来源链接必须使用受支持官方披露网站的 HTTPS 地址")
    return urlunsplit(("https", str(parsed.hostname), parsed.path or "/", parsed.query, ""))


def _security(session: Session, security_id: int) -> Security:
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券不存在")
    return security


def _manual_source_url(security: Security, url: str) -> str:
    try:
        return validate_official_url(url, security.market)
    except ValueError:
        configured = (security.provider_data or {}).get("official_ir_url")
        if not isinstance(configured, str):
            raise ValueError("请使用官方披露链接，或先配置该证券的官方 IR 地址") from None
        try:
            expected, target = urlsplit(configured), urlsplit(url.strip())
            valid = (
                all(
                    parsed.scheme == "https"
                    and parsed.hostname
                    and parsed.username is None
                    and parsed.password is None
                    and parsed.port in (None, 443)
                    for parsed in (expected, target)
                )
                and expected.hostname == target.hostname
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("原文链接必须与已配置的官方 IR HTTPS 域名一致") from None
        return urlunsplit(("https", str(target.hostname), target.path or "/", target.query, ""))


class ResearchDataService:
    def __init__(self, settings: Settings, transport: Any = None):
        self.settings = settings
        self.transport = transport

    def refresh(
        self,
        session: Session,
        security_ids: list[int] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Use the caller's transaction; background refreshes must use refresh_isolated."""
        from .research_refresh import refresh_research_data

        return refresh_research_data(
            session, self.settings, security_ids=security_ids, now=now, transport=self.transport
        )

    def refresh_isolated(
        self,
        session_factory: SessionFactory,
        security_ids: list[int] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch without holding a write transaction and commit each source independently."""
        from .research_refresh import refresh_research_data

        return refresh_research_data(
            session_factory, self.settings, security_ids=security_ids,
            now=now, transport=self.transport,
        )


def record_manual_disclosure(
    session: Session,
    security_id: int,
    *,
    title: str,
    source_url: str,
    published_at: datetime | None = None,
    excerpt: str = "",
    now: datetime | None = None,
) -> ResearchDisclosure:
    security = _security(session, security_id)
    observed = _aware(now or datetime.now(UTC))
    published = _aware(published_at, "披露时间") if published_at else None
    if published and published > observed:
        raise ValueError("披露时间不能晚于当前时间")
    source_url = _manual_source_url(security, source_url)
    if not title.strip():
        raise ValueError("披露标题不能为空")
    fingerprint = _fingerprint([security_id, source_url, title.strip(), published, excerpt])
    existing = session.scalar(
        select(ResearchDisclosure).where(ResearchDisclosure.fingerprint == fingerprint)
    )
    if existing is not None:
        return existing
    row = ResearchDisclosure(
        fingerprint=fingerprint,
        security_id=security_id,
        source="manual_official",
        source_url=source_url,
        title=title.strip(),
        published_at=published,
        available_at=observed,
        observed_at=observed,
        excerpt=excerpt.strip(),
        content_status="user_excerpt" if excerpt.strip() else "link_only",
    )
    session.add(row)
    session.flush()
    return row


def upsert_calendar(
    session: Session,
    *,
    security_id: int | None,
    event_key: str,
    title: str,
    event_type: str,
    scheduled_date: date,
    scheduled_at: datetime | None,
    timezone: str,
    status: str,
    source: str,
    source_url: str,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ResearchCalendarRevision:
    if security_id is not None:
        _security(session, security_id)
    observed = _aware(now or datetime.now(UTC))
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("事件时区无效") from None
    scheduled = _aware(scheduled_at, "事件时间") if scheduled_at else None
    if scheduled and scheduled.astimezone(zone).date() != scheduled_date:
        raise ValueError("事件日期与事件时间不一致")
    if not event_key.strip() or not title.strip():
        raise ValueError("事件标识与标题不能为空")
    latest = session.scalar(
        select(ResearchCalendarRevision)
        .where(ResearchCalendarRevision.event_key == event_key)
        .order_by(ResearchCalendarRevision.revision.desc())
        .limit(1)
    )
    if latest is not None and latest.security_id != security_id:
        raise ValueError("事件标识已属于其他证券")
    values = dict(
        security_id=security_id,
        event_key=event_key,
        title=title.strip(),
        event_type=event_type,
        scheduled_date=scheduled_date,
        scheduled_at=scheduled,
        timezone=timezone,
        status=status,
        source=source,
        source_url=source_url,
        details=details or {},
        time_precision="datetime" if scheduled else "date",
    )
    content_fingerprint = _fingerprint(values)
    if latest is not None and latest.details.get("_content_fingerprint") == content_fingerprint:
        return latest
    revision = latest.revision + 1 if latest is not None else 1
    values["details"] = {**(details or {}), "_content_fingerprint": content_fingerprint}
    row = ResearchCalendarRevision(
        **values,
        revision=revision,
        fingerprint=_fingerprint([event_key, revision, values]),
        available_at=observed,
        observed_at=observed,
    )
    session.add(row)
    session.flush()
    return row


def record_manual_calendar(
    session: Session,
    security_id: int,
    *,
    event_key: str,
    title: str,
    event_type: str,
    scheduled_date: date,
    source_url: str,
    scheduled_at: datetime | None = None,
    timezone: str = "America/New_York",
    status: str = "confirmed",
    now: datetime | None = None,
) -> ResearchCalendarRevision:
    security = _security(session, security_id)
    if event_type not in {"earnings", "dividend", "other"}:
        raise ValueError("不支持的公司事件类型")
    if status not in {"confirmed", "estimated", "cancelled"}:
        raise ValueError("不支持的事件状态")
    event_key = event_key.strip()
    if not event_key:
        raise ValueError("事件标识不能为空")
    previous = session.scalar(
        select(ResearchCalendarRevision)
        .where(ResearchCalendarRevision.event_key.in_(
            [event_key, f"manual:{security_id}:{event_key}"]
        ))
        .order_by(
            (ResearchCalendarRevision.event_key == event_key).desc(),
            ResearchCalendarRevision.revision.desc(),
        )
        .limit(1)
    )
    if previous is not None and previous.security_id != security_id:
        raise ValueError("事件标识已属于其他证券或宏观日历")
    if previous is not None and previous.status == "reported" and status != "cancelled":
        raise ValueError("已公布事件不能改回未来安排；请为新期间登记独立事件")
    key = (
        previous.event_key if previous is not None else (
            event_key if event_key.startswith(f"manual:{security_id}:")
            else f"manual:{security_id}:{event_key}"
        )
    )
    return upsert_calendar(
        session,
        security_id=security_id,
        event_key=key,
        title=title,
        event_type=event_type,
        scheduled_date=scheduled_date,
        scheduled_at=scheduled_at,
        timezone=timezone,
        status=status,
        source="manual_official",
        source_url=_manual_source_url(security, source_url),
        details={
            key: value for key, value in previous.details.items()
            if key != "_content_fingerprint"
        } if previous is not None else None,
        now=now,
    )


def record_manual_expectation(
    session: Session,
    security_id: int,
    *,
    event_key: str,
    metric: str,
    value: float,
    unit: str,
    financial_period: str,
    source_url: str,
    expected_at: datetime | None = None,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> ResearchExpectation:
    security = _security(session, security_id)
    observed = _aware(now or datetime.now(UTC))
    if observed_at is not None and _aware(observed_at, "观察时间") != observed:
        raise ValueError("观察时间由系统记录，不能回填历史时间")
    expected = _aware(expected_at, "预期时间") if expected_at else observed
    if expected > observed:
        raise ValueError("预期时间不能晚于当前时间")
    if not math.isfinite(value) or not all(
        part.strip() for part in (metric, unit, financial_period)
    ):
        raise ValueError("预期需提供有限数值、指标、财务期间和单位")
    source_url = _manual_source_url(security, source_url)
    calendar = session.scalar(
        select(ResearchCalendarRevision)
        .where(
            ResearchCalendarRevision.security_id == security_id,
            ResearchCalendarRevision.event_key.in_(
                [event_key, f"manual:{security_id}:{event_key}"]
            ),
            ResearchCalendarRevision.observed_at <= observed,
            ResearchCalendarRevision.available_at <= observed,
        )
        .order_by(ResearchCalendarRevision.revision.desc())
        .limit(1)
    )
    if calendar is None:
        raise ValueError("需要先登记关联事件日历")
    release_boundary = (
        _stored(calendar.scheduled_at)
        if calendar.scheduled_at
        else datetime.combine(
            calendar.scheduled_date, time.min, ZoneInfo(calendar.timezone)
        ).astimezone(UTC)
    )
    actual_known = any(
        calendar.details.get(key) is not None
        for key in ("epsActual", "revenueActual", "eps_actual", "revenue_actual", "actual_date")
    )
    is_pre_release = (
        observed < release_boundary
        and not actual_known
        and calendar.status not in {"cancelled", "released", "reported", "completed"}
    )
    fingerprint = _fingerprint(
        [
            security_id,
            calendar.id,
            metric,
            value,
            unit,
            financial_period,
            source_url,
            expected,
        ]
    )
    existing = session.scalar(
        select(ResearchExpectation).where(ResearchExpectation.fingerprint == fingerprint)
    )
    if existing is not None:
        return existing
    row = ResearchExpectation(
        fingerprint=fingerprint,
        security_id=security_id,
        event_key=calendar.event_key,
        calendar_revision_id=calendar.id,
        metric=metric.strip(),
        value=value,
        unit=unit.strip(),
        financial_period=financial_period.strip(),
        source="manual_official",
        source_url=source_url,
        estimate_kind="user_recorded_estimate",
        expected_at=expected,
        is_pre_release=is_pre_release,
        observed_at=observed,
        available_at=observed,
    )
    session.add(row)
    session.flush()
    return row


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _stored(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items() if not key.startswith("_")}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _record_dict(row: Any) -> dict[str, Any]:
    return {column.name: _json_value(getattr(row, column.name)) for column in row.__table__.columns}


def get_research_data(
    session: Session,
    security_id: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    _security(session, security_id)
    current = _aware(now or datetime.now(UTC))
    facts = session.scalars(
        select(ResearchFinancialFact)
        .where(
            ResearchFinancialFact.security_id == security_id,
            ResearchFinancialFact.observed_at <= current,
            ResearchFinancialFact.available_at <= current,
        )
        .order_by(
            ResearchFinancialFact.period_end.desc(),
            ResearchFinancialFact.available_at.desc(),
            ResearchFinancialFact.id.desc(),
        )
    ).all()
    disclosures = session.scalars(
        select(ResearchDisclosure)
        .where(
            ResearchDisclosure.security_id == security_id,
            ResearchDisclosure.observed_at <= current,
            ResearchDisclosure.available_at <= current,
        )
        .order_by(ResearchDisclosure.observed_at.desc(), ResearchDisclosure.id.desc())
    ).all()
    calendar_history = session.scalars(
        select(ResearchCalendarRevision)
        .where(
            or_(
                ResearchCalendarRevision.security_id == security_id,
                ResearchCalendarRevision.security_id.is_(None),
            ),
            ResearchCalendarRevision.observed_at <= current,
            ResearchCalendarRevision.available_at <= current,
        )
        .order_by(ResearchCalendarRevision.revision.desc(), ResearchCalendarRevision.id.desc())
    ).all()
    latest: dict[str, ResearchCalendarRevision] = {}
    for calendar in calendar_history:
        latest.setdefault(calendar.event_key, calendar)
    calendars = sorted(latest.values(), key=lambda row: (row.scheduled_date, row.id))
    expectations = session.scalars(
        select(ResearchExpectation)
        .where(
            ResearchExpectation.security_id == security_id,
            ResearchExpectation.observed_at <= current,
            ResearchExpectation.available_at <= current,
        )
        .order_by(ResearchExpectation.observed_at.desc(), ResearchExpectation.id.desc())
    ).all()
    source_states = session.scalars(
        select(ResearchSourceState)
        .where(
            or_(
                ResearchSourceState.security_id == security_id,
                ResearchSourceState.security_id.is_(None),
            ),
        )
        .order_by(ResearchSourceState.source_key)
    ).all()
    health = []
    gaps = []
    for state in source_states:
        item = _record_dict(state)
        if state.last_attempt_at and _stored(state.last_attempt_at) > current:
            continue
        if (
            state.expires_at
            and _stored(state.expires_at) <= current
            and state.status == "available"
        ):
            item["status"] = "stale"
            item["message"] = "已到重新核对时间，等待刷新；上次成功取得的资料仍保留。"
        health.append(item)
        if item["status"] != "available":
            gaps.append(
                {"source": state.source_key, "status": item["status"], "message": item["message"]}
            )
    if not facts:
        gaps.append(
            {"source": "financial_facts", "status": "missing", "message": "暂无可用财务事实"}
        )
    if not any(row.security_id == security_id for row in calendars):
        gaps.append(
            {"source": "company_calendar", "status": "missing", "message": "公司事件日历尚未覆盖"}
        )
    return {
        "as_of": current.isoformat(),
        "security_id": security_id,
        "financial_facts": [_record_dict(row) for row in facts],
        "disclosures": [_record_dict(row) for row in disclosures],
        "calendar": [_record_dict(row) for row in calendars],
        "calendar_revisions": [_record_dict(row) for row in calendar_history],
        "expectations": [_record_dict(row) for row in expectations],
        "source_health": health,
        "coverage_gaps": gaps,
        "coverage": "已保存的来源范围；没有事件记录不等于没有事件",
    }
