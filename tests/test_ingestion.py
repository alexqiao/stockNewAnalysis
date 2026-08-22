from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Article, Event, EventArticle, IngestionRun, Security
from trade_news_analysis.services.ingestion import IngestionService
from trade_news_analysis.services.normalization import NormalizedArticle
from trade_news_analysis.services.sources import NewsSource, SourceResult


class FakeSource:
    name = "fake-feed"
    markets: tuple[str, ...] = ("A", "HK", "US")
    coverage = "broad"

    def fetch(self) -> SourceResult:
        first = NormalizedArticle(
            source="Example Wire",
            title="朱雀三号成功完成火箭回收",
            summary="火箭完成返回和回收验证。",
            url="https://example.com/story-one",
            published_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        )
        second = NormalizedArticle(
            source="Another Wire",
            title="朱雀三号成功完成火箭回收！",
            summary="另一家媒体确认同一回收事件。",
            url="https://example.com/story-two",
            published_at=datetime(2026, 8, 20, 13, tzinfo=UTC),
        )
        return SourceResult(source=self.name, articles=[first, second, first])


def fake_sources(_securities: list[Security], _settings: Settings) -> list[NewsSource]:
    return [FakeSource()]


def no_master(_settings: Settings) -> None:
    return None


def test_ingestion_clusters_duplicate_reports_into_one_event(
    session_factory: SessionFactory, settings: Settings
) -> None:
    service = IngestionService(
        session_factory,
        settings,
        source_factory=fake_sources,
        master_factory=no_master,
    )
    first_run = service.create_run("test")
    service.execute_run(first_run)
    second_run = service.create_run("test")
    service.execute_run(second_run)

    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 2
        assert session.scalar(select(func.count()).select_from(Event)) == 1
        assert session.scalar(select(func.count()).select_from(EventArticle)) == 2
        event = session.scalar(select(Event))
        assert event is not None
        assert event.status == "pending"
        run = session.get(IngestionRun, second_run)
        assert run is not None
        assert run.articles_seen == 3
        assert run.articles_new == 0


def test_source_failure_is_recorded_without_crashing_run(
    session_factory: SessionFactory, settings: Settings
) -> None:
    class BrokenSource:
        name = "broken"
        markets: tuple[str, ...] = ("A",)
        coverage = "partial"

        def fetch(self) -> SourceResult:
            raise TimeoutError("slow upstream")

    def broken_sources(_securities: list[Security], _settings: Settings) -> list[NewsSource]:
        return [BrokenSource()]

    service = IngestionService(
        session_factory,
        settings,
        source_factory=broken_sources,
        master_factory=no_master,
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)
    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "partial"
        assert "TimeoutError" in run.errors[0]
