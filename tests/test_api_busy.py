from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import Article, Event, EventArticle, XAccount, XPost
from trade_news_analysis.services.coordinator import PipelineBusyError, PipelineCoordinator


@pytest.fixture
def busy_client(
    settings: Settings, session_factory: SessionFactory,
) -> Iterator[tuple[TestClient, PipelineCoordinator]]:
    coordinator = PipelineCoordinator(
        session_factory, settings, source_factory=lambda _securities, _settings: [],
    )
    with TestClient(create_app(settings, session_factory, coordinator)) as client:
        yield client, coordinator


@pytest.fixture
def pending_post(session_factory: SessionFactory) -> tuple[int, int]:
    with session_factory() as session:
        evidence = Event(event_key="api-busy", title="Company order", status="complete")
        account = session.scalar(select(XAccount).order_by(XAccount.id))
        assert account is not None
        post = XPost(
            account_id=account.id, post_id="100001", url="https://x.com/author/status/100001",
            text="Company order announcement", post_type="original", published_at=datetime.now(UTC),
            screening_status="review",
        )
        session.add_all([evidence, post])
        session.commit()
        return post.id, evidence.id


@pytest.mark.parametrize("status", ["complete", "error"])
def test_busy_reanalysis_preserves_previous_event_state(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    client, coordinator = busy_client
    _, event_id = pending_post
    with session_factory() as session:
        evidence = session.get(Event, event_id)
        assert evidence is not None
        evidence.status, evidence.error = status, "previous error"
        evidence.analysis_attempts = 2
        session.commit()

    def busy(_event_id: int) -> None:
        raise PipelineBusyError("已有任务正在运行，请稍后重新分析")

    monkeypatch.setattr(coordinator, "submit_analysis", busy)
    response = client.post(f"/api/v1/events/{event_id}/analyses")
    assert response.status_code == 409
    assert "稍后重新分析" in response.json()["detail"]
    with session_factory() as session:
        evidence = session.get(Event, event_id)
        assert evidence is not None
        assert (evidence.status, evidence.error, evidence.analysis_attempts) == (
            status, "previous error", 2,
        )


def test_accepted_reanalysis_leaves_status_change_to_worker(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, coordinator = busy_client
    _, event_id = pending_post
    submitted: list[int] = []
    monkeypatch.setattr(coordinator, "submit_analysis", submitted.append)
    response = client.post(f"/api/v1/events/{event_id}/analyses")
    assert response.status_code == 202
    assert response.json() == {"event_id": event_id, "status": "queued"}
    assert submitted == [event_id]
    with session_factory() as session:
        evidence = session.get(Event, event_id)
        assert evidence is not None and evidence.status == "complete"
    assert client.post("/api/v1/events/999999/analyses").status_code == 404
    assert submitted == [event_id]


@pytest.mark.parametrize("auto_analyze", [True, False])
@pytest.mark.parametrize("mode,expected,message", [
    ("busy", "pending", "后台正忙"),
    ("available", "queued", "已排队"),
])
def test_x_manual_decision_submits_independently_of_auto_analysis_setting(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch,
    mode: str, expected: str, message: str, auto_analyze: bool,
) -> None:
    client, coordinator = busy_client
    post_id, event_id = pending_post
    settings.auto_analyze = auto_analyze
    submitted = []

    def submit(identifier: int) -> None:
        submitted.append(identifier)
        if mode == "busy":
            raise PipelineBusyError("busy")

    monkeypatch.setattr(coordinator, "submit_analysis", submit)
    response = client.post(f"/api/v1/x/posts/{post_id}/decision", json={
        "decision": "promote", "event_id": event_id,
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["post_id"] == post_id
    assert payload["decision"] == "promote"
    assert payload["event_id"] == event_id
    assert payload["analysis_status"] == expected
    assert "人工决定已保存" in payload["message"]
    assert message in payload["message"]
    if expected == "pending":
        assert f"#{event_id}" in payload["message"]
        assert "重新分析" in payload["message"]
    assert submitted == [event_id]
    with session_factory() as session:
        post = session.get(XPost, post_id)
        assert post is not None and post.screening_status == "promoted"
        assert post.promoted_article_id is not None
        assert post.related_event_id == event_id


def test_unlinked_x_decision_does_not_submit_analysis(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, coordinator = busy_client
    submitted: list[int] = []
    monkeypatch.setattr(coordinator, "submit_analysis", submitted.append)
    response = client.post(
        f"/api/v1/x/posts/{pending_post[0]}/decision", json={"decision": "ignore"},
    )
    assert response.status_code == 200
    assert response.json()["analysis_status"] == "not_required"
    assert submitted == []


@pytest.mark.parametrize("remaining_fact", [False, True])
def test_withdrawing_x_evidence_rebuilds_without_automatic_model_call(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    session_factory: SessionFactory, monkeypatch: pytest.MonkeyPatch, remaining_fact: bool,
) -> None:
    client, coordinator = busy_client
    post_id, event_id = pending_post
    submitted: list[int] = []
    monkeypatch.setattr(coordinator, "submit_analysis", submitted.append)
    assert client.post(f"/api/v1/x/posts/{post_id}/decision", json={
        "decision": "promote", "event_id": event_id,
    }).status_code == 200
    submitted.clear()
    if remaining_fact:
        with session_factory() as session:
            article = Article(
                fingerprint="remaining-fact", canonical_url="https://example.test/disclosure",
                source="official", title="Company disclosure", story_cluster_id="remaining-fact",
            )
            session.add(article)
            session.flush()
            session.add(EventArticle(event_id=event_id, article_id=article.id))
            session.commit()
    response = client.post(f"/api/v1/x/posts/{post_id}/decision", json={
        "decision": "context",
    })
    assert response.status_code == 200
    assert response.json()["analysis_status"] == (
        "reanalysis_required" if remaining_fact else "not_required"
    )
    assert submitted == []
    with session_factory() as session:
        event = session.get(Event, event_id)
        assert event is not None
        assert event.status == ("stale" if remaining_fact else "excluded")


def test_busy_decision_notice_survives_page_reload_and_reanalysis_can_retry(
    busy_client: tuple[TestClient, PipelineCoordinator], pending_post: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playwright_api = pytest.importorskip("playwright.sync_api")
    client, coordinator = busy_client
    post_id, event_id = pending_post
    state = {"busy": True}

    def submit(_identifier: int) -> None:
        if state["busy"]:
            raise PipelineBusyError("已有任务正在运行，请稍后重新分析")

    monkeypatch.setattr(coordinator, "submit_analysis", submit)
    coordinator._x_status = "screening"

    def route_request(route: Any) -> None:
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != "busy.test":
            route.abort()
            return
        headers = {"Content-Type": request.headers["content-type"]} \
            if "content-type" in request.headers else {}
        response = client.request(
            request.method, url.path, content=request.post_data, headers=headers,
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers),
                      body=response.content)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error as exc:
            if "executable" in str(exc).lower() or "not found" in str(exc).lower():
                pytest.skip("Local Chrome is required for busy decision feedback tests")
            raise
        try:
            page = browser.new_page()
            page.route("**/*", route_request)
            page.goto("http://busy.test/x")
            card = page.locator(f"#post-{post_id}")
            card.locator('[data-decision="promote"]').click()
            playwright_api.expect(card.locator(".direction")).to_have_text("promoted")
            playwright_api.expect(page.locator("#decision-message")).to_contain_text("人工决定已保存")
            playwright_api.expect(page.locator("#decision-message")).to_contain_text("后台正忙")
            playwright_api.expect(page.locator("#decision-message")).to_contain_text("重新分析")
            playwright_api.expect(page.locator("#message")).to_contain_text("正在进行模型筛选")
            page.goto(f"http://busy.test/events/{event_id}")
            button = page.locator("#reanalyze")
            button.click()
            playwright_api.expect(page.locator("#reanalyze-message")).to_contain_text("稍后重新分析")
            playwright_api.expect(button).to_be_enabled()
            state["busy"] = False
            button.click()
            playwright_api.expect(button).to_have_text("已排队")
        finally:
            browser.close()
