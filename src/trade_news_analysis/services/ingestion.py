"""Idempotent raw-news persistence and event clustering."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import OPPORTUNITY_ASSET_SYMBOLS, Settings
from ..db import SessionFactory
from ..models import (
    Article,
    Event,
    EventArticle,
    IngestionRun,
    Security,
    SourceHealth,
    Watchlist,
    utc_now,
)
from .normalization import NormalizedArticle, ensure_aware, title_similarity
from .providers import SecurityMasterProvider, SecurityRecord, build_security_master_provider
from .sources import NewsSource, build_default_sources

SourceFactory = Callable[[list[Security], Settings], list[NewsSource]]
MasterFactory = Callable[[Settings], SecurityMasterProvider | None]


class IngestionService:
    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings,
        source_factory: SourceFactory = build_default_sources,
        master_factory: MasterFactory = build_security_master_provider,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.source_factory = source_factory
        self.master_factory = master_factory

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
                Article.published_at >= ensure_aware(published) - timedelta(hours=72),
                Article.published_at <= ensure_aware(published) + timedelta(hours=72),
            )
        ).all()
        for candidate in nearby:
            if title_similarity(candidate.title, item.title) >= 0.72:
                return candidate.story_cluster_id
        return hashlib.sha256(item.title.casefold().encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _source_health(
        session: Session,
        source: str,
        capability: str = "news",
        markets: list[str] | None = None,
        coverage: str = "partial",
    ) -> SourceHealth:
        health = session.scalar(select(SourceHealth).where(SourceHealth.source == source))
        if not health:
            health = SourceHealth(
                source=source,
                consecutive_failures=0,
                items_last_run=0,
                markets=[],
            )
            session.add(health)
        health.capability = capability
        health.markets = markets or []
        health.coverage = coverage
        return health

    @staticmethod
    def _upsert_security(session: Session, record: SecurityRecord) -> Security:
        security = session.scalar(
            select(Security).where(
                Security.market == record.market,
                Security.exchange == record.exchange,
                Security.symbol == record.symbol,
            )
        )
        if security is None:
            symbol_matches = session.scalars(
                select(Security).where(
                    Security.market == record.market,
                    Security.symbol == record.symbol,
                )
            ).all()
            security = symbol_matches[0] if len(symbol_matches) == 1 else None
        if security is None:
            security = Security(
                market=record.market,
                exchange=record.exchange,
                symbol=record.symbol,
                name=record.name,
            )
            session.add(security)
        security.name = record.name
        security.aliases = sorted(set([*(security.aliases or []), *record.aliases]))
        existing_provider_data = security.provider_data or {}
        is_research_asset = bool(existing_provider_data.get("research_asset"))
        security.industry = (
            security.industry if is_research_asset else record.industry or security.industry
        )
        security.business_summary = record.business_summary
        security.market_cap = record.market_cap
        security.currency = record.currency
        security.timezone = record.timezone
        security.calendar = record.calendar
        research_metadata = {
            key: existing_provider_data[key]
            for key in ("research_asset", "opportunity_group", "opportunity_scope")
            if key in existing_provider_data
        }
        security.provider_data = {**(record.provider_data or {}), **research_metadata}
        security.updated_at = utc_now()
        return security

    def _sync_security_master(self, session: Session, errors: list[str]) -> None:
        provider = self.master_factory(self.settings)
        if provider is None:
            return
        health = self._source_health(
            session, provider.name, "security_master", list(provider.markets), "broad"
        )
        health.last_attempt_at = utc_now()
        try:
            records = provider.fetch_securities()
            for record in records:
                self._upsert_security(session, record)
            health.last_success_at = utc_now()
            health.last_error = None
            health.consecutive_failures = 0
            health.items_last_run = len(records)
        except Exception as exc:
            message = f"{provider.name}: {type(exc).__name__}: {exc}"[:1000]
            errors.append(message)
            health.last_error = message
            health.consecutive_failures = (health.consecutive_failures or 0) + 1
            health.items_last_run = 0
        session.commit()

    def _persist_article(
        self, session: Session, item: NormalizedArticle
    ) -> tuple[bool, int | None]:
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
        event = session.scalar(
            select(Event).where(Event.event_key == article.story_cluster_id)
        )
        event_created = event is None
        if event is None:
            event = Event(
                event_key=article.story_cluster_id,
                status="pending",
                title=article.title,
                summary=article.summary,
                occurred_at=article.published_at,
            )
            session.add(event)
            session.flush()
        link = session.scalar(
            select(EventArticle).where(
                EventArticle.event_id == event.id, EventArticle.article_id == article.id
            )
        )
        if link is None:
            session.add(EventArticle(event_id=event.id, article_id=article.id))
            should_queue = not event_created and event.status == "complete"
            if should_queue:
                event.status = "pending"
        else:
            should_queue = False
        return created, event.id if event_created or should_queue else None

    def execute_run(self, run_id: int) -> None:
        with self.session_factory() as session:
            run = session.get(IngestionRun, run_id)
            if run is None:
                return
            run.status = "running"
            run.started_at = utc_now()
            session.commit()
            errors: list[str] = []
            self._sync_security_master(session, errors)
            tracked = session.scalars(
                select(Security)
                .join(Watchlist, Watchlist.security_id == Security.id)
                .where(Watchlist.active.is_(True), Security.active.is_(True))
                .order_by(Security.market, Security.symbol)
            ).all()
            research_assets = session.scalars(
                select(Security).where(
                    Security.active.is_(True),
                    Security.market == "US",
                    Security.symbol.in_(OPPORTUNITY_ASSET_SYMBOLS),
                )
            ).all()
            tracked = list({item.id: item for item in [*tracked, *research_assets]}.values())
            seen = new = 0
            queued_events: set[int] = set()
            for source in self.source_factory(list(tracked), self.settings):
                health = self._source_health(
                    session,
                    source.name,
                    "news",
                    list(getattr(source, "markets", ())),
                    getattr(source, "coverage", "partial"),
                )
                health.last_attempt_at = utc_now()
                try:
                    result = source.fetch()
                    health.last_success_at = utc_now()
                    health.last_error = None
                    health.consecutive_failures = 0
                    health.items_last_run = len(result.articles)
                    for item in result.articles:
                        seen += 1
                        was_new, queued_event_id = self._persist_article(session, item)
                        new += int(was_new)
                        if queued_event_id is not None:
                            queued_events.add(queued_event_id)
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
            run.analyses_created = len(queued_events)
            run.errors = errors
            run.status = "partial" if errors else "complete"
            run.completed_at = utc_now()
            session.commit()
