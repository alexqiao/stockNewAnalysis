"""Load shared research inputs once for a request and reuse them across horizons."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..models import EventSecurityImpact, PEAnalysisProfile, Security
from ..research_data_models import ResearchCalendarRevision
from ..risk_models import MarketResearchSnapshot
from .market_research import market_research_payload
from .research_workflow import ActionPlanData, load_action_plan_data
from .risk import RiskInputsData, load_risk_inputs


@dataclass
class ResearchInputs:
    market: dict[int, dict[str, Any]]
    pe_profiles: dict[int, PEAnalysisProfile]
    risk: RiskInputsData
    actions: ActionPlanData
    calendars: dict[int, list[ResearchCalendarRevision]]


def load_research_inputs(
    session: Session, securities: Sequence[Security], impacts: Sequence[EventSecurityImpact],
    holdings: dict[str, Any], now: datetime,
) -> ResearchInputs:
    ids = {security.id for security in securities}
    ranked = select(
        MarketResearchSnapshot.id,
        func.row_number().over(
            partition_by=MarketResearchSnapshot.security_id,
            order_by=(MarketResearchSnapshot.as_of.desc(), MarketResearchSnapshot.id.desc()),
        ).label("recency"),
    ).where(MarketResearchSnapshot.security_id.in_(ids)).subquery()
    snapshots = {row.security_id: row for row in session.scalars(
        select(MarketResearchSnapshot).join(ranked, MarketResearchSnapshot.id == ranked.c.id)
        .where(ranked.c.recency == 1)
    )}
    market = {security.id: market_research_payload(
        security.id, security, snapshots.get(security.id), now,
    ) for security in securities}
    pe_profiles = {row.security_id: row for row in session.scalars(
        select(PEAnalysisProfile).where(PEAnalysisProfile.security_id.in_(ids))
    )}
    records: dict[int | None, list[ResearchCalendarRevision]] = defaultdict(list)
    for row in session.scalars(select(ResearchCalendarRevision).where(
        or_(ResearchCalendarRevision.security_id.in_(ids),
            ResearchCalendarRevision.security_id.is_(None)),
        ResearchCalendarRevision.observed_at <= now,
        ResearchCalendarRevision.available_at <= now,
    ).order_by(ResearchCalendarRevision.revision.desc(), ResearchCalendarRevision.id.desc())):
        records[row.security_id].append(row)
    calendars = {security_id: sorted(
        [*records[security_id], *records[None]],
        key=lambda row: (row.revision, row.id), reverse=True,
    ) for security_id in ids}
    return ResearchInputs(
        market, pe_profiles, load_risk_inputs(session, securities, holdings, market),
        load_action_plan_data(session, ids, impacts), calendars,
    )
