from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import (
    Article,
    ArticleSymbol,
    ImpactAnalysis,
    IngestionRun,
    Watchlist,
)
from trade_news_analysis.services.ingestion import IngestionService
from trade_news_analysis.services.normalization import NormalizedArticle
from trade_news_analysis.services.sources import NewsSource, SourceResult


class FakeSource:
    name = "fake-feed"

    def fetch(self) -> SourceResult:
        article = NormalizedArticle(
            source="Example Wire",
            title="Apple and Microsoft launch paid enterprise service",
            summary="Customers have started paying for the joint service.",
            url="https://example.com/story?utm_source=test",
            published_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
            hinted_symbols={"AAPL"},
        )
        return SourceResult(source=self.name, articles=[article, article])


def fake_sources(_watchlist: list[Watchlist], _settings: Settings) -> list[NewsSource]:
    return [FakeSource()]


def test_ingestion_is_idempotent_and_splits_companies(
    session_factory: SessionFactory, settings: Settings
) -> None:
    service = IngestionService(session_factory, settings, source_factory=fake_sources)
    first_run = service.create_run("test")
    service.execute_run(first_run)
    second_run = service.create_run("test")
    service.execute_run(second_run)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 1
        assert session.scalar(select(func.count()).select_from(ArticleSymbol)) == 2
        assert session.scalar(select(func.count()).select_from(ImpactAnalysis)) == 2
        symbols = set(session.scalars(select(ArticleSymbol.symbol)).all())
        assert symbols == {"AAPL", "MSFT"}
        run = session.get(IngestionRun, second_run)
        assert run is not None
        assert run.articles_seen == 2
        assert run.articles_new == 0


def test_source_failure_is_recorded_without_crashing_run(
    session_factory: SessionFactory, settings: Settings
) -> None:
    class BrokenSource:
        name = "broken"

        def fetch(self) -> SourceResult:
            raise TimeoutError("slow upstream")

    def broken_sources(_watchlist: list[Watchlist], _settings: Settings) -> list[NewsSource]:
        return [BrokenSource()]

    service = IngestionService(session_factory, settings, source_factory=broken_sources)
    run_id = service.create_run("test")
    service.execute_run(run_id)
    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "partial"
        assert "TimeoutError" in run.errors[0]
