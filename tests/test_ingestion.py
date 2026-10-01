from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import (
    Article,
    Event,
    EventArticle,
    IngestionRun,
    Security,
    SourceHealth,
)
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.ingestion import IngestionService
from trade_news_analysis.services.normalization import NormalizedArticle
from trade_news_analysis.services.providers import SecurityRecord
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


def test_source_flush_failure_rolls_back_and_later_sources_continue(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    first, second, _ = FakeSource().fetch().articles
    third = replace(first, url="https://example.com/independent", title="Independent story")

    class Source:
        markets = ("US",)
        coverage = "partial"

        def __init__(self, name: str, items: list[NormalizedArticle]):
            self.name, self.items = name, items

        def fetch(self) -> SourceResult:
            return SourceResult(self.name, self.items)

    class FailingIngestion(IngestionService):
        fail = True

        def _persist_article(
            self, session: Session, item: NormalizedArticle,
        ) -> tuple[bool, int | None]:
            if self.fail and item.url == second.url:
                session.add(Article(
                    fingerprint=first.fingerprint, canonical_url=first.url,
                    source=first.source, title=first.title, story_cluster_id="duplicate",
                ))
                session.flush()  # Real UNIQUE failure leaves Session needing rollback.
            return super()._persist_article(session, item)

    service = FailingIngestion(
        session_factory, settings, master_factory=no_master,
        source_factory=lambda _items, _config: [
            Source("broken", [first, second]), Source("working", [third]),
        ],
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)
    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None and run.status == "partial"
        assert run.articles_seen == run.articles_new == 1
        assert session.scalars(select(Article.canonical_url)).all() == [third.url]
        health = session.scalar(select(SourceHealth).where(SourceHealth.source == "broken"))
        assert health is not None and health.consecutive_failures == 1
    service.fail = False
    service.execute_run(service.create_run("retry"))
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Article)) == 3


def test_coordinator_owned_ingestion_does_not_mark_whole_run_complete(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    service = IngestionService(
        session_factory, settings, source_factory=fake_sources, master_factory=no_master,
    )
    run_id = service.create_run("test")
    service.execute_run(run_id, finalize=False)
    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None and run.status == "running"
        assert run.completed_at is None
        assert run.articles_new == 2


def test_same_url_material_revision_invalidates_once_without_duplicate_article(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    service = IngestionService(
        session_factory, settings, source_factory=fake_sources, master_factory=no_master,
    )
    original = FakeSource().fetch().articles[0]
    with session_factory() as session:
        _, event_id = service._persist_article(session, original)
        event = session.get(Event, event_id)
        assert event is not None
        event.status = "complete"
        session.commit()
        revised = replace(original, summary="The publisher corrected the disclosed amount.")
        assert service._persist_article(session, revised) == (False, None)
        assert event.status == "stale" and event.evidence_version == 1
        session.commit()
        assert service._persist_article(session, revised) == (False, None)
        assert event.evidence_version == 1
        assert session.scalar(select(func.count()).select_from(Article)) == 1
        assert session.scalar(select(Article.summary)) == revised.summary


class FakeSemanticMatcher:
    def __init__(self, scores: list[float] | None):
        self.scores = scores

    def similarities(self, _query: str, _candidates: Sequence[str]) -> list[float] | None:
        return self.scores

    def status(self) -> dict[str, object]:
        return {
            "enabled": True,
            "dependency_available": True,
            "model": "fake",
            "lexical_fallback": self.scores is None,
            "error": None,
        }


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


@pytest.mark.parametrize("status", ["complete", "partial", "error", "unavailable", "pending"])
def test_new_evidence_invalidates_finished_or_failed_events(
    session_factory: SessionFactory, settings: Settings, status: str,
) -> None:
    service = IngestionService(
        session_factory, settings, source_factory=fake_sources, master_factory=no_master,
    )
    first, second, _ = FakeSource().fetch().articles
    with session_factory() as session:
        created, event_id = service._persist_article(session, first)
        assert created and event_id is not None
        event = session.get(Event, event_id)
        assert event is not None
        event.status = status
        session.flush()

        created, queued_id = service._persist_article(session, second)

        assert created
        assert queued_id == (None if status == "pending" else event_id)
        assert event.status == ("pending" if status == "pending" else "stale")
        assert event.evidence_version == 1
        session.flush()
        assert service._persist_article(session, second) == (False, None)


def test_semantic_clustering_selects_best_cross_language_candidate(
    session_factory: SessionFactory, settings: Settings
) -> None:
    enabled_settings = settings.model_copy(update={"semantic_clustering_enabled": True})
    service = IngestionService(
        session_factory,
        enabled_settings,
        source_factory=lambda _securities, _settings: [],
        master_factory=no_master,
        semantic_matcher=FakeSemanticMatcher([0.84, 0.91]),
    )
    with session_factory() as session:
        session.add_all(
            [
                Article(
                    fingerprint="a" * 64,
                    canonical_url="https://example.com/preview",
                    source="Wire A",
                    title="NVIDIA previews a new accelerator",
                    published_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
                    story_cluster_id="preview-cluster",
                ),
                Article(
                    fingerprint="b" * 64,
                    canonical_url="https://example.com/launch",
                    source="Wire B",
                    title="NVIDIA launches the Blackwell accelerator",
                    published_at=datetime(2026, 8, 20, 11, tzinfo=UTC),
                    story_cluster_id="launch-cluster",
                ),
            ]
        )
        session.commit()
        cluster_id = service._cluster_id(
            session,
            NormalizedArticle(
                source="中文财经",
                title="英伟达正式发布 Blackwell 加速器",
                summary="",
                url="https://example.cn/blackwell",
                published_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
            ),
        )
    assert cluster_id == "launch-cluster"


def test_semantic_clustering_respects_threshold_and_lexical_fallback(
    session_factory: SessionFactory, settings: Settings
) -> None:
    enabled_settings = settings.model_copy(update={"semantic_clustering_enabled": True})
    with session_factory() as session:
        session.add(
            Article(
                fingerprint="c" * 64,
                canonical_url="https://example.com/existing",
                source="Wire",
                title="Apple expands its paid enterprise service",
                published_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
                story_cluster_id="existing-cluster",
            )
        )
        session.commit()
        below_threshold = IngestionService(
            session_factory,
            enabled_settings,
            source_factory=lambda _securities, _settings: [],
            master_factory=no_master,
            semantic_matcher=FakeSemanticMatcher([0.81]),
        )
        unrelated = NormalizedArticle(
            source="Wire",
            title="Microsoft opens a new research laboratory",
            summary="",
            url="https://example.com/research",
            published_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        )
        assert below_threshold._cluster_id(session, unrelated) != "existing-cluster"

        at_threshold = IngestionService(
            session_factory,
            enabled_settings,
            source_factory=lambda _securities, _settings: [],
            master_factory=no_master,
            semantic_matcher=FakeSemanticMatcher([0.82]),
        )
        assert at_threshold._cluster_id(session, unrelated) == "existing-cluster"

        lexical_fallback = IngestionService(
            session_factory,
            enabled_settings,
            source_factory=lambda _securities, _settings: [],
            master_factory=no_master,
            semantic_matcher=FakeSemanticMatcher(None),
        )
        duplicate = NormalizedArticle(
            source="Another Wire",
            title="Apple expands its paid enterprise service!",
            summary="",
            url="https://example.com/duplicate",
            published_at=datetime(2026, 8, 20, 13, tzinfo=UTC),
        )
        assert lexical_fallback._cluster_id(session, duplicate) == "existing-cluster"


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


def test_source_backoff_handles_sqlite_naive_timestamp(
    session_factory: SessionFactory, settings: Settings
) -> None:
    class BackedOffSource:
        name = "backed-off"
        markets: tuple[str, ...] = ("US",)
        coverage = "tracked"
        called = False

        def fetch(self) -> SourceResult:
            self.called = True
            raise AssertionError("backed-off source should not be fetched")

    source = BackedOffSource()
    with session_factory() as session:
        session.add(
            SourceHealth(
                source=source.name,
                consecutive_failures=3,
                items_last_run=0,
                last_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        session.commit()

    service = IngestionService(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [source],
        master_factory=no_master,
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)

    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "complete"
    assert source.called is False


def test_security_master_backoff_skips_recent_failing_provider(
    session_factory: SessionFactory, settings: Settings
) -> None:
    class BackedOffMaster:
        name = "backed-off-master"
        markets: tuple[str, ...] = ("A",)
        called = False

        def fetch_securities(self) -> list[SecurityRecord]:
            self.called = True
            raise AssertionError("backed-off provider should not be fetched")

    provider = BackedOffMaster()
    with session_factory() as session:
        session.add(
            SourceHealth(
                source=provider.name,
                capability="security_master",
                consecutive_failures=3,
                items_last_run=0,
                last_attempt_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        session.commit()

    service = IngestionService(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
        master_factory=lambda _settings: provider,
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)

    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "complete"
    assert provider.called is False


def test_coordinator_records_and_propagates_unhandled_pipeline_error(
    session_factory: SessionFactory, settings: Settings
) -> None:
    def broken_factory(_securities: list[Security], _settings: Settings) -> list[NewsSource]:
        raise TypeError("unexpected pipeline failure")

    coordinator = PipelineCoordinator(
        session_factory,
        settings.model_copy(update={"auto_analyze": False}),
        source_factory=broken_factory,
    )
    run_id = coordinator.submit_pipeline("test")
    with pytest.raises(TypeError, match="unexpected pipeline failure"):
        coordinator.wait_for_pipeline()
    coordinator.shutdown()

    with session_factory() as session:
        run = session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.completed_at is not None
        assert "TypeError" in run.errors[-1]
        assert "unexpected pipeline failure" not in run.errors[-1]


def test_ingestion_tracks_macro_research_assets(
    session_factory: SessionFactory, settings: Settings
) -> None:
    tracked_symbols: set[str] = set()

    def capture_sources(
        securities: list[Security], _settings: Settings
    ) -> list[NewsSource]:
        tracked_symbols.update(item.symbol for item in securities)
        return []

    service = IngestionService(
        session_factory,
        settings,
        source_factory=capture_sources,
        master_factory=no_master,
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)

    assert {"GLD", "GOVT"} <= tracked_symbols


def test_security_master_preserves_macro_asset_classification(
    session_factory: SessionFactory, settings: Settings
) -> None:
    class MacroAssetMaster:
        name: str = "macro-master"
        markets: tuple[str, ...] = ("US",)

        def fetch_securities(self) -> list[SecurityRecord]:
            return [
                SecurityRecord(
                    market="US",
                    exchange="US",
                    symbol="GLD",
                    name="SPDR Gold Shares",
                    aliases=["Gold"],
                    provider_data={"source": "test"},
                )
            ]

    service = IngestionService(
        session_factory,
        settings,
        source_factory=lambda _securities, _settings: [],
        master_factory=lambda _settings: MacroAssetMaster(),
    )
    run_id = service.create_run("test")
    service.execute_run(run_id)

    with session_factory() as session:
        gold_assets = session.scalars(
            select(Security).where(Security.market == "US", Security.symbol == "GLD")
        ).all()
        assert len(gold_assets) == 1
        assert gold_assets[0].industry == "黄金"
        assert gold_assets[0].provider_data["opportunity_group"] == "黄金"
        assert gold_assets[0].provider_data["source"] == "test"


def test_security_master_preserves_local_research_and_broker_configuration(
    session: Session,
) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    local = {
        "official_ir_url": "https://investor.example.com",
        "ibkr_con_id": 123456,
        "sec_cik": 320193,
        "cik": "0000320193",
    }
    security.provider_data = {**local, "source": "old-provider", "stale_provider_key": True}
    session.flush()

    updated = IngestionService._upsert_security(session, SecurityRecord(
        market=security.market, exchange=security.exchange, symbol=security.symbol,
        name="Updated company name", aliases=[],
        provider_data={"source": "new-provider", "ibkr_con_id": 999, "sec_cik": 999},
    ))

    assert updated.id == security.id
    assert updated.name == "Updated company name"
    assert updated.provider_data == {**local, "source": "new-provider"}
