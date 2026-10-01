"""Invalidate derived event analysis without scheduling additional model calls."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Article, Event, EventArticle, EventSecurityImpact, Security
from .analysis_context import build_analysis_context, material_research_hash


def expire_analysis_relationships(session: Session) -> None:
    for instance in list(session.identity_map.values()):
        if isinstance(instance, Event):
            session.expire(instance, ["impacts", "article_links"])
        elif isinstance(instance, Security):
            session.expire(instance, ["impacts"])


def invalidate_event_analysis(
    session: Session, event_ids: Iterable[int], reason: str,
) -> list[int]:
    ids = sorted(set(event_ids))
    if not ids:
        return []
    session.flush()
    eligible = set(session.scalars(
        select(EventArticle.event_id).join(Article).where(
            EventArticle.event_id.in_(ids), Article.analysis_eligible.is_(True),
        )
    ))
    changed: list[int] = []
    for active, status in ((eligible, "stale"), (set(ids) - eligible, "excluded")):
        if active:
            changed.extend(session.scalars(update(Event).where(Event.id.in_(active)).values(
                evidence_version=Event.evidence_version + 1,
                analysis_stale=True, analysis_stale_reason=reason,
                status=status, analysis_next_retry_at=None,
            ).returning(Event.id)))
    session.execute(update(EventSecurityImpact).where(
        EventSecurityImpact.event_id.in_(changed), EventSecurityImpact.is_current.is_(True),
    ).values(is_current=False))
    expire_analysis_relationships(session)
    return sorted(changed)


def invalidate_security_analysis(
    session: Session, security_ids: Iterable[int], reason: str,
) -> list[int]:
    events = session.scalars(select(EventSecurityImpact.event_id).where(
        EventSecurityImpact.security_id.in_(set(security_ids)),
    ).distinct())
    return invalidate_event_analysis(session, events, reason)


def reconcile_research_inputs(
    session: Session, security_ids: Iterable[int] | None = None,
    *, previous_hashes: dict[int, str] | None = None,
) -> list[int]:
    query = select(EventSecurityImpact).where(EventSecurityImpact.is_current.is_(True))
    if security_ids is not None:
        query = query.where(EventSecurityImpact.security_id.in_(set(security_ids)))
    impacts = session.scalars(query).all()
    hashes: dict[int, str] = {}
    changed = set()
    for impact in impacts:
        previous = (impact.research_inputs or {}).get("material_hash") or (
            previous_hashes or {}
        ).get(impact.security_id)
        if not previous:
            continue  # Legacy analysis has no comparable captured inputs.
        if impact.security_id not in hashes:
            hashes[impact.security_id] = material_research_hash(
                build_analysis_context(session, impact.security_id)
            )
        if previous != hashes[impact.security_id]:
            changed.add(impact.event_id)
    return invalidate_event_analysis(session, changed, "财务事实或核验记录已变化，请重新分析")
