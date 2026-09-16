from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from trade_news_analysis.config import DEFAULT_X_ACCOUNTS, Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import (
    Article,
    Event,
    EventArticle,
    SourceHealth,
    XAccount,
    XPost,
)
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.ingestion import IngestionService
from trade_news_analysis.services.scoring import evidence_quality
from trade_news_analysis.services.x_posts import (
    BrowserPost,
    PlaywrightXFetcher,
    XIngestionService,
    XPostScreener,
    independent_evidence_count,
)

SCREENING = {
    "classification": "fact",
    "claim_summary": "某公司已签署新的供应协议",
    "themes": ["供应链"],
    "entities": ["Example Corp"],
    "stance": "bullish",
    "horizon": "1m",
    "market_relevance": 4,
    "specificity": 4,
    "incrementality": 4,
    "factual_claims": ["帖子称协议已经签署"],
    "verification_needs": ["核对公司公告"],
    "rationale": "具体但仍需核验",
}


class FakeFetcher:
    def __init__(self, posts: list[BrowserPost]):
        self.posts = posts
        self.calls: list[str] = []

    def fetch(self, handle: str) -> list[BrowserPost]:
        self.calls.append(handle)
        return self.posts if handle == DEFAULT_X_ACCOUNTS[0] else []


def browser_post(
    post_id: str,
    *,
    post_type: str = "original",
    age_hours: int = 1,
) -> BrowserPost:
    return BrowserPost(
        post_id=post_id,
        url=f"https://x.com/{DEFAULT_X_ACCOUNTS[0]}/status/{post_id}",
        post_type=post_type,
        text="Example Corp signed a new supply agreement.",
        published_at=datetime.now(UTC) - timedelta(hours=age_hours),
        quoted_post_id="999" if post_type == "quote" else None,
        quoted_author_handle="example" if post_type == "quote" else None,
        quoted_text="Primary announcement" if post_type == "quote" else "",
        external_links=["https://example.com/announcement"],
        media=[{"type": "image", "url": "https://example.com/chart.png"}],
    )


def x_service(
    session_factory: SessionFactory,
    settings: Settings,
    fetcher: FakeFetcher,
) -> XIngestionService:
    ingestion = IngestionService(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
        master_factory=lambda _settings: None,
    )
    screener = XPostScreener(
        settings,
        completion=lambda _system, _prompt: json.dumps(SCREENING, ensure_ascii=False),
    )
    return XIngestionService(
        session_factory,
        settings,
        ingestion,
        fetcher=fetcher,
        screener=screener,
    )


def test_browser_context_uses_installed_google_chrome(
    tmp_path: Path, settings: Settings
) -> None:
    class FakeChromium:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def launch_persistent_context(self, **kwargs: object) -> object:
            self.kwargs = kwargs
            return object()

    chromium = FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)
    configured = settings.model_copy(
        update={"x_browser_profile_path": tmp_path / "x-profile"}
    )

    result = PlaywrightXFetcher(configured)._context(playwright, headless=False)

    assert result is not None
    assert chromium.kwargs["channel"] == "chrome"
    assert chromium.kwargs["headless"] is False
    assert chromium.kwargs["user_data_dir"] == str(tmp_path / "x-profile")


def test_database_seeds_curated_x_accounts(session_factory: SessionFactory) -> None:
    with session_factory() as session:
        accounts = session.scalars(
            select(XAccount).order_by(XAccount.priority, XAccount.id)
        ).all()
        assert [item.handle for item in accounts] == list(DEFAULT_X_ACCOUNTS)
        assert {item.account_type for item in accounts} == {"commentator"}


def test_truncated_public_fact_stays_in_review_with_coverage(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    item = browser_post("888")
    item.raw_data = {"is_truncated": True}
    fetcher = FakeFetcher([item])
    fetcher.coverage = "public_page"  # type: ignore[attr-defined]
    with session_factory() as session:
        account = session.scalar(select(XAccount).where(XAccount.handle == DEFAULT_X_ACCOUNTS[0]))
        assert account is not None
        account.account_type = "company"
        session.commit()

    assert x_service(session_factory, settings, fetcher).execute() == set()

    with session_factory() as session:
        saved = session.scalar(select(XPost).where(XPost.post_id == "888"))
        assert saved is not None
        assert saved.screening_status == "review"
        assert saved.promoted_article_id is None
        assert "打开原帖核对完整正文" in saved.screening["verification_needs"]
        health = session.scalar(select(SourceHealth).where(
            SourceHealth.source == f"X:@{DEFAULT_X_ACCOUNTS[0]}"
        ))
        assert health is not None and health.coverage == "public_page"
        assert health.last_success_at is not None


def test_public_summary_never_overwrites_full_post_and_full_text_can_be_rescreened(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    item = browser_post("777")
    service = x_service(session_factory, settings, FakeFetcher([item]))
    with session_factory() as session:
        account = session.scalar(select(XAccount).where(XAccount.handle == DEFAULT_X_ACCOUNTS[0]))
        assert account is not None
        post, _ = service._upsert_post(session, account, item)
        full_text = post.text
        post.screening_status = "context"
        post.screening = {"classification": "opinion"}
        item.text = "Only a summary…"
        item.raw_data = {"is_truncated": True}
        post, created = service._upsert_post(session, account, item)
        assert not created
        assert post.text == full_text
        assert post.screening == {"classification": "opinion"}

        short = browser_post("778")
        short.raw_data = {"is_truncated": True}
        post, _ = service._upsert_post(session, account, short)
        post.screening_status = "review"
        post.screening = {"classification": "fact"}
        short.raw_data = {"is_truncated": False}
        short.text = "Now includes the full announcement."
        post, _ = service._upsert_post(session, account, short)
        assert post.text == short.text
        assert post.screening == {}


def test_failed_account_does_not_return_rolled_back_event_ids(
    session_factory: SessionFactory, settings: Settings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = x_service(session_factory, settings, FakeFetcher([
        browser_post("901"), browser_post("902")
    ]))
    with session_factory() as session:
        account = session.scalar(select(XAccount).where(XAccount.handle == DEFAULT_X_ACCOUNTS[0]))
        assert account is not None
        account.account_type = "company"
        session.commit()
    original_upsert = service._upsert_post

    def failing_upsert(
        session: Session, account: XAccount, item: BrowserPost
    ) -> tuple[XPost, bool]:
        if item.post_id == "902":
            raise RuntimeError("second post failed")
        return original_upsert(session, account, item)

    monkeypatch.setattr(service, "_upsert_post", failing_upsert)
    assert service.execute() == set()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert session.scalar(select(func.count()).select_from(XPost)) == 0


def test_x_ingestion_filters_types_and_keeps_commentator_fact_for_review(
    session_factory: SessionFactory, settings: Settings
) -> None:
    fetcher = FakeFetcher(
        [
            browser_post("1001"),
            browser_post("1002", post_type="quote"),
            browser_post("1003", post_type="reply"),
            browser_post("1004", post_type="repost"),
            browser_post("1005", age_hours=30),
        ]
    )
    service = x_service(session_factory, settings, fetcher)

    assert service.execute() == set()
    assert service.execute() == set()

    with session_factory() as session:
        posts = session.scalars(select(XPost).order_by(XPost.post_id)).all()
        assert [item.post_id for item in posts] == ["1001", "1002"]
        assert {item.screening_status for item in posts} == {"review"}
        assert all(item.promoted_article_id is None for item in posts)
        assert session.scalar(select(func.count()).select_from(Article)) == 0


def test_x_ingestion_retries_failed_screening_once_provider_recovers(
    session_factory: SessionFactory, settings: Settings
) -> None:
    calls: list[str] = []

    def completion(_system: str, prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            raise RuntimeError("temporary provider failure")
        return json.dumps(SCREENING, ensure_ascii=False)

    service = x_service(session_factory, settings, FakeFetcher([browser_post("1101")]))
    service.screener = XPostScreener(settings, completion=completion)
    assert service.execute() == set()
    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "1101"))
        assert post is not None
        assert post.screening_status == "review"
        assert "error" in post.screening

    assert service.execute() == set()
    assert service.execute() == set()

    assert len(calls) == 2
    with session_factory() as session:
        posts = session.scalars(select(XPost).where(XPost.post_id == "1101")).all()
        assert len(posts) == 1
        assert posts[0].screening == SCREENING
        assert posts[0].screening_status == "review"
        assert posts[0].promoted_article_id is None


@pytest.mark.parametrize("status", ["pending", "review"])
@pytest.mark.parametrize("screening", [{}, {"error": ""}])
def test_x_ingestion_retries_existing_posts_without_successful_screening(
    session_factory: SessionFactory,
    settings: Settings,
    status: str,
    screening: dict[str, object],
) -> None:
    service = x_service(session_factory, settings, FakeFetcher([browser_post("1201")]))
    service.execute()
    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "1201"))
        assert post is not None
        post.screening_status = status
        post.screening = screening
        session.commit()

    calls: list[str] = []

    def completion(_system: str, prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(SCREENING, ensure_ascii=False)

    service.screener = XPostScreener(settings, completion=completion)
    service.execute()
    service.execute()

    assert len(calls) == 1
    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "1201"))
        assert post is not None
        assert post.screening == SCREENING
        assert post.screening_status == "review"


@pytest.mark.parametrize(
    ("status", "screening"),
    [
        (status, screening)
        for status in ("context", "ignore", "ignored", "promoted")
        for screening in ({}, {"error": "prior failure"})
    ]
    + [("review", SCREENING), ("pending", SCREENING)],
)
def test_x_ingestion_preserves_decisions_and_successful_screening(
    session_factory: SessionFactory,
    settings: Settings,
    status: str,
    screening: dict[str, object],
) -> None:
    service = x_service(session_factory, settings, FakeFetcher([browser_post("1301")]))
    service.execute()
    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "1301"))
        assert post is not None
        post.screening_status = status
        post.screening = screening
        session.commit()

    calls: list[str] = []

    def completion(_system: str, prompt: str) -> str:
        calls.append(prompt)
        return json.dumps(SCREENING, ensure_ascii=False)

    service.screener = XPostScreener(settings, completion=completion)
    service.execute()

    assert calls == []
    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "1301"))
        assert post is not None
        assert post.screening_status == status
        assert post.screening == screening


def test_trusted_account_auto_promotes_and_manual_retraction_excludes_event(
    session_factory: SessionFactory, settings: Settings
) -> None:
    with session_factory() as session:
        session.execute(update(XAccount).values(active=False))
        account = session.scalar(
            select(XAccount).where(XAccount.handle == DEFAULT_X_ACCOUNTS[0])
        )
        assert account is not None
        account.active = True
        account.account_type = "company"
        session.commit()

    service = x_service(session_factory, settings, FakeFetcher([browser_post("2001")]))
    queued = service.execute()
    assert len(queued) == 1

    with session_factory() as session:
        post = session.scalar(select(XPost).where(XPost.post_id == "2001"))
        assert post is not None
        assert post.screening_status == "promoted"
        article = session.get(Article, post.promoted_article_id)
        assert article is not None
        assert article.content_kind == "social_post"
        assert article.evidence_role == "official_primary"
        assert article.analysis_eligible is True
        event_id = post.related_event_id
        assert event_id in queued

        assert service.apply_decision(session, post.id, "context") == event_id
        session.commit()
        assert article.analysis_eligible is False
        event = session.get(Event, event_id)
        assert event is not None
        assert event.status == "excluded"


def test_independent_evidence_count_excludes_personal_social_leads(
    session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        event = Event(event_key="evidence-test", title="Evidence", status="pending")
        session.add(event)
        session.flush()
        articles = [
            Article(
                fingerprint=str(index) * 64,
                canonical_url=f"https://example.com/{index}",
                source=source,
                title=f"Article {index}",
                story_cluster_id=event.event_key,
                content_kind=kind,
                evidence_role=role,
                author_handle=handle,
            )
            for index, (source, kind, role, handle) in enumerate(
                [
                    ("Wire", "news", "reporting", None),
                    ("X:@blogger", "social_post", "social_lead", "blogger"),
                    ("X:@official", "social_post", "official_primary", "official"),
                    ("X:@official", "social_post", "official_primary", "official"),
                ],
                1,
            )
        ]
        session.add_all(articles)
        session.flush()
        session.add_all(
            [EventArticle(event_id=event.id, article_id=item.id) for item in articles]
        )
        session.commit()
        session.refresh(event)
        assert independent_evidence_count(event) == 2
        assert evidence_quality(0) == 0


def test_x_account_and_feed_api(
    session_factory: SessionFactory, settings: Settings
) -> None:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
    )
    app = create_app(settings, session_factory, coordinator)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/x/accounts",
            json={"handle": "@new_author", "account_type": "media"},
        )
        assert created.status_code == 201
        account_id = created.json()["id"]
        updated = client.patch(
            f"/api/v1/x/accounts/{account_id}",
            json={"display_name": "New Author", "active": False},
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "New Author"
        assert client.post(
            "/api/v1/x/accounts", json={"handle": "new_author"}
        ).status_code == 409

        assert client.get("/x/accounts").status_code == 200
        feed = client.get("/x")
        assert feed.status_code == 200
        assert "博主动态" in feed.text
