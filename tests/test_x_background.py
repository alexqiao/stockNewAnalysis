from __future__ import annotations

from datetime import UTC, datetime
from threading import Event as ThreadEvent
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.config import DEFAULT_X_ACCOUNTS, Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import SourceHealth, XPost
from trade_news_analysis.schemas import XPostScreeningPayload
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.x_posts import BrowserPost, XPostScreener


def test_x_collects_while_news_is_busy_and_defers_screening(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings.model_copy(
        update={"auto_analyze": False, "research_refresh_enabled": False}
    )
    coordinator = PipelineCoordinator(
        session_factory, configured, source_factory=lambda _securities, _settings: [],
    )
    news_started = ThreadEvent()
    release_news = ThreadEvent()
    collected = ThreadEvent()

    def blocked_news() -> None:
        news_started.set()
        assert release_news.wait(timeout=15)

    coordinator._pipeline_future = coordinator.executor.submit(blocked_news)
    assert news_started.wait(timeout=2)
    handle = DEFAULT_X_ACCOUNTS[0]
    post = BrowserPost(
        post_id="123", url=f"https://x.com/{handle}/status/123", post_type="original",
        text="A recent public post", published_at=datetime.now(UTC),
    )
    coordinator.x_ingestion.fetcher = Mock(
        fetch=Mock(side_effect=lambda account: [post] if account == handle else []),
        coverage="public_page",
    )
    payload = XPostScreeningPayload(
        classification="opinion", claim_summary="作者观点", stance="neutral", horizon="1w",
        market_relevance=2, specificity=2, incrementality=2, rationale="待核验",
    )
    completion = Mock(return_value=payload.model_dump_json())
    coordinator.x_ingestion.screener = XPostScreener(configured, completion=completion)
    execute = coordinator.x_ingestion.execute

    def observe_collection(*, screen_posts: bool = True) -> set[int]:
        result = execute(screen_posts=screen_posts)
        collected.set()
        return result

    monkeypatch.setattr(coordinator.x_ingestion, "execute", observe_collection)
    with TestClient(create_app(configured, session_factory, coordinator)) as client:
        try:
            assert client.get("/api/v1/runs/x-ingest/status").json() == {"status": "idle"}
            assert client.post("/api/v1/runs/x-ingest").status_code == 202
            assert collected.wait(timeout=5)
            completion.assert_not_called()
            with session_factory() as session:
                saved = session.scalar(select(XPost).where(XPost.post_id == "123"))
                assert saved is not None
                assert saved.text == post.text
                assert saved.screening_status == "pending"
                health = session.scalar(select(SourceHealth).where(
                    SourceHealth.source == f"X:@{handle}"
                ))
                assert health is not None and health.last_success_at is not None
                assert health.coverage == "public_page"
            assert client.get("/api/v1/runs/x-ingest/status").json()["status"] == "queued_screening"
            assert client.post("/api/v1/runs/x-ingest").status_code == 409
        finally:
            release_news.set()
        assert coordinator._x_future is not None
        coordinator._x_future.result(timeout=5)
        assert client.get("/api/v1/runs/x-ingest/status").json() == {"status": "completed"}
        with session_factory() as session:
            saved = session.scalar(select(XPost).where(XPost.post_id == "123"))
            assert saved is not None and saved.screening_status == "context"
        completion.assert_called_once()
        coordinator.x_ingestion.screen_pending()
        completion.assert_called_once()


def test_failed_collection_is_visible_and_can_be_retried(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    coordinator = PipelineCoordinator(
        session_factory, settings, source_factory=lambda _securities, _settings: [],
    )
    coordinator.x_ingestion.fetcher = Mock(fetch=Mock(side_effect=RuntimeError("连接失败")))
    try:
        for _ in range(2):
            future = coordinator.submit_x_ingestion()
            with pytest.raises(RuntimeError, match="个账号失败"):
                future.result(timeout=5)
            assert coordinator.x_status == "failed"
    finally:
        coordinator.shutdown()
