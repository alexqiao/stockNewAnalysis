from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Article, Event, EventArticle, EventSecurityImpact, Security

from . import test_decisions_api as api_fixtures

decisions_client = api_fixtures.decisions_client


def test_security_exact_match_precedes_partial_matches_before_limit(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        session.add_all(Security(
            market="US", exchange="NASDAQ", symbol=f"AAA{index}", name=f"Target supplier {index}",
        ) for index in range(30))
        exact = Security(market="US", exchange="NASDAQ", symbol="TARGET", name="Exact company")
        session.add(exact)
        session.commit()
        identifier = exact.id
    result = decisions_client.get("/api/v1/securities?q=target&limit=1")
    assert result.status_code == 200
    assert [row["id"] for row in result.json()] == [identifier]
    result = decisions_client.get("/api/v1/securities?q=Exact%20company&limit=1")
    assert [row["id"] for row in result.json()] == [identifier]


@pytest.mark.parametrize("filters", [
    "symbol=aapl", "direction=bullish", "source=target", "symbol=AAPL&direction=bullish",
])
def test_news_filters_run_before_limit_even_with_many_newer_unrelated_articles(
    decisions_client: TestClient, session_factory: SessionFactory, filters: str,
) -> None:
    now = datetime.now(UTC)
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        assert security_id is not None
        old = Article(
            fingerprint="filter-target", story_cluster_id="filter-target", source="Target Wire",
            title="Company order", canonical_url="https://example.test/target",
            published_at=now - timedelta(days=30),
        )
        evidence = Event(event_key="filter-target", title=old.title, status="complete")
        session.add_all([old, evidence])
        session.flush()
        session.add_all([
            EventArticle(article_id=old.id, event_id=evidence.id),
            EventSecurityImpact(
                security_id=security_id, event_id=evidence.id, status="complete", is_current=True,
                impacts={"5": {"direction": "bullish"}},
            ),
        ])
        session.add_all(Article(
            fingerprint=f"filter-noise-{index}", story_cluster_id=f"filter-noise-{index}",
            source="Unrelated Wire", title="Other article", published_at=now,
            canonical_url=f"https://example.test/noise/{index}",
        ) for index in range(210))
        session.commit()
        identifier = old.id
    response = decisions_client.get(f"/api/v1/news?{filters}&limit=1")
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [identifier]


def test_news_direction_filter_does_not_reuse_superseded_impacts(
    decisions_client: TestClient, session_factory: SessionFactory,
) -> None:
    with session_factory() as session:
        security_id = session.scalar(select(Security.id).where(Security.symbol == "AAPL"))
        assert security_id is not None
        article = Article(
            fingerprint="superseded", story_cluster_id="superseded", source="Wire", title="Order",
            canonical_url="https://example.test/old", published_at=datetime.now(UTC),
        )
        evidence = Event(event_key="superseded", title=article.title, status="complete")
        session.add_all([article, evidence])
        session.flush()
        session.add_all([
            EventArticle(article_id=article.id, event_id=evidence.id),
            EventSecurityImpact(
                security_id=security_id, event_id=evidence.id, is_current=False,
                impacts={"5": {"direction": "bullish"}},
            ),
            EventSecurityImpact(
                security_id=security_id, event_id=evidence.id, is_current=True,
                impacts={"5": {"direction": "bearish"}},
            ),
        ])
        session.commit()
    assert decisions_client.get("/api/v1/news?direction=bullish").json() == []
    assert len(decisions_client.get("/api/v1/news?direction=bearish").json()) == 1
