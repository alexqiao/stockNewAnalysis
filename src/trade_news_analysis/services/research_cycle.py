"""Persist reviewable decisions after research refreshes and user edits."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import EventSecurityImpact, Security, Watchlist
from ..workflow_models import XClaim, XClaimSecurity


def _stored_claim_inputs(
    session: Session, security_ids: set[int],
) -> dict[int, tuple[tuple[int, str, str], ...]]:
    inputs: dict[int, list[tuple[int, str, str]]] = {identity: [] for identity in security_ids}
    if security_ids:
        rows = session.execute(select(
            XClaimSecurity.security_id, XClaim.id, XClaim.status, XClaim.source_version,
        ).join(XClaim, XClaim.id == XClaimSecurity.claim_id).where(
            XClaimSecurity.security_id.in_(security_ids), XClaim.status != "expired",
        ).order_by(XClaim.id))
        for security_id, claim_id, status, source_version in rows:
            inputs[security_id].append((claim_id, status, source_version))
    return {identity: tuple(claims) for identity, claims in inputs.items()}


def sync_claims_and_reconcile(session: Session) -> tuple[dict[str, int], list[int]]:
    from .analysis_versions import invalidate_event_analysis, reconcile_research_inputs
    from .research_workflow import sync_x_claims

    legacy = [
        (impact.event_id, impact.security_id)
        for impact in session.scalars(select(EventSecurityImpact).where(
            EventSecurityImpact.is_current.is_(True),
        ))
        if not (impact.research_inputs or {}).get("material_hash")
    ]
    legacy_securities = {security_id for _event_id, security_id in legacy}
    # Read stored states: list_claims already projects changed/expired posts into
    # their new state before sync, which would conceal transitions for legacy inputs.
    before = _stored_claim_inputs(session, legacy_securities)
    claims = sync_x_claims(session)
    invalidated = reconcile_research_inputs(session)
    after = _stored_claim_inputs(session, legacy_securities)
    changed = {identity for identity in legacy_securities if before[identity] != after[identity]}
    legacy_events = {event_id for event_id, identity in legacy if identity in changed}
    invalidated += invalidate_event_analysis(
        session, legacy_events - set(invalidated), "X 主张依据已变化，请重新分析",
    )
    return claims, sorted(invalidated)


def capture_research_state(
    session: Session, settings: Settings, security_ids: list[int] | None = None
) -> dict[str, Any]:
    # Lazy import keeps the web composition independent of the background coordinator.
    from ..api import build_research_context
    from .action_evaluation import record_action_snapshot
    from .research_workflow import sync_action_tasks
    from .scoring import rebuild_signal_snapshots

    # Claim synchronization covers every security, even for a scoped capture.
    claims, invalidated = sync_claims_and_reconcile(session)
    if invalidated:
        rebuild_signal_snapshots(session)
    query = select(Security).join(Watchlist).where(Watchlist.active.is_(True)).options(
        selectinload(Security.watchlist_entry), selectinload(Security.pe_analysis_profile)
    )
    if security_ids is not None:
        query = query.where(Security.id.in_(security_ids))
    securities = list(session.scalars(query))
    holdings = {
        security.id: security.watchlist_entry.holding_status
        for security in securities if security.watchlist_entry
    }
    context = build_research_context(session, settings, securities, holdings)
    snapshots = []
    for security in securities:
        row = context[security.id]
        for horizon in (1, 5, 20):
            judgment = row["judgments"][str(horizon)]
            judgment["action"]["plan"] = sync_action_tasks(
                session, security.id, horizon, judgment["action"]["plan"]
            )
            snapshots.append(record_action_snapshot(
                session, security.id, horizon, judgment,
                signal=row["signals"][str(horizon)], market_research=row["market_research"],
                risk_plan=row["risk_plans"][str(horizon)],
            ))
    session.flush()
    return {
        "securities": len(securities), "snapshots": len(snapshots), "claims": claims,
        "invalidated_event_ids": invalidated,
    }
