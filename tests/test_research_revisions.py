from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Article, Event, EventArticle, EventSecurityImpact
from trade_news_analysis.services.research_workflow import (
    _business_deadline,
    claim_page,
    list_claims,
    sync_action_tasks,
    sync_x_claims,
    update_task,
)
from trade_news_analysis.workflow_models import ActionTask, ActionTaskHistory, XClaim, XClaimHistory

from . import test_research_api as api_fixtures
from .test_research_workflow import NOW, generic_plan, saved_post, security_id, support

decisions_client = api_fixtures.decisions_client
decision_security = api_fixtures.decision_security


def test_api_requires_revision_and_rejects_stale_task_without_audit(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        task = sync_action_tasks(session, security_id(session), 5, generic_plan(), now)["tasks"][0]
        session.commit()
    endpoint = f"/api/v1/research/tasks/{task['id']}"
    payload = {"status": "done", "note": "页面甲已核对"}
    assert decisions_client.patch(endpoint, json=payload).status_code == 422
    first = decisions_client.patch(
        endpoint, json={**payload, "expected_revision": task["revision"]},
    )
    assert first.status_code == 200
    assert first.json()["revision"] > task["revision"]
    second = decisions_client.patch(endpoint, json={
        "status": "dismissed", "note": "页面乙的旧草稿", "expected_revision": task["revision"],
    })
    assert second.status_code == 409
    assert second.json()["detail"]["latest"]["note"] == "页面甲已核对"
    with session_factory() as session:
        assert session.scalar(select(func.count(ActionTaskHistory.id))) == 2


def test_claim_conflict_keeps_new_evidence_and_history(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        saved_post(session, published_at=now - timedelta(hours=1))
        sync_x_claims(session, now)
        claim = list_claims(session, now=now)[0]
        session.commit()
    endpoint = f"/api/v1/research/claims/{claim['id']}"
    first = decisions_client.patch(endpoint, json={
        "status": "verified", "note": "官方依据", "evidence": [support()],
        "expected_revision": claim["revision"],
    })
    assert first.status_code == 200
    second = decisions_client.patch(endpoint, json={
        "status": "pending", "note": "旧页面无依据", "evidence": [],
        "expected_revision": claim["revision"],
    })
    assert second.status_code == 409
    latest = decisions_client.get(endpoint).json()
    assert latest["status"] == "verified"
    assert len(latest["evidence"]) == 1
    assert len(latest["history"]) == 2
    assert second.json()["detail"]["latest"]["revision"] == latest["revision"]


def test_background_stale_session_cannot_overwrite_manual_edit(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        first = sync_action_tasks(session, security_id(session), 5, generic_plan(), NOW)["tasks"][0]
        session.commit()
    with session_factory() as background, session_factory() as human:
        old = background.get(ActionTask, first["id"])
        assert old is not None
        # End the read transaction while retaining the deliberately stale ORM object.
        background.commit()
        update_task(human, first["id"], status="done", note="人工新记录", now=NOW,
                    expected_revision=first["revision"])
        human.commit()
        old.note = "后台旧记录"
        old.revision += 1
        with pytest.raises(StaleDataError):
            background.commit()


def test_claim_page_bounds_history_and_keeps_pagination_stable(session: Session) -> None:
    saved_post(session, screening={
        "classification": "fact", "entities": ["AAPL"], "horizon": "1w",
        "factual_claims": [f"Apple claim {index}" for index in range(45)],
    })
    sync_x_claims(session, NOW)
    session.flush()
    first = claim_page(session, security_id(session), now=NOW)
    assert len(first["items"]) == 20
    second = claim_page(session, security_id(session), before_id=first["next_cursor"], now=NOW)
    third = claim_page(session, security_id(session), before_id=second["next_cursor"], now=NOW)
    ids = [item["id"] for page in [first, second, third] for item in page["items"]]
    assert len(set(ids)) == 45 and ids == sorted(ids, reverse=True)
    assert third["next_cursor"] is None
    assert claim_page(session, -1, now=NOW)["items"] == []
    claim = session.get(XClaim, ids[0])
    assert claim is not None
    for index in range(50):
        session.add(XClaimHistory(claim_id=claim.id, from_status="pending", to_status="pending",
                                  reason="manual_review", note=str(index), created_at=NOW))
    session.flush()
    session.expire_all()
    page = claim_page(session, security_id(session), now=NOW)
    assert len(page["items"][0]["history"]) == 1
    assert page["items"][0]["history_deferred"] is True
    assert not any(isinstance(item, XClaimHistory) and item.note != "49"
                   for item in session.identity_map.values())
    assert len(list_claims(session, claim_ids=[claim.id], now=NOW)[0]["history"]) == 51
    with pytest.raises(ValueError):
        claim_page(session, limit=101)


@pytest.mark.parametrize(("market", "start", "expected"), [
    ("US", datetime(2026, 11, 25, 12, tzinfo=UTC), datetime(2026, 11, 27, 18, tzinfo=UTC)),
    ("A", datetime(2026, 9, 30, 8, tzinfo=UTC), datetime(2026, 10, 8, 7, tzinfo=UTC)),
    ("HK", datetime(2026, 9, 30, 8, tzinfo=UTC), datetime(2026, 10, 2, 8, tzinfo=UTC)),
])
def test_review_deadline_uses_exchange_holidays_and_half_day(
    market: str, start: datetime, expected: datetime,
) -> None:
    assert _business_deadline(start, 1, market) == expected


def test_manual_task_deadline_survives_source_revisions(session: Session) -> None:
    plan = generic_plan()
    task = sync_action_tasks(session, security_id(session), 5, plan, NOW)["tasks"][0]
    chosen = NOW + timedelta(days=30, hours=4)
    update_task(session, task["id"], status="pending", note="人工约定期限", now=NOW,
                review_due_at=chosen, expected_revision=task["revision"])
    plan["tasks"][0]["reference"] = "新的来源版本"
    revised = sync_action_tasks(session, security_id(session), 5, plan, NOW)["tasks"][0]
    assert revised["review_due_at"] == chosen.isoformat()
    assert revised["revision"] > task["revision"]


def test_disclosure_explicit_link_invalidates_only_on_change_and_validates_association(
    decisions_client: TestClient, decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> None:
    identifier, impact_id = decision_security
    with session_factory() as session:
        impact = session.get(EventSecurityImpact, impact_id)
        assert impact is not None
        event_id = impact.event_id
        article = Article(
            fingerprint="manual-link-test", canonical_url="https://news.example/report",
            title="已有报道", source="News", story_cluster_id="manual-link-test",
            published_at=datetime.now(UTC), analysis_eligible=True,
        )
        session.add(article)
        session.flush()
        session.add(EventArticle(event_id=event_id, article_id=article.id))
        session.commit()
        article_id = article.id
    base = f"/api/v1/research/securities/{identifier}"
    choices = decisions_client.get(base).json()["related_reports"]
    assert any(row["article_id"] == article_id and row["event_id"] == event_id for row in choices)
    payload = {"title": "官方原文", "source_url": "https://www.sec.gov/Archives/source.htm",
               "article_id": article_id, "event_id": event_id}
    invalid = decisions_client.post(base + "/disclosures", json={**payload, "event_id": 999999})
    assert invalid.status_code == 422
    assert decisions_client.post(base + "/disclosures", json=payload).status_code == 200
    with session_factory() as session:
        article = session.get(Article, article_id)
        event = session.get(Event, event_id)
        assert article is not None and article.original_source_url == payload["source_url"]
        assert event is not None and event.analysis_stale is True
        version = event.evidence_version
        impact = session.get(EventSecurityImpact, impact_id)
        assert impact is not None and impact.is_current is False
    assert decisions_client.post(base + "/disclosures", json=payload).status_code == 200
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None and event.evidence_version == version
