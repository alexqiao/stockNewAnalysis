"""Idempotent news persistence and per-company analysis creation."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import SessionFactory
from ..models import (
    Article,
    ArticleSymbol,
    ImpactAnalysis,
    IngestionRun,
    SourceHealth,
    Watchlist,
    utc_now,
)
from .normalization import NormalizedArticle, ensure_aware, match_watchlist, title_similarity
from .sources import NewsSource, build_default_sources

SourceFactory = Callable[[list[Watchlist], Settings], list[NewsSource]]


class IngestionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        source_factory: SourceFactory = build_default_sources,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.source_factory = source_factory

    def create_run(self, trigger: str) -> int:
        with self.session_factory() as session:
            run = IngestionRun(trigger=trigger, status="queued")
            session.add(run)
            session.commit()
            return run.id

    def _cluster_id(self, session: Session, item: NormalizedArticle) -> str:
        published = item.published_at or utc_now()
        nearby = session.scalars(
            select(Article).where(
                Article.published_at >= ensure_aware(published) - timedelta(hours=48),
                Article.published_at <= ensure_aware(published) + timedelta(hours=48),
            )
        ).all()
        for candidate in nearby:
            if title_similarity(candidate.title, item.title) >= 0.85:
                return candidate.story_cluster_id
        return hashlib.sha256(item.title.casefold().encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _source_health(session: Session, source: str) -> SourceHealth:
        health = session.scalar(select(SourceHealth).where(SourceHealth.source == source))
        if not health:
            health = SourceHealth(
                source=source,
                consecutive_failures=0,
                items_last_run=0,
            )
            session.add(health)
        return health

    def _persist_article(
        self, session: Session, item: NormalizedArticle, watchlist: list[Watchlist]
    ) -> tuple[bool, int]:
        article = session.scalar(select(Article).where(Article.fingerprint == item.fingerprint))
        created = article is None
        if article is None:
            article = Article(
                fingerprint=item.fingerprint,
                canonical_url=item.url,
                source=item.source,
                title=item.title,
                summary=item.summary,
                published_at=item.published_at,
                story_cluster_id=self._cluster_id(session, item),
                raw_data=item.raw_data,
            )
            session.add(article)
            session.flush()

        created_analyses = 0
        matches = match_watchlist(item, watchlist)
        for symbol, (method, in_title) in matches.items():
            relation = session.scalar(
                select(ArticleSymbol).where(
                    ArticleSymbol.article_id == article.id, ArticleSymbol.symbol == symbol
                )
            )
            if relation is None:
                relation = ArticleSymbol(
                    article_id=article.id,
                    symbol=symbol,
                    match_method=method,
                    in_title=in_title,
                )
                session.add(relation)
                session.flush()
            current = session.scalar(
                select(ImpactAnalysis).where(
                    ImpactAnalysis.article_symbol_id == relation.id,
                    ImpactAnalysis.is_current.is_(True),
                )
            )
            if current is None:
                session.add(
                    ImpactAnalysis(
                        article_symbol_id=relation.id,
                        status="pending",
                        model=self.settings.llm_model,
                    )
                )
                created_analyses += 1
        return created, created_analyses

    def execute_run(self, run_id: int) -> None:
        with self.session_factory() as session:
            run = session.get(IngestionRun, run_id)
            if run is None:
                return
            run.status = "running"
            run.started_at = utc_now()
            session.commit()
            watchlist = session.scalars(
                select(Watchlist).where(Watchlist.active.is_(True)).order_by(Watchlist.symbol)
            ).all()
            errors: list[str] = []
            seen = new = analyses = 0
            for source in self.source_factory(list(watchlist), self.settings):
                health = self._source_health(session, source.name)
                health.last_attempt_at = utc_now()
                try:
                    result = source.fetch()
                    health.last_success_at = utc_now()
                    health.last_error = None
                    health.consecutive_failures = 0
                    health.items_last_run = len(result.articles)
                    for item in result.articles:
                        seen += 1
                        was_new, analysis_count = self._persist_article(
                            session, item, list(watchlist)
                        )
                        new += int(was_new)
                        analyses += analysis_count
                    session.commit()
                except Exception as exc:
                    message = f"{source.name}: {type(exc).__name__}: {exc}"[:1000]
                    errors.append(message)
                    health.last_error = message
                    health.consecutive_failures = (health.consecutive_failures or 0) + 1
                    health.items_last_run = 0
                    session.commit()
            run.articles_seen = seen
            run.articles_new = new
            run.analyses_created = analyses
            run.errors = errors
            run.status = "partial" if errors else "complete"
            run.completed_at = utc_now()
            session.commit()
