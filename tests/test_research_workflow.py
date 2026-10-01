from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import (
    Article,
    Event,
    EventSecurityImpact,
    Security,
    XAccount,
    XPost,
)
from trade_news_analysis.services.research_workflow import (
    list_claims,
    list_tasks,
    read_action_plan,
    sync_action_tasks,
    sync_x_claims,
    update_claim,
    update_task,
)
from trade_news_analysis.workflow_models import ActionTask, XClaim

NOW = datetime(2026, 9, 16, 8, tzinfo=UTC)


def security_id(session: Session) -> int:
    value = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
    assert value is not None
    return value


def generic_plan(detail: str = "核对报价时间") -> dict[str, Any]:
    return {"horizon": 5, "tasks": [{
        "kind": "quote", "title": "刷新报价", "detail": detail, "source_kind": "valuation",
        "source_id": None, "on_pass": "重新评估", "on_fail": "继续待核实", "priority": 10,
    }]}


def event_plan(session: Session) -> tuple[Event, dict[str, Any]]:
    event = Event(
        event_key="workflow-event", title="公司披露订单", status="complete",
        demand_status="observed", occurred_at=NOW, updated_at=NOW,
    )
    session.add(event)
    session.flush()
    session.add(EventSecurityImpact(
        event_id=event.id, security_id=security_id(session), status="complete", is_current=True,
        impacts={"5": {"direction": "bullish", "confidence": 0.8}},
    ))
    session.flush()
    return event, {"tasks": [{
        "kind": "evidence", "title": "核对订单", "detail": "核对官方公告订单金额",
        "source_kind": "event", "source_id": event.id,
    }]}


def saved_post(session: Session, **overrides: Any) -> XPost:
    account = session.scalar(select(XAccount).order_by(XAccount.id))
    assert account is not None
    values: dict[str, Any] = {
        "account_id": account.id, "post_id": "1990000000000000000",
        "url": "https://x.com/researcher/status/1990000000000000000", "post_type": "original",
        "text": "Apple signed a supply contract.", "published_at": NOW - timedelta(hours=1),
        "screening_status": "review", "screening": {
            "classification": "fact", "factual_claims": ["Apple signed a supply contract"],
            "claim_summary": "Apple signed a supply contract", "entities": ["AAPL"],
            "verification_needs": ["核对公司公告"], "horizon": "1w",
        },
        "raw_data": {},
    }
    values.update(overrides)
    post = XPost(**values)
    session.add(post)
    session.flush()
    return post


def support(**overrides: Any) -> dict[str, Any]:
    return {
        "url": "https://investor.example.com/releases/order", "stance": "support",
        "is_official": True, "note": "公司公告明确订单金额与签约日期", **overrides,
    }


def test_task_completion_survives_sync_and_new_session(session_factory: SessionFactory) -> None:
    with session_factory() as session:
        identifier = security_id(session)
        plan = generic_plan()
        first = sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"][0]
        done = update_task(session, first["id"], status="done", note="已核对数据日期", now=NOW)
        session.commit()
        assert done["completion_does_not_trigger_trade"] is True
    with session_factory() as session:
        result = sync_action_tasks(session, identifier, 5, plan, NOW + timedelta(hours=1))
        task = result["tasks"][0]
        assert task["id"] == first["id"]
        assert task["status"] == "done"
        assert task["note"] == "已核对数据日期"
        assert task["review_due_at"] == first["review_due_at"]
        assert [row["reason"] for row in task["history"]] == ["created", "manual_review"]


def test_task_keys_separate_horizons_and_changed_details(session: Session) -> None:
    identifier = security_id(session)
    five = sync_action_tasks(session, identifier, 5, generic_plan(), NOW)["tasks"][0]
    one = sync_action_tasks(session, identifier, 1, generic_plan(), NOW)["tasks"][0]
    revised = sync_action_tasks(session, identifier, 5, generic_plan("核对报价币种"), NOW)
    assert len({five["id"], one["id"], revised["tasks"][0]["id"]}) == 3
    old = session.get(ActionTask, five["id"])
    assert old is not None and old.status == "expired"
    assert old.expiration_reason == "not_in_plan"


def test_task_missing_then_restored_reopens_with_history(session: Session) -> None:
    identifier = security_id(session)
    task = sync_action_tasks(session, identifier, 5, generic_plan(), NOW)["tasks"][0]
    update_task(session, task["id"], status="dismissed", note="暂不评估", now=NOW)
    sync_action_tasks(session, identifier, 5, {"tasks": []}, NOW)
    restored = sync_action_tasks(session, identifier, 5, generic_plan(), NOW)["tasks"][0]
    assert restored["id"] == task["id"]
    assert restored["status"] == "pending"
    assert [item["to_status"] for item in restored["history"]] == [
        "pending", "dismissed", "expired", "pending"
    ]


def test_changed_event_reopens_completed_task_but_timezone_reload_does_not(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        event, plan = event_plan(session)
        identifier, event_id = security_id(session), event.id
        task = sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"][0]
        update_task(session, task["id"], status="done", note="首次公告已核对", now=NOW)
        session.commit()
    with session_factory() as session:
        unchanged = sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"][0]
        assert unchanged["status"] == "done"
        stored_event = session.get(Event, event_id)
        assert stored_event is not None
        stored_event.summary = "公司补充公告修正订单数量"
        stored_event.updated_at = NOW + timedelta(hours=1)
        session.flush()
        readonly = read_action_plan(session, identifier, 5, plan, NOW + timedelta(hours=1))
        assert readonly["tasks"][0]["status"] == "pending"
        assert readonly["tasks"][0]["needs_refresh"] is True
        with pytest.raises(ValueError, match="先刷新"):
            update_task(session, task["id"], status="done", note="旧结果", now=NOW)
        changed = sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"][0]
        assert changed["id"] == task["id"]
        assert changed["status"] == "pending"
        assert changed["history"][-1]["reason"] == "source_changed"
        assert changed["history"][1]["note"] == "首次公告已核对"


def test_revoked_event_cannot_keep_active_tasks(session: Session) -> None:
    event, plan = event_plan(session)
    identifier = security_id(session)
    task = sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"][0]
    event.status = "excluded"
    session.flush()
    assert sync_action_tasks(session, identifier, 5, plan, NOW)["tasks"] == []
    with pytest.raises(ValueError, match="来源已不可用"):
        update_task(session, task["id"], status="done", note="旧结果", now=NOW)


def test_task_expiry_requires_explicit_future_deadline_to_reopen(session: Session) -> None:
    identifier = security_id(session)
    task = sync_action_tasks(session, identifier, 1, generic_plan(), NOW)["tasks"][0]
    later = NOW + timedelta(days=2)
    expired = sync_action_tasks(session, identifier, 1, generic_plan(), later)["tasks"][0]
    assert expired["status"] == "expired"
    again = sync_action_tasks(session, identifier, 1, generic_plan(), later)["tasks"][0]
    assert again["status"] == "expired"
    with pytest.raises(ValueError, match="期限"):
        update_task(session, task["id"], status="pending", note="重新核验", now=later)
    reopened = update_task(
        session, task["id"], status="pending", note="按新日期重新核验",
        review_due_at=later + timedelta(days=1), now=later,
    )
    assert reopened["status"] == "pending"
    assert reopened["history"][-1]["from_status"] == "expired"


def test_read_helpers_do_not_write_task_state(session: Session) -> None:
    identifier = security_id(session)
    unsaved = read_action_plan(session, identifier, 1, generic_plan(), NOW)
    assert unsaved["tasks"][0]["id"] is None
    assert unsaved["needs_refresh"] is True
    assert not session.new and not session.dirty
    sync_action_tasks(session, identifier, 1, generic_plan(), NOW)
    session.commit()
    later = NOW + timedelta(days=2)
    result = read_action_plan(session, identifier, 1, generic_plan(), later)
    assert result["tasks"][0]["status"] == "expired"
    assert list_tasks(session, identifier, now=later)[0]["status"] == "expired"
    assert session.scalar(select(ActionTask.status)) == "pending"
    assert not session.new and not session.dirty


def test_task_updates_need_reason_and_valid_state(session: Session) -> None:
    task = sync_action_tasks(session, security_id(session), 5, generic_plan(), NOW)["tasks"][0]
    with pytest.raises(ValueError, match="备注"):
        update_task(session, task["id"], status="done", note=" ", now=NOW)
    with pytest.raises(ValueError, match="状态"):
        update_task(session, task["id"], status="buy", note="买入", now=NOW)


def test_claim_sync_is_idempotent_and_never_promotes_or_scores(session: Session) -> None:
    post = saved_post(session, raw_data={"is_truncated": True})
    assert sync_x_claims(session, NOW)["created"] == 1
    assert sync_x_claims(session, NOW)["created"] == 0
    claim = list_claims(session, security_id(session), now=NOW)[0]
    assert claim["status"] == "pending"
    assert claim["source_truncated"] is True
    assert claim["source_snapshot"]["text"] == post.text
    assert claim["formal_score_impact"] == 0
    assert claim["independent_evidence_count"] == 0
    assert len(claim["history"]) == 1
    assert post.promoted_article_id is None
    assert session.scalar(select(func.count(Article.id))) == 0


def test_partial_event_retains_successful_stock_association(session: Session) -> None:
    evidence, _ = event_plan(session)
    evidence.status = "partial"
    saved_post(session, related_event_id=evidence.id, screening={
        "classification": "fact", "factual_claims": ["公司披露供应合同"], "entities": [],
    })
    sync_x_claims(session, NOW)
    claims = list_claims(session, security_id(session), now=NOW)
    assert len(claims) == 1
    assert claims[0]["securities"] == [{
        "security_id": security_id(session), "match_basis": "event",
    }]


def test_claim_summary_fallback_remains_unverified(session: Session) -> None:
    saved_post(session, screening={
        "classification": "prediction", "claim_summary": "预计 Apple 扩大采购",
        "entities": ["AAPL"], "factual_claims": [],
    })
    sync_x_claims(session, NOW)
    claim = list_claims(session, now=NOW)[0]
    assert claim["claim_kind"] == "summary_to_verify"
    assert claim["status"] == "pending"


def test_claim_ambiguous_ticker_does_not_match_security(session: Session) -> None:
    session.add(Security(market="HK", exchange="HKEX", symbol="AAPL", name="Other Apple"))
    saved_post(session)
    sync_x_claims(session, NOW)
    assert list_claims(session, security_id(session), now=NOW) == []
    assert len(list_claims(session, now=NOW)) == 1


def test_claim_verification_requires_official_evidence_and_audits_manual_result(
    session: Session,
) -> None:
    post = saved_post(session)
    sync_x_claims(session, NOW)
    claim_id = list_claims(session, now=NOW)[0]["id"]
    with pytest.raises(ValueError, match="官方支持"):
        update_claim(session, claim_id, status="verified", note="关键词相同", now=NOW)
    with pytest.raises(ValueError, match="官方支持"):
        update_claim(session, claim_id, status="verified", note="博主转述", now=NOW,
                     evidence=[support(is_official=False)])
    result = update_claim(session, claim_id, status="verified", note="逐项对照公司原始公告",
                          evidence=[support()], now=NOW)
    assert result["status"] == "verified"
    assert result["history"][-1]["from_status"] == "pending"
    assert result["history"][-1]["note"] == "逐项对照公司原始公告"
    assert result["official_support_count"] == 1
    assert post.promoted_article_id is None


def test_reposts_of_same_original_source_count_as_one_evidence(session: Session) -> None:
    saved_post(session)
    sync_x_claims(session, NOW)
    claim_id = list_claims(session, now=NOW)[0]["id"]
    result = update_claim(
        session, claim_id, status="partially_supported", note="两篇转述来自同一公告",
        evidence=[
            support(url="https://media-one.example/report", is_official=False,
                    source_url="https://issuer.example/order?utm_source=one#top"),
            support(url="https://media-two.example/report", is_official=False,
                    source_url="https://issuer.example/order?utm_source=two"),
        ], now=NOW,
    )
    assert result["independent_evidence_count"] == 1
    assert len(result["evidence"]) == 1
    assert result["official_support_count"] == 0


def test_official_conflict_blocks_verified_and_can_record_refutation(session: Session) -> None:
    saved_post(session)
    sync_x_claims(session, NOW)
    claim_id = list_claims(session, now=NOW)[0]["id"]
    entries = [support(), support(url="https://issuer.example/correction", stance="conflict",
                                  note="补充公告称合同尚未签署")]
    with pytest.raises(ValueError, match="反证"):
        update_claim(session, claim_id, status="verified", note="有冲突", evidence=entries, now=NOW)
    result = update_claim(session, claim_id, status="refuted", note="公司澄清尚未签约",
                          evidence=entries, now=NOW)
    assert result["status"] == "refuted"
    assert result["official_conflict_count"] == 1


def test_claim_source_change_reopens_previous_verification_and_keeps_history(
    session: Session,
) -> None:
    post = saved_post(session, raw_data={"is_truncated": True})
    sync_x_claims(session, NOW)
    claim_id = list_claims(session, now=NOW)[0]["id"]
    update_claim(session, claim_id, status="verified", note="核对官方全文中的该主张",
                 evidence=[support()], now=NOW)
    post.text = "Apple signed a supply contract, subject to regulatory approval."
    post.raw_data = {"is_truncated": False}
    session.flush()
    preview = list_claims(session, now=NOW)[0]
    assert preview["status"] == "pending"
    assert preview["needs_refresh"] is True
    assert not session.new and not session.dirty
    with pytest.raises(ValueError, match="先刷新"):
        update_claim(session, claim_id, status="verified", note="沿用旧核验", now=NOW)
    assert sync_x_claims(session, NOW)["updated"] == 1
    claim = list_claims(session, now=NOW)[0]
    assert claim["status"] == "pending"
    assert claim["source_truncated"] is False
    assert claim["history"][-1]["reason"] == "source_changed"
    assert claim["history"][-2]["snapshot"]["source_snapshot"]["is_truncated"] is True


def test_claim_expiration_is_read_only_until_sync_and_can_be_explicitly_reopened(
    session: Session,
) -> None:
    saved_post(session, screening={
        "classification": "fact", "factual_claims": ["Apple signed a contract"], "horizon": "1d"
    })
    sync_x_claims(session, NOW)
    session.commit()
    later = NOW + timedelta(days=3)
    claim = list_claims(session, now=later)[0]
    assert claim["status"] == "expired"
    assert session.scalar(select(XClaim.status)) == "pending"
    assert not session.new and not session.dirty
    assert sync_x_claims(session, later)["expired"] == 1
    with pytest.raises(ValueError, match="期限"):
        update_claim(session, claim["id"], status="pending", note="再核实", now=later)
    restored = update_claim(session, claim["id"], status="pending", note="延期等候官方答复",
                            review_due_at=later + timedelta(days=2), now=later)
    assert restored["status"] == "pending"
    assert restored["history"][-1]["from_status"] == "expired"


def test_ignored_and_restored_source_expires_and_reopens_claim(session: Session) -> None:
    post = saved_post(session)
    sync_x_claims(session, NOW)
    post.screening_status = "ignored"
    session.flush()
    assert sync_x_claims(session, NOW)["expired"] == 1
    assert list_claims(session, now=NOW)[0]["expiration_reason"] == "source_unavailable"
    post.screening_status = "review"
    session.flush()
    assert sync_x_claims(session, NOW)["updated"] == 1
    assert list_claims(session, now=NOW)[0]["status"] == "pending"


def test_suggested_official_article_is_only_a_candidate_not_verification(session: Session) -> None:
    saved_post(session)
    session.add(Article(
        fingerprint="workflow-candidate", canonical_url="https://issuer.example/official",
        source="Issuer", title="Apple discusses a future contract", published_at=NOW,
        story_cluster_id="workflow-candidate", evidence_role="official_primary",
    ))
    session.flush()
    sync_x_claims(session, NOW)
    claim = list_claims(session, now=NOW)[0]
    assert claim["suggested_sources"][0]["match_only"] is True
    assert claim["suggested_sources"][0]["is_official"] is True
    assert claim["status"] == "pending"
    assert claim["evidence"] == []


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "https://u:p@test/a"])
def test_invalid_evidence_url_cannot_change_claim(session: Session, url: str) -> None:
    saved_post(session)
    sync_x_claims(session, NOW)
    claim_id = list_claims(session, now=NOW)[0]["id"]
    with pytest.raises(ValueError, match="HTTP"):
        update_claim(session, claim_id, status="verified", note="核对", now=NOW,
                     evidence=[support(url=url)])
    assert list_claims(session, now=NOW)[0]["status"] == "pending"
