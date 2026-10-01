"""Persist review work without promoting evidence or issuing trade instructions."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import Article, Event, EventSecurityImpact, EventSecurityImpactTheme, Security, XPost
from ..workflow_models import (
    ActionTask,
    ActionTaskHistory,
    XClaim,
    XClaimEvidence,
    XClaimHistory,
    XClaimSecurity,
)
from .scoring import event_is_available
from .social_context import _identity

TASK_LABELS = {
    "pending": "待核实", "done": "已完成核验", "dismissed": "已搁置", "expired": "已过期"
}
CLAIM_LABELS = {
    "pending": "待核实", "partially_supported": "部分有依据", "verified": "已人工核实",
    "refuted": "已被反证", "expired": "已过期",
}


@dataclass
class ActionPlanData:
    tasks: dict[tuple[int, int], list[ActionTask]]
    impacts: dict[tuple[int, int], list[EventSecurityImpact]]
    events: dict[int, Event]
    posts: dict[int, XPost]


def load_action_plan_data(
    session: Session, security_ids: set[int], impacts: Sequence[EventSecurityImpact],
) -> ActionPlanData:
    tasks: dict[tuple[int, int], list[ActionTask]] = defaultdict(list)
    post_ids: set[int] = set()
    for task in session.scalars(select(ActionTask).where(
        ActionTask.security_id.in_(security_ids), ActionTask.horizon.in_((1, 5, 20)),
    ).options(joinedload(ActionTask.history))).unique():
        tasks[task.security_id, task.horizon].append(task)
        source_id = task.payload.get("source_id")
        if task.payload.get("source_kind") == "post" and isinstance(source_id, int):
            post_ids.add(source_id)
    grouped: dict[tuple[int, int], list[EventSecurityImpact]] = defaultdict(list)
    events = {}
    for impact in impacts:
        grouped[impact.security_id, impact.event_id].append(impact)
        events[impact.event_id] = impact.event
    for group in grouped.values():
        group.sort(key=lambda item: item.id)
    posts = {post.id: post for post in session.scalars(
        select(XPost).where(XPost.id.in_(post_ids))
    )} if post_ids else {}
    return ActionPlanData(dict(tasks), dict(grouped), events, posts)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _now(value: datetime | None) -> datetime:
    return _utc(value or datetime.now(UTC))


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _strings(value: Any) -> list[str]:
    return list(dict.fromkeys(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )) if isinstance(value, list) else []


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _business_deadline(start: datetime, sessions: int, market: str = "US") -> datetime:
    """Review at the close of the Nth exchange session after the local start date."""
    import pandas as pd

    from .market_research import calendar_for_market

    calendar = calendar_for_market(market)
    next_day = _utc(start).astimezone(calendar.tz).date() + timedelta(days=1)
    first = calendar.date_to_session(pd.Timestamp(next_day), direction="next")
    last = calendar.sessions_window(first, sessions)[-1]
    return _utc(calendar.session_close(last).to_pydatetime())


class RevisionConflict(ValueError):
    def __init__(self, latest: dict[str, Any]) -> None:
        super().__init__("此记录已在另一页面更新，草稿已保留；请对照最新记录后重新提交。")
        self.latest = latest


def _guard_revision(
    session: Session, model: type[ActionTask] | type[XClaim], record_id: int,
    expected_revision: int | None,
) -> None:
    if expected_revision is None:
        return  # Internal callers may operate on a freshly loaded transaction.
    matched = session.scalar(update(model).where(
        model.id == record_id, model.revision == expected_revision,
    ).values(revision=model.revision).returning(model.id).execution_options(
        synchronize_session=False,
    ))
    if matched is None:
        latest = session.get(model, record_id, populate_existing=True)
        if latest is None:
            raise LookupError("核验记录不存在")
        if isinstance(latest, ActionTask):
            raise RevisionConflict(_task_dict(latest))
        assert isinstance(latest, XClaim)
        raise RevisionConflict(_claim_dict(latest))
    session.expire_all()


def _deadline(value: datetime | None, current: datetime, fallback: datetime) -> datetime:
    deadline = _utc(value) if value is not None else _utc(fallback)
    if deadline <= current:
        raise ValueError("复核期限须晚于当前时间；过期项目重新打开时请设置新的期限")
    return deadline


def _url(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("证据链接须为完整的 HTTP 或 HTTPS 地址")
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError
        port = parsed.port
    except ValueError:
        raise ValueError("证据链接须为不含认证信息的 HTTP 或 HTTPS 地址") from None
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    tracking_keys = {"fbclid", "gclid"}
    if hostname in {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}:
        tracking_keys.update({"s", "t", "ref_src"})
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in tracking_keys
    ))
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/") or "/", query, ""))


def _history(rows: Sequence[Any]) -> list[dict[str, Any]]:
    return [{
        "id": row.id, "from_status": row.from_status, "to_status": row.to_status,
        "reason": row.reason, "note": row.note, "created_at": _iso(row.created_at),
        "snapshot": row.snapshot,
    } for row in rows]


def _task_snapshot(task: ActionTask) -> dict[str, Any]:
    return {
        "status": task.status, "note": task.note, "review_due_at": _iso(task.review_due_at),
        "source_version": task.source_version, "payload": task.payload,
        "expiration_reason": task.expiration_reason,
    }


def _task_audit(
    task: ActionTask, previous: str | None, reason: str, now: datetime, note: str = ""
) -> None:
    task.updated_at = now
    task.revision = (task.revision or 1) + (1 if task.id is not None else 0)
    task.history.append(ActionTaskHistory(
        from_status=previous, to_status=task.status, reason=reason,
        note=note or task.note, snapshot=_task_snapshot(task), created_at=now,
    ))


def _task_dict(task: ActionTask) -> dict[str, Any]:
    return {
        **task.payload, "id": task.id, "task_key": task.task_key,
        "security_id": task.security_id, "horizon": task.horizon,
        "status": task.status, "status_label": TASK_LABELS[task.status], "note": task.note,
        "review_due_at": _iso(task.review_due_at), "completed_at": _iso(task.completed_at),
        "expiration_reason": task.expiration_reason, "updated_at": _iso(task.updated_at),
        "history": _history(task.history), "completion_does_not_trigger_trade": True,
        "persisted": True, "needs_refresh": False, "revision": task.revision,
    }


def _task_source(
    session: Session, security_id: int, payload: Mapping[str, Any], now: datetime,
    *, preloaded: ActionPlanData | None = None,
) -> tuple[str, bool]:
    source_kind, source_id = payload.get("source_kind"), payload.get("source_id")
    if source_kind == "event" and isinstance(source_id, int):
        event = (preloaded.events.get(source_id) if preloaded is not None
                 else session.get(Event, source_id))
        impacts = (preloaded.impacts.get((security_id, source_id), [])
                   if preloaded is not None else session.scalars(select(EventSecurityImpact).where(
            EventSecurityImpact.event_id == source_id,
            EventSecurityImpact.security_id == security_id,
            EventSecurityImpact.is_current.is_(True),
            EventSecurityImpact.status == "complete",
        ).order_by(EventSecurityImpact.id)).all())
        if (event is None or event.status not in {"complete", "partial"}
                or not event_is_available(event, now)):
            return "", False
        if not impacts:
            return "", False
        return _digest({
            "event": [event.title, event.summary, event.status, _iso(event.occurred_at),
                      _iso(event.updated_at), event.missing_proof, event.demand_status,
                      _iso(getattr(event, "first_disclosed_at", None)),
                      getattr(event, "fact_time_verified", None),
                      getattr(event, "financial_period", None),
                      getattr(event, "fact_source_url", None)],
            "impacts": [[item.id, item.impacts, item.thesis, item.catalysts, item.falsifiers]
                        for item in impacts],
        }), True
    if source_kind == "post" and isinstance(source_id, int):
        post = (preloaded.posts.get(source_id) if preloaded is not None
                else session.get(XPost, source_id))
        if post is None or post.screening_status in {"ignore", "ignored"}:
            return "", False
        if _utc(post.published_at) > now:
            return "", False
        return _digest([
            post.text, post.quoted_text, post.screening, (post.raw_data or {}).get("is_truncated")
        ]), True
    return _digest({key: payload.get(key) for key in (
        "detail", "on_pass", "on_fail", "when", "reference"
    )}), True


def sync_action_tasks(
    session: Session, security_id: int, horizon: int, plan: Mapping[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Flush a checklist revision; the caller owns commit/rollback and scheduling."""
    if horizon not in {1, 5, 20}:
        raise ValueError("行动评估周期只能为 1、5 或 20 个交易日")
    security = session.get(Security, security_id)
    if security is None:
        raise LookupError("证券不存在")
    current = _now(now)
    stored = session.scalars(select(ActionTask).where(
        ActionTask.security_id == security_id, ActionTask.horizon == horizon,
    ).options(selectinload(ActionTask.history))).all()
    by_key = {task.task_key: task for task in stored}
    active_keys: set[str] = set()
    result = []
    for raw in plan.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        payload = {key: value for key, value in raw.items() if key not in {
            "id", "task_key", "history", "status", "status_label", "note", "review_due_at",
        }}
        detail = re.sub(r"\s+", " ", str(payload.get("detail") or "")).strip()
        key = _digest([security_id, horizon, payload.get("kind"), payload.get("source_kind"),
                       payload.get("source_id"), detail])
        if key in active_keys:
            continue
        version, available = _task_source(session, security_id, payload, current)
        if not available:
            continue
        active_keys.add(key)
        task = by_key.get(key)
        if task is None:
            task = ActionTask(
                task_key=key, security_id=security_id, horizon=horizon,
                kind=str(payload.get("kind") or "research"),
                source_kind=str(payload.get("source_kind") or ""),
                source_id=payload.get("source_id"), source_version=version,
                payload=payload, status="pending", note="", expiration_reason="",
                review_due_at=_business_deadline(current, horizon, security.market),
                created_at=current, updated_at=current, last_seen_at=current,
            )
            session.add(task)
            _task_audit(task, None, "created", current)
        else:
            previous = task.status
            changed = task.source_version != version
            restored = task.status == "expired" and task.expiration_reason == "not_in_plan"
            task.payload = payload
            task.last_seen_at = current
            if changed or restored:
                task.status, task.note, task.expiration_reason = "pending", "", ""
                task.completed_at = None
                task.source_version = version
                if not task.review_due_manual:
                    task.review_due_at = _business_deadline(current, horizon, security.market)
                _task_audit(task, previous, "source_changed" if changed else "restored", current)
            elif task.status != "expired" and _utc(task.review_due_at) <= current:
                task.status, task.expiration_reason = "expired", "deadline"
                _task_audit(task, previous, "deadline", current)
        result.append(task)
    for task in stored:
        if task.task_key not in active_keys and task.status != "expired":
            previous = task.status
            task.status, task.expiration_reason = "expired", "not_in_plan"
            _task_audit(task, previous, "not_in_plan", current)
    session.flush()
    return {**plan, "horizon": horizon, "tasks": [_task_dict(task) for task in result],
            "completion_does_not_trigger_trade": True}


def list_tasks(
    session: Session, security_id: int | None = None, horizon: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    query = select(ActionTask).options(selectinload(ActionTask.history))
    if security_id is not None:
        query = query.where(ActionTask.security_id == security_id)
    if horizon is not None:
        query = query.where(ActionTask.horizon == horizon)
    tasks = session.scalars(
        query.order_by(ActionTask.updated_at.desc(), ActionTask.id.desc())
    ).all()
    current = _now(now)
    return [_task_read_state(task, current) for task in tasks]


def _task_read_state(task: ActionTask, now: datetime) -> dict[str, Any]:
    result = _task_dict(task)
    if _utc(task.review_due_at) <= now and task.status != "expired":
        result.update(status="expired", status_label=TASK_LABELS["expired"],
                      expiration_reason="deadline", needs_refresh=True)
    return result


def read_action_plan(
    session: Session, security_id: int, horizon: int, plan: Mapping[str, Any],
    now: datetime | None = None,
    *, preloaded: ActionPlanData | None = None,
) -> dict[str, Any]:
    """Overlay persisted state without creating tasks or writing expiry on a GET."""
    if horizon not in {1, 5, 20}:
        raise ValueError("行动评估周期只能为 1、5 或 20 个交易日")
    current = _now(now)
    stored = (preloaded.tasks.get((security_id, horizon), []) if preloaded is not None
              else session.scalars(select(ActionTask).where(
        ActionTask.security_id == security_id, ActionTask.horizon == horizon,
    ).options(selectinload(ActionTask.history))).all())
    by_key = {task.task_key: task for task in stored}
    tasks = []
    seen: set[str] = set()
    for raw in plan.get("tasks") or []:
        if not isinstance(raw, Mapping):
            continue
        detail = re.sub(r"\s+", " ", str(raw.get("detail") or "")).strip()
        key = _digest([security_id, horizon, raw.get("kind"), raw.get("source_kind"),
                       raw.get("source_id"), detail])
        if key in seen:
            continue
        seen.add(key)
        task = by_key.get(key)
        if task is None:
            tasks.append({**raw, "id": None, "task_key": key, "persisted": False,
                          "needs_refresh": True, "status": "pending", "status_label": "待保存",
                          "note": "", "review_due_at": None, "history": [],
                          "completion_does_not_trigger_trade": True})
            continue
        item = {**raw, **_task_read_state(task, current)}
        version, available = _task_source(session, security_id, raw, current, preloaded=preloaded)
        if not available:
            item.update(status="expired", status_label=TASK_LABELS["expired"],
                        expiration_reason="source_unavailable", needs_refresh=True)
        elif version != task.source_version:
            item.update(status="pending", status_label="来源变化，待重新核验",
                        note="", needs_refresh=True)
        tasks.append(item)
    return {**plan, "horizon": horizon, "tasks": tasks,
            "needs_refresh": any(item.get("needs_refresh") for item in tasks),
            "completion_does_not_trigger_trade": True}


def update_task(
    session: Session, task_id: int, *, status: str, note: str,
    review_due_at: datetime | None = None, now: datetime | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    if status not in TASK_LABELS:
        raise ValueError("未知的行动任务状态")
    if not note.strip():
        raise ValueError("请填写完成、搁置或重新核验的具体备注")
    _guard_revision(session, ActionTask, task_id, expected_revision)
    task = session.get(ActionTask, task_id)
    if task is None:
        raise LookupError("行动任务不存在")
    current = _now(now)
    deadline = _deadline(review_due_at, current, task.review_due_at) if status != "expired" else (
        _utc(review_due_at) if review_due_at else task.review_due_at
    )
    version, available = _task_source(session, task.security_id, task.payload, current)
    if not available and status != "expired":
        raise ValueError("原始来源已不可用，请重新生成行动计划后核验")
    if available and version != task.source_version and status != "expired":
        raise ValueError("原始来源已有变化，请先刷新行动计划再核验")
    if task.status != "expired" and _utc(task.review_due_at) <= current:
        previous = task.status
        task.status, task.expiration_reason = "expired", "deadline"
        _task_audit(task, previous, "deadline", current)
    previous = task.status
    task.status, task.note = status, note.strip()
    task.review_due_at = deadline
    if review_due_at is not None:
        task.review_due_manual = True
    task.completed_at = current if status == "done" else None
    task.expiration_reason = "manual" if status == "expired" else ""
    _task_audit(task, previous, "manual_review", current)
    session.flush()
    return _task_dict(task)


def _claim_snapshot(claim: XClaim) -> dict[str, Any]:
    return {
        "status": claim.status, "note": claim.note, "claim_text": claim.claim_text,
        "source_version": claim.source_version, "source_snapshot": claim.source_snapshot,
        "review_due_at": _iso(claim.review_due_at), "evidence": _evidence_dicts(claim),
        "expiration_reason": claim.expiration_reason,
    }


def _claim_audit(
    claim: XClaim, previous: str | None, reason: str, now: datetime, note: str = ""
) -> None:
    claim.updated_at = now
    claim.revision = (claim.revision or 1) + (1 if claim.id is not None else 0)
    claim.history.append(XClaimHistory(
        from_status=previous, to_status=claim.status, reason=reason, note=note or claim.note,
        snapshot=_claim_snapshot(claim), created_at=now,
    ))


def _evidence_dicts(claim: XClaim) -> list[dict[str, Any]]:
    return [{
        "url": item.url, "source_url": item.source_url, "stance": item.stance,
        "is_official": item.is_official, "note": item.note, "source_key": item.source_key,
    } for item in claim.evidence]


def _claim_dict(claim: XClaim, *, include_history: bool = True) -> dict[str, Any]:
    evidence = _evidence_dicts(claim)
    return {
        "id": claim.id, "revision": claim.revision, "claim_key": claim.claim_key,
        "post_id": claim.post_id,
        "author": claim.author, "post_url": claim.post_url,
        "published_at": _iso(claim.published_at), "claim_text": claim.claim_text,
        "claim_kind": claim.claim_kind, "verification_needs": claim.verification_needs,
        "source_truncated": claim.source_truncated, "source_snapshot": claim.source_snapshot,
        "security_ids": [link.security_id for link in claim.securities],
        "securities": [{"security_id": link.security_id, "match_basis": link.match_basis}
                       for link in claim.securities],
        "status": claim.status, "status_label": CLAIM_LABELS[claim.status], "note": claim.note,
        "review_due_at": _iso(claim.review_due_at), "reviewed_at": _iso(claim.reviewed_at),
        "expiration_reason": claim.expiration_reason, "evidence": evidence,
        "independent_evidence_count": len({item["source_key"] for item in evidence}),
        "official_support_count": len({item["source_key"] for item in evidence
                                       if item["is_official"] and item["stance"] == "support"}),
        "official_conflict_count": len({item["source_key"] for item in evidence
                                        if item["is_official"] and item["stance"] == "conflict"}),
        "history": _history(claim.history) if include_history else [],
        "history_deferred": not include_history, "formal_score_impact": 0,
        "verification_does_not_promote_post": True,
        "review_calendar_note": (
            "自动期限按关联证券交易所收盘计算，多市场取较早期限；人工期限优先。"
            if claim.securities else "未关联证券：自动期限按自然日计算；人工期限优先。"
        ),
    }


def _association_maps(
    session: Session, now: datetime,
) -> tuple[dict[str, set[int]], dict[int, set[int]], dict[str, set[int]]]:
    identities: dict[str, set[int]] = defaultdict(set)
    for security in session.scalars(select(Security)):
        values = [security.name, security.symbol, *(security.aliases or [])]
        for qualifier in (security.market, security.exchange):
            values.extend([f"{qualifier}:{security.symbol}", f"{security.symbol}.{qualifier}"])
        for value in values:
            if isinstance(value, str) and _identity(value):
                identities[_identity(value)].add(security.id)
    events: dict[int, set[int]] = defaultdict(set)
    themes: dict[str, set[int]] = defaultdict(set)
    impacts = session.scalars(select(EventSecurityImpact).where(
        EventSecurityImpact.is_current.is_(True), EventSecurityImpact.status == "complete",
    ).options(
        selectinload(EventSecurityImpact.event),
        selectinload(EventSecurityImpact.theme_links).selectinload(EventSecurityImpactTheme.theme),
    ))
    for impact in impacts:
        if (impact.event.status not in {"complete", "partial"}
                or not event_is_available(impact.event, now)):
            continue
        events[impact.event_id].add(impact.security_id)
        for link in impact.theme_links:
            for value in (link.theme.name, link.theme.slug):
                themes[_identity(value)].add(impact.security_id)
    return identities, events, themes


def _claim_associations(
    post: XPost, screening: Mapping[str, Any],
    maps: tuple[dict[str, set[int]], dict[int, set[int]], dict[str, set[int]]],
) -> dict[int, str]:
    identities, events, themes = maps
    matches = {
        security_id: "event" for security_id in events.get(post.related_event_id or 0, set())
    }
    for entity in _strings(screening.get("entities")):
        found = identities.get(_identity(entity), set())
        if len(found) == 1:
            for security_id in found:
                matches.setdefault(security_id, "entity")
    for theme in _strings(screening.get("themes")):
        for security_id in themes.get(_identity(theme), set()):
            matches.setdefault(security_id, "theme")
    return matches


def _expire_claim(claim: XClaim, now: datetime, reason: str) -> bool:
    if claim.status == "expired":
        return False
    previous = claim.status
    claim.status, claim.expiration_reason = "expired", reason
    _claim_audit(claim, previous, reason, now)
    return True


def _post_snapshot(post: XPost) -> dict[str, Any]:
    snapshot = {
        "text": post.text, "quoted_text": post.quoted_text, "url": post.url,
        "post_id": post.post_id, "author": post.account.handle,
        "published_at": _iso(post.published_at),
        "is_truncated": bool((post.raw_data or {}).get("is_truncated")),
        "external_links": post.external_links,
        "screening": post.screening if isinstance(post.screening, dict) else {},
    }
    result: dict[str, Any] = json.loads(json.dumps(snapshot, ensure_ascii=False))
    return result


def _post_available(post: XPost | None, now: datetime) -> bool:
    if post is None:
        return False
    screening = post.screening if isinstance(post.screening, dict) else {}
    return (
        post.screening_status not in {"ignore", "ignored"}
        and post.post_type in {"original", "quote"}
        and screening.get("classification") not in {"irrelevant", "promotion"}
        and _utc(post.published_at) <= now
    )


def sync_x_claims(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Extract review subjects from saved posts; no network, promotion, or fact inference."""
    current = _now(now)
    maps = _association_maps(session, current)
    stored = session.scalars(select(XClaim).options(
        selectinload(XClaim.history), selectinload(XClaim.evidence), selectinload(XClaim.securities)
    )).all()
    by_key = {claim.claim_key: claim for claim in stored}
    active_keys: set[str] = set()
    created = updated = expired = 0
    posts = session.scalars(select(XPost).where(
        XPost.published_at <= current,
        XPost.screening_status.not_in(["ignore", "ignored"]),
        XPost.post_type.in_(["original", "quote"]),
    ).options(selectinload(XPost.account)))
    for post in posts:
        screening = post.screening if isinstance(post.screening, dict) else {}
        if screening.get("classification") in {"irrelevant", "promotion"}:
            continue
        texts = _strings(screening.get("factual_claims"))
        kind = "fact_claim"
        if not texts and isinstance(screening.get("claim_summary"), str):
            texts, kind = _strings([screening["claim_summary"]]), "summary_to_verify"
        snapshot = _post_snapshot(post)
        version = _digest(snapshot)
        needs = _strings(screening.get("verification_needs"))
        matches = _claim_associations(post, screening, maps)
        sessions = {"intraday": 1, "1d": 1, "1w": 5, "1m": 20, "long_term": 60}.get(
            str(screening.get("horizon")), 5
        )
        markets = set(session.scalars(select(Security.market).where(Security.id.in_(matches))))
        default_due = min(
            _business_deadline(post.published_at, sessions, market) for market in markets
        ) if markets else _utc(post.published_at) + timedelta(days=sessions)
        for text in texts:
            key = _digest([post.id, re.sub(r"\s+", " ", text).casefold()])
            if key in active_keys:
                continue
            active_keys.add(key)
            claim = by_key.get(key)
            if claim is None:
                claim = XClaim(
                    claim_key=key, post_id=post.id, author=post.account.handle, post_url=post.url,
                    published_at=post.published_at, claim_text=text, claim_kind=kind,
                    verification_needs=needs, source_truncated=snapshot["is_truncated"],
                    source_version=version, source_snapshot=snapshot, status="pending", note="",
                    expiration_reason="", review_due_at=default_due,
                    created_at=current, updated_at=current,
                )
                session.add(claim)
                _claim_audit(claim, None, "created", current)
                created += 1
            elif claim.source_version != version:
                previous = claim.status
                claim.source_version, claim.source_snapshot = version, snapshot
                claim.source_truncated = snapshot["is_truncated"]
                claim.verification_needs = needs
                claim.status, claim.note, claim.expiration_reason = "pending", "", ""
                claim.reviewed_at = None
                _claim_audit(claim, previous, "source_changed", current)
                updated += 1
            elif claim.status == "expired" and claim.expiration_reason == "source_unavailable":
                if _utc(claim.review_due_at) > current:
                    claim.status, claim.expiration_reason, claim.note = "pending", "", ""
                    claim.reviewed_at = None
                    _claim_audit(claim, "expired", "restored", current)
                    updated += 1
            existing_links = {link.security_id: link for link in claim.securities}
            for security_id, basis in matches.items():
                if security_id in existing_links:
                    existing_links[security_id].match_basis = basis
                else:
                    claim.securities.append(XClaimSecurity(
                        security_id=security_id, match_basis=basis
                    ))
            for link in list(claim.securities):
                if link.security_id not in matches:
                    claim.securities.remove(link)
            if _utc(claim.review_due_at) <= current:
                expired += int(_expire_claim(claim, current, "deadline"))
    for claim in stored:
        if claim.claim_key not in active_keys:
            expired += int(_expire_claim(claim, current, "source_unavailable"))
        elif _utc(claim.review_due_at) <= current:
            expired += int(_expire_claim(claim, current, "deadline"))
    session.flush()
    return {
        "created": created, "updated": updated, "expired": expired, "total": len(stored) + created
    }


def _suggested_sources(claim: XClaim, articles: Sequence[Article]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw in _strings(claim.source_snapshot.get("external_links")):
        try:
            url = _url(raw)
        except ValueError:
            continue
        result[url] = {"url": url, "title": "原帖引用链接", "reason": "作者引用，尚未独立核验",
                       "match_only": True, "is_official": False}
    words = {word.casefold() for word in re.findall(r"[A-Za-z0-9]{4,}|[\u4e00-\u9fff]{2,}",
                                                   claim.claim_text)}
    for article in articles:
        title = article.title.casefold()
        if not any(word in title for word in words):
            continue
        try:
            url = _url(article.canonical_url)
        except ValueError:
            continue
        result[url] = {"url": url, "article_id": article.id, "title": article.title,
                       "reason": "仅关键词匹配，须人工检查是否支持该主张",
                       "match_only": True, "is_official": True}
        if len(result) >= 8:
            break
    return list(result.values())[:8]


def list_claims(
    session: Session, security_id: int | None = None, *, now: datetime | None = None,
    claim_ids: Sequence[int] | None = None, include_history: bool = True,
) -> list[dict[str, Any]]:
    current = _now(now)
    query = select(XClaim).options(selectinload(XClaim.evidence), selectinload(XClaim.securities))
    if include_history:
        query = query.options(selectinload(XClaim.history))
    if claim_ids is not None:
        query = query.where(XClaim.id.in_(claim_ids))
    if security_id is not None:
        query = query.where(XClaim.securities.any(XClaimSecurity.security_id == security_id))
    claims = session.scalars(query.order_by(XClaim.published_at.desc(), XClaim.id.desc())).all()
    articles = session.scalars(select(Article).where(
        Article.evidence_role == "official_primary", Article.analysis_eligible.is_(True),
        Article.published_at >= current - timedelta(days=90), Article.published_at <= current,
    ).order_by(Article.published_at.desc()).limit(200)).all()
    result = []
    post_ids = {claim.post_id for claim in claims if claim.post_id is not None}
    posts = {post.id: post for post in session.scalars(
        select(XPost).where(XPost.id.in_(post_ids)).options(selectinload(XPost.account))
    )} if post_ids else {}
    for claim in claims:
        item = {**_claim_dict(claim, include_history=include_history),
                "suggested_sources": _suggested_sources(claim, articles)}
        post = posts.get(claim.post_id) if claim.post_id is not None else None
        if not _post_available(post, current):
            item.update(status="expired", status_label=CLAIM_LABELS["expired"],
                        expiration_reason="source_unavailable", needs_refresh=True)
        elif post is not None and _digest(_post_snapshot(post)) != claim.source_version:
            item.update(status="pending", status_label="来源变化，待重新核实",
                        note="", needs_refresh=True)
        if _utc(claim.review_due_at) <= current and claim.status != "expired":
            item.update(status="expired", status_label=CLAIM_LABELS["expired"],
                        expiration_reason="deadline")
        result.append(item)
    return result


def claim_page(
    session: Session, security_id: int | None = None, *, limit: int = 20,
    before_id: int | None = None, status: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Bound list and history work before materializing records."""
    if not 1 <= limit <= 100:
        raise ValueError("每页数量须为 1–100")
    if status is not None and status not in CLAIM_LABELS:
        raise ValueError("未知的主张核验状态")
    current = _now(now)
    query = select(XClaim.id)
    if security_id is not None:
        query = query.where(XClaim.securities.any(XClaimSecurity.security_id == security_id))
    if before_id is not None:
        query = query.where(XClaim.id < before_id)
    if status == "expired":
        query = query.where(or_(XClaim.status == "expired", XClaim.review_due_at <= current))
    elif status is not None:
        query = query.where(XClaim.status == status, XClaim.review_due_at > current)
    ids = list(session.scalars(query.order_by(XClaim.id.desc()).limit(limit + 1)))
    more, ids = len(ids) > limit, ids[:limit]
    items = list_claims(session, security_id, now=current, claim_ids=ids, include_history=False)
    items.sort(key=lambda item: item["id"], reverse=True)
    latest_ids = select(func.max(XClaimHistory.id)).where(
        XClaimHistory.claim_id.in_(ids),
    ).group_by(XClaimHistory.claim_id)
    latest = {row.claim_id: row for row in session.scalars(
        select(XClaimHistory).where(XClaimHistory.id.in_(latest_ids)),
    )} if ids else {}
    for item in items:
        entry = latest.get(item["id"])
        item["history"] = _history([entry]) if entry else []
    return {"items": items, "next_cursor": ids[-1] if more else None, "limit": limit,
            "security_id": security_id, "status": status}


def get_claim(session: Session, claim_id: int) -> dict[str, Any]:
    items = list_claims(session, claim_ids=[claim_id])
    if not items:
        raise LookupError("待核验主张不存在")
    return items[0]


def _validated_evidence(evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("每条证据须包含链接、支持或反证方向及具体备注")
        url = _url(item.get("url"))
        source_url = _url(item.get("source_url") or url)
        stance = item.get("stance")
        if stance not in {"support", "conflict"}:
            raise ValueError("证据方向只能为 support 或 conflict")
        note = str(item.get("note") or "").strip()
        if not note:
            raise ValueError("请说明证据支持或反驳主张的具体内容")
        official = item.get("is_official", False)
        if not isinstance(official, bool):
            raise ValueError("是否官方来源须为布尔值")
        key = _digest(source_url)
        candidate = {"url": url, "source_url": source_url, "source_key": key,
                     "stance": stance, "is_official": official, "note": note}
        previous = unique.get((key, stance))
        if previous is None or (official and not previous["is_official"]):
            unique[(key, stance)] = candidate
    return list(unique.values())


def update_claim(
    session: Session, claim_id: int, *, status: str, note: str,
    evidence: Sequence[Mapping[str, Any]] | None = None,
    review_due_at: datetime | None = None, now: datetime | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Record an explicit human judgment; supplied evidence replaces the prior set."""
    if status not in CLAIM_LABELS:
        raise ValueError("未知的主张核验状态")
    if not note.strip():
        raise ValueError("请填写主张核验结论的具体依据")
    _guard_revision(session, XClaim, claim_id, expected_revision)
    claim = session.get(XClaim, claim_id)
    if claim is None:
        raise LookupError("待核验主张不存在")
    material_before = (claim.status, _evidence_dicts(claim))
    current = _now(now)
    deadline = _deadline(review_due_at, current, claim.review_due_at) if status != "expired" else (
        _utc(review_due_at) if review_due_at else claim.review_due_at
    )
    entries = _validated_evidence(evidence if evidence is not None else _evidence_dicts(claim))
    official_support = [item for item in entries
                        if item["is_official"] and item["stance"] == "support"]
    official_conflict = [item for item in entries
                         if item["is_official"] and item["stance"] == "conflict"]
    if status == "verified" and (not official_support or official_conflict):
        raise ValueError("核实须有人工确认的官方支持材料，并先处理官方反证；关键词匹配不算核实")
    if status == "refuted" and not official_conflict:
        raise ValueError("反证结论须关联人工确认的官方反证材料")
    if status == "partially_supported" and not any(item["stance"] == "support" for item in entries):
        raise ValueError("部分有依据须至少关联一条具体支持材料")
    post = session.get(XPost, claim.post_id) if claim.post_id is not None else None
    if status != "expired" and not _post_available(post, current):
        raise ValueError("原始帖子已不可用，请重新核对来源")
    if (status != "expired" and post is not None
            and _digest(_post_snapshot(post)) != claim.source_version):
        raise ValueError("原始帖子已有变化，请先刷新主张清单再核验")
    if _utc(claim.review_due_at) <= current:
        _expire_claim(claim, current, "deadline")
    previous = claim.status
    if evidence is not None:
        old = {(item.source_key, item.stance): item for item in claim.evidence}
        wanted = {(item["source_key"], item["stance"]) for item in entries}
        for item in entries:
            existing = old.get((item["source_key"], item["stance"]))
            if existing is None:
                claim.evidence.append(XClaimEvidence(**item, created_at=current))
            else:
                for field in ("url", "source_url", "is_official", "note"):
                    setattr(existing, field, item[field])
        for stored_evidence in list(claim.evidence):
            if (stored_evidence.source_key, stored_evidence.stance) not in wanted:
                claim.evidence.remove(stored_evidence)
    claim.status, claim.note, claim.review_due_at = status, note.strip(), deadline
    claim.reviewed_at = current
    claim.expiration_reason = "manual" if status == "expired" else ""
    _claim_audit(claim, previous, "manual_review", current)
    session.flush()
    changed_events = []
    if material_before != (claim.status, _evidence_dicts(claim)):
        from .analysis_versions import invalidate_security_analysis

        changed_events = invalidate_security_analysis(
            session, [link.security_id for link in claim.securities],
            "人工主张核验已变化，请重新分析",
        )
    return {**_claim_dict(claim), "invalidated_event_ids": changed_events}
