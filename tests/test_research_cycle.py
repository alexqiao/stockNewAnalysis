from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import EventSecurityImpact, Security, SecuritySignalSnapshot
from trade_news_analysis.risk_models import ActionDecisionSnapshot
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.research_cycle import capture_research_state
from trade_news_analysis.services.research_workflow import sync_x_claims, update_claim
from trade_news_analysis.services.scoring import rebuild_signal_snapshots
from trade_news_analysis.services.x_posts import BrowserPost, XIngestionService
from trade_news_analysis.workflow_models import XClaim

from .test_analysis import add_pending_event, completion
from .test_research_workflow import saved_post, support


@pytest.mark.parametrize("change", ["source_changed", "expired"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("capture_actions", [False, True])
def test_claim_sync_withdraws_analysis_before_recording_action_inputs(
    session: Session, session_factory: SessionFactory, settings: Settings,
    change: str, legacy: bool, capture_actions: bool,
) -> None:
    now = datetime.now(UTC)
    post = saved_post(session, published_at=now - timedelta(hours=1))
    sync_x_claims(session, now)
    claim = session.scalar(select(XClaim))
    assert claim is not None
    update_claim(
        session, claim.id, status="verified", note="已核对官方订单披露", evidence=[support()],
        review_due_at=now + timedelta(hours=1), now=now,
    )
    session.commit()
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    impact = session.scalar(select(EventSecurityImpact))
    assert impact is not None and impact.is_current
    if legacy:
        impact.research_inputs = {}
        session.commit()
    security_id = impact.security_id
    original = rebuild_signal_snapshots(session)
    assert any(snapshot.evidence_event_ids == [event.id] for snapshot in original)
    if change == "source_changed":
        XIngestionService._upsert_post(session, post.account, BrowserPost(
            post_id=post.post_id, url=post.url, post_type=post.post_type,
            text=post.text + " Update: contract cancelled.", published_at=post.published_at,
        ))
        session.commit()

    reviewed_at = now + timedelta(hours=2) if change == "expired" else now
    with patch("trade_news_analysis.services.research_workflow._now", return_value=reviewed_at):
        if capture_actions:
            result = capture_research_state(session, settings, [security_id])
            assert result["invalidated_event_ids"] == [event.id]
        else:
            coordinator = PipelineCoordinator(
                session_factory, settings.model_copy(update={"research_refresh_enabled": False}),
            )
            try:
                with patch.object(coordinator.x_ingestion, "screen_pending", return_value=set()):
                    coordinator._finish_x_ingestion()
            finally:
                coordinator.shutdown()
            session.expire_all()
    session.commit()
    assert claim.status == ("expired" if change == "expired" else "pending")
    assert event.analysis_stale and event.status == "stale"
    assert event.evidence_version == 1
    assert not impact.is_current
    latest = session.scalars(select(ActionDecisionSnapshot).where(
        ActionDecisionSnapshot.security_id == security_id,
    )).all()
    assert len(latest) == (3 if capture_actions else 0)
    assert all(snapshot.inputs["signal"]["evidence_event_ids"] == [] for snapshot in latest)
    assert all(snapshot.inputs["signal"]["direction"] == "neutral" for snapshot in latest)


def test_unchanged_legacy_claim_does_not_invalidate_analysis(
    session: Session, settings: Settings,
) -> None:
    now = datetime.now(UTC)
    saved_post(session, published_at=now - timedelta(hours=1))
    sync_x_claims(session, now)
    claim = session.scalar(select(XClaim))
    assert claim is not None
    update_claim(
        session, claim.id, status="verified", note="已核对官方披露", evidence=[support()],
        review_due_at=now + timedelta(days=1), now=now,
    )
    session.commit()
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    impact = session.scalar(select(EventSecurityImpact))
    assert impact is not None
    impact.research_inputs = {}
    session.commit()
    rebuild_signal_snapshots(session)

    result = capture_research_state(session, settings, [impact.security_id])
    assert result["invalidated_event_ids"] == []
    assert claim.status == "verified"
    assert impact.is_current
    assert event.status == "complete" and not event.analysis_stale


def test_scoped_capture_still_reconciles_claims_for_other_securities(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    impact = session.scalar(select(EventSecurityImpact))
    assert impact is not None
    impact.research_inputs = {**impact.research_inputs, "material_hash": "older-material-inputs"}
    session.commit()
    rebuild_signal_snapshots(session)
    another_id = session.scalar(select(Security.id).where(Security.symbol == "MSFT"))
    assert another_id is not None

    result = capture_research_state(session, settings, [another_id])
    assert result["invalidated_event_ids"] == [event.id]
    assert not impact.is_current
    latest = session.scalar(select(SecuritySignalSnapshot).where(
        SecuritySignalSnapshot.security_id == impact.security_id,
    ).order_by(SecuritySignalSnapshot.id.desc()))
    assert latest is not None and latest.direction == "neutral"
