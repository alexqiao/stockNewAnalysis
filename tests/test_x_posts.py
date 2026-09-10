from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from trade_news_analysis.config import DEFAULT_X_ACCOUNTS, Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import (
    Article,
    Event,
    EventArticle,
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
