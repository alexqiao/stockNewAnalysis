from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Article, EventArticle, EventSecurityImpact, Security
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.analysis_context import (
    build_analysis_context,
    material_research_hash,
)
from trade_news_analysis.services.analysis_versions import (
    invalidate_event_analysis,
    reconcile_research_inputs,
)
from trade_news_analysis.services.scoring import rebuild_signal_snapshots

from .test_analysis import (
    VALID_EVENT_PAYLOAD,
    VALID_IMPACT_PAYLOAD,
    add_pending_event,
    completion,
)


@pytest.mark.parametrize("change", ["withdraw", "new_evidence"])
@pytest.mark.parametrize("model_fails", [False, True])
def test_concurrent_evidence_change_discards_inflight_result(
    session: Session, session_factory: SessionFactory, settings: Settings,
    change: str, model_fails: bool,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    rebuild_signal_snapshots(session)
    changed = False

    def responding(_system: str, prompt: str) -> str:
        nonlocal changed
        if not changed:
            with session_factory() as writer:
                article = writer.scalar(select(Article))
                assert article is not None
                if change == "withdraw":
                    article.analysis_eligible = False
                else:
                    added = Article(
                        fingerprint="b" * 64, canonical_url="https://example.com/new",
                        source="SEC EDGAR", title="New material fact", story_cluster_id="cluster",
                    )
                    writer.add(added)
                    writer.flush()
                    writer.add(EventArticle(event_id=event.id, article_id=added.id))
                invalidate_event_analysis(writer, [event.id], "changed while model waits")
                writer.commit()
            changed = True
        if model_fails:
            raise TimeoutError("test model timeout")
        return json.dumps(
            VALID_EVENT_PAYLOAD if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
        )

    EventAnalyzer(settings, completion=responding).analyze_event(session, event)
    assert event.status == ("excluded" if change == "withdraw" else "stale")
    assert event.evidence_version == 1
    assert event.analysis_next_retry_at is None
    assert session.scalar(select(func.count()).select_from(EventSecurityImpact)) == 1
    assert not session.scalar(select(EventSecurityImpact.id).where(
        EventSecurityImpact.is_current.is_(True),
    ))
    snapshots = rebuild_signal_snapshots(session)
    assert snapshots and all(snapshot.direction == "neutral" for snapshot in snapshots)


def test_no_eligible_evidence_excludes_without_model_and_withdraws_signals(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    rebuild_signal_snapshots(session)
    event.article_links[0].article.analysis_eligible = False
    invalidate_event_analysis(session, [event.id], "manual withdrawal")
    session.commit()

    def forbidden(_system: str, _prompt: str) -> str:
        pytest.fail("Excluded evidence must not call the model")

    EventAnalyzer(settings, completion=forbidden).analyze_event(session, event)
    assert event.status == "excluded"
    assert not any(impact.is_current for impact in event.impacts)
    assert all(snapshot.direction == "neutral" for snapshot in rebuild_signal_snapshots(session))


@pytest.mark.parametrize("material_change", [False, True])
def test_material_input_changes_require_review_but_quote_updates_do_not(
    session: Session, settings: Settings, material_change: bool,
) -> None:
    context: dict[str, Any] = {
        "financial": {"metrics": [{"concept": "Revenue", "value": 100}], "gaps": []},
        "x_review_records": [], "as_of": "2026-01-01", "market": {"quote": 10},
    }
    context["material_hash"] = material_research_hash(context)
    event = add_pending_event(session)
    with patch(
        "trade_news_analysis.services.analysis.build_analysis_context", return_value=context,
    ):
        EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    stored = session.scalar(select(EventSecurityImpact))
    assert stored is not None and stored.research_inputs["financial"] == context["financial"]
    assert stored.research_inputs["event_evidence"]["event_id"] == event.id
    updated = {
        **context, "market": {"quote": 11}, "as_of": "2026-02-01",
        "financial": {"metrics": [{"concept": "Revenue", "value": 101}], "gaps": []}
        if material_change else context["financial"],
    }
    with patch(
        "trade_news_analysis.services.analysis_versions.build_analysis_context",
        return_value=updated,
    ):
        ids = reconcile_research_inputs(session)
        assert ids == ([event.id] if material_change else [])
        assert reconcile_research_inputs(session) == []
    assert stored.is_current is not material_change
    assert event.status == ("stale" if material_change else "complete")


def test_legacy_impact_without_inputs_is_not_mass_invalidated(session: Session) -> None:
    event = add_pending_event(session)
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    session.add(EventSecurityImpact(event_id=event.id, security_id=security.id, status="complete"))
    session.commit()
    assert reconcile_research_inputs(session) == []


def test_legacy_impact_invalidates_only_on_observed_refresh_change(session: Session) -> None:
    event = add_pending_event(session)
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    session.add(EventSecurityImpact(event_id=event.id, security_id=security.id, status="complete"))
    session.commit()
    current = build_analysis_context(session, security.id)
    assert reconcile_research_inputs(
        session, previous_hashes={security.id: material_research_hash(current)},
    ) == []
    assert reconcile_research_inputs(
        session, previous_hashes={security.id: "previous-material-content"},
    ) == [event.id]


def test_first_analysis_rechecks_material_inputs_before_commit(
    session: Session, settings: Settings,
) -> None:
    event = add_pending_event(session)
    context: dict[str, Any] = {"financial": {"value": 1}, "x_review_records": []}

    def build(_session: Session, _security_id: int) -> dict[str, Any]:
        result = json.loads(json.dumps(context))
        result["material_hash"] = material_research_hash(result)
        return result

    def complete(system: str, prompt: str) -> str:
        if "canonical_title" not in prompt:
            context["financial"]["value"] = 2
        return completion(system, prompt)

    with patch("trade_news_analysis.services.analysis.build_analysis_context", side_effect=build):
        EventAnalyzer(settings, completion=complete).analyze_event(session, event)
    assert event.status == "stale"
    assert event.evidence_version == 1
    assert session.scalar(select(func.count()).select_from(EventSecurityImpact)) == 0


def test_context_preserves_metric_categories_and_orders_surprises_by_publication(
    session: Session,
) -> None:
    concepts = [f"RevenueSegment{index}" for index in range(30)] + [
        "NetIncomeLoss", "OperatingIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
        "EarningsPerShareBasic", "LongTermDebt",
    ]
    financial = {
        "as_of": "2026-09-30", "gaps": [],
        "metrics": [{
            "concept": concept,
            "latest": {"period_end": "2026-06-30", "available_at": "2026-07-01"},
        } for concept in concepts],
        "surprises": [
            {"event_key": "old", "metric": "revenue", "actual_published_at": "2025-01-01"},
            {"event_key": "new", "metric": "revenue", "actual_published_at": "2026-09-01"},
        ],
    }
    with patch(
        "trade_news_analysis.services.analysis_context.get_financial_research",
        return_value=financial,
    ), patch(
        "trade_news_analysis.services.analysis_context.get_market_research", return_value={},
    ), patch("trade_news_analysis.services.analysis_context.list_claims", return_value=[]):
        context = build_analysis_context(session, 1)
    selected = {item["concept"] for item in context["financial"]["metrics"]}
    assert set(concepts[-5:]).issubset(selected)
    assert len(selected) == 20
    assert context["financial"]["surprises"][0]["event_key"] == "new"
