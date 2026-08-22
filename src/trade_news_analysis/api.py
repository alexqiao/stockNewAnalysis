"""Stock-first JSON API and server-rendered research pages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Article,
    Event,
    EventArticle,
    EventSecurityImpact,
    EventTheme,
    IngestionRun,
    Security,
    SecuritySignalSnapshot,
    SourceHealth,
    Theme,
    Watchlist,
)
from .schemas import RunResponse, WatchlistReplace
from .services.coordinator import PipelineBusyError, PipelineCoordinator
from .services.metrics import build_metrics

api_router = APIRouter(prefix="/api/v1")
web_router = APIRouter()
VALID_HORIZONS = {1, 5, 20}
VALID_MARKETS = {"A", "HK", "US"}


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _coordinator(request: Request) -> PipelineCoordinator:
    return request.app.state.coordinator


def _security_brief(security: Security) -> dict[str, Any]:
    return {
        "id": security.id,
        "market": security.market,
        "exchange": security.exchange,
        "symbol": security.symbol,
        "name": security.name,
        "industry": security.industry,
        "market_cap": security.market_cap,
        "currency": security.currency,
    }


def _impact_dict(impact: EventSecurityImpact) -> dict[str, Any]:
    return {
        "id": impact.id,
        "event_id": impact.event_id,
        "security": _security_brief(impact.security),
        "status": impact.status,
        "chain_level": impact.chain_level,
        "impacts": impact.impacts,
        "opportunity_score": impact.opportunity_score,
        "dimensions": {
            "demand_certainty": impact.demand_certainty,
            "transmission_clarity": impact.transmission_clarity,
            "business_purity": impact.business_purity,
            "scale_elasticity": impact.scale_elasticity,
            "market_neglect": impact.market_neglect,
            "novelty_unpriced": impact.novelty_unpriced,
            "evidence_quality": impact.evidence_quality,
            "verification_speed": impact.verification_speed,
            "risk_penalty": impact.risk_penalty,
        },
        "financial_channels": impact.financial_channels,
        "thesis": impact.thesis,
        "catalysts": impact.catalysts,
        "risks": impact.risks,
        "falsifiers": impact.falsifiers,
        "evidence": impact.evidence,
        "error": impact.error,
    }


def _event_query() -> Any:
    return select(Event).options(
        selectinload(Event.article_links).selectinload(EventArticle.article),
        selectinload(Event.theme_links).selectinload(EventTheme.theme),
        selectinload(Event.impacts).selectinload(EventSecurityImpact.security),
    )


def _event_dict(event: Event) -> dict[str, Any]:
    impacts = [item for item in event.impacts if item.is_current]
    return {
        "id": event.id,
        "event_key": event.event_key,
        "status": event.status,
        "title": event.title,
        "summary": event.summary,
        "event_type": event.event_type,
        "observed_demand": event.observed_demand,
        "occurred_at": event.occurred_at,
        "updated_at": event.updated_at,
        "themes": [
            {"id": link.theme.id, "slug": link.theme.slug, "name": link.theme.name}
            for link in event.theme_links
        ],
        "articles": [
            {
                "id": link.article.id,
                "source": link.article.source,
                "title": link.article.title,
                "url": link.article.canonical_url,
                "published_at": link.article.published_at,
            }
            for link in event.article_links
        ],
        "impacts": sorted(
            (_impact_dict(item) for item in impacts),
            key=lambda item: item["opportunity_score"],
            reverse=True,
        ),
        "unresolved_candidates": event.unresolved_candidates,
        "error": event.error,
    }


def _article_dict(article: Article) -> dict[str, Any]:
    return {
        "id": article.id,
        "source": article.source,
        "title": article.title,
        "summary": article.summary,
        "url": article.canonical_url,
        "published_at": article.published_at,
        "fetched_at": article.fetched_at,
        "events": [
            {
                "id": link.event.id,
                "title": link.event.title,
                "status": link.event.status,
                "themes": [item.theme.name for item in link.event.theme_links],
                "securities": [
                    _security_brief(impact.security)
                    for impact in link.event.impacts
                    if impact.is_current
                ],
            }
            for link in article.event_links
        ],
    }


def _latest_snapshot_map(
    session: Session, horizon: int, security_ids: set[int] | None = None
) -> dict[int, SecuritySignalSnapshot]:
    query = (
        select(SecuritySignalSnapshot)
        .where(SecuritySignalSnapshot.horizon == horizon)
        .order_by(SecuritySignalSnapshot.as_of.desc(), SecuritySignalSnapshot.id.desc())
    )
    if security_ids is not None:
        if not security_ids:
            return {}
        query = query.where(SecuritySignalSnapshot.security_id.in_(security_ids))
    result: dict[int, SecuritySignalSnapshot] = {}
    for snapshot in session.scalars(query):
        result.setdefault(snapshot.security_id, snapshot)
    return result


def _signal_dict(snapshot: SecuritySignalSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "as_of": snapshot.as_of,
        "horizon": snapshot.horizon,
        "score": snapshot.score,
        "direction": snapshot.direction,
        "confidence": snapshot.confidence,
        "conflict": snapshot.conflict,
        "rank": snapshot.rank,
        "evidence_event_ids": snapshot.evidence_event_ids,
        "components": snapshot.components,
    }


@api_router.post("/runs/ingest", response_model=RunResponse, status_code=status.HTTP_202_ACCEPTED)
def start_ingestion(request: Request) -> IngestionRun:
    try:
        run_id = _coordinator(request).submit_pipeline("manual")
    except PipelineBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    with _session(request) as session:
        run = session.get(IngestionRun, run_id)
        if not run:
            raise HTTPException(status_code=500, detail="任务创建失败")
        return run


@api_router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: int, request: Request) -> IngestionRun:
    with _session(request) as session:
        run = session.get(IngestionRun, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        return run


@api_router.get("/opportunities")
def list_opportunities(
    request: Request,
    market: str | None = Query(default=None, pattern="^(A|HK|US)$"),
    theme: str | None = None,
    horizon: int = Query(default=5),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    if horizon not in VALID_HORIZONS:
        raise HTTPException(status_code=422, detail="horizon 只能是 1、5 或 20")
    with _session(request) as session:
        allowed_ids: set[int] | None = None
        if theme:
            event_ids = set(
                session.scalars(
                    select(EventTheme.event_id)
                    .join(Theme)
                    .where((Theme.slug == theme) | (Theme.name == theme))
                )
            )
            allowed_ids = set(
                session.scalars(
                    select(EventSecurityImpact.security_id).where(
                        EventSecurityImpact.event_id.in_(event_ids),
                        EventSecurityImpact.is_current.is_(True),
                    )
                )
            ) if event_ids else set()
        snapshots = _latest_snapshot_map(session, horizon, allowed_ids)
        query = select(Security).where(Security.id.in_(snapshots))
        if market:
            query = query.where(Security.market == market)
        securities = {item.id: item for item in session.scalars(query)}
        rows = []
        for security_id, snapshot in snapshots.items():
            if security_id not in securities or snapshot.score <= 0:
                continue
            signal = _signal_dict(snapshot)
            if signal is not None:
                rows.append(
                    {"security": _security_brief(securities[security_id]), "signal": signal}
                )
        rows.sort(key=lambda item: item["signal"]["score"], reverse=True)
        return rows[:limit]


@api_router.get("/securities")
def list_securities(
    request: Request,
    market: str | None = Query(default=None, pattern="^(A|HK|US)$"),
    q: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    with _session(request) as session:
        query = select(Security).where(Security.active.is_(True))
        if market:
            query = query.where(Security.market == market)
        if q:
            query = query.where(
                Security.name.ilike(f"%{q}%") | Security.symbol.ilike(f"%{q}%")
            )
        return [
            _security_brief(item)
            for item in session.scalars(
                query.order_by(Security.market, Security.symbol).limit(limit)
            )
        ]


@api_router.get("/securities/{security_id}")
def get_security(security_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        security = session.scalar(
            select(Security)
            .where(Security.id == security_id)
            .options(
                selectinload(Security.impacts).selectinload(EventSecurityImpact.event),
                selectinload(Security.snapshots),
            )
        )
        if security is None:
            raise HTTPException(status_code=404, detail="证券不存在")
        latest = {
            horizon: _latest_snapshot_map(session, horizon, {security.id}).get(security.id)
            for horizon in VALID_HORIZONS
        }
        impacts = [item for item in security.impacts if item.is_current]
        impacts.sort(key=lambda item: item.event.occurred_at or item.created_at, reverse=True)
        return {
            **_security_brief(security),
            "aliases": security.aliases,
            "business_summary": security.business_summary,
            "timezone": security.timezone,
            "calendar": security.calendar,
            "signals": {str(key): _signal_dict(value) for key, value in latest.items()},
            "impacts": [
                {**_impact_dict(item), "event_title": item.event.title}
                for item in impacts
            ],
        }


@api_router.get("/themes")
def list_themes(request: Request) -> list[dict[str, Any]]:
    with _session(request) as session:
        themes = session.scalars(
            select(Theme).options(selectinload(Theme.event_links)).order_by(Theme.name)
        ).all()
        return [
            {
                "id": item.id,
                "slug": item.slug,
                "name": item.name,
                "description": item.description,
                "event_count": len(item.event_links),
            }
            for item in themes
        ]


@api_router.get("/themes/{theme_id}")
def get_theme(theme_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        theme = session.scalar(
            select(Theme)
            .where(Theme.id == theme_id)
            .options(
                selectinload(Theme.event_links)
                .selectinload(EventTheme.event)
                .selectinload(Event.impacts)
                .selectinload(EventSecurityImpact.security)
            )
        )
        if theme is None:
            raise HTTPException(status_code=404, detail="主题不存在")
        events = [link.event for link in theme.event_links]
        impacts = [
            impact
            for event in events
            for impact in event.impacts
            if impact.is_current and impact.status == "complete"
        ]
        impacts.sort(key=lambda item: item.opportunity_score, reverse=True)
        return {
            "id": theme.id,
            "slug": theme.slug,
            "name": theme.name,
            "description": theme.description,
            "events": [
                {"id": item.id, "title": item.title, "occurred_at": item.occurred_at}
                for item in sorted(
                    events,
                    key=lambda value: value.occurred_at or value.created_at,
                    reverse=True,
                )
            ],
            "candidates": [_impact_dict(item) for item in impacts],
        }


@api_router.get("/events/{event_id}")
def get_event(event_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        event = session.scalar(_event_query().where(Event.id == event_id))
        if event is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        return _event_dict(event)


@api_router.post("/events/{event_id}/analyses", status_code=status.HTTP_202_ACCEPTED)
def reanalyze_event(event_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="事件不存在")
        event.status = "pending"
        event.error = None
        session.commit()
    _coordinator(request).submit_analysis(event_id)
    return {"event_id": event_id, "status": "queued"}


@api_router.get("/news")
def list_news(
    request: Request,
    symbol: str | None = None,
    source: str | None = None,
    direction: str | None = Query(default=None, pattern="^(bullish|neutral|bearish)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    with _session(request) as session:
        query = (
            select(Article)
            .order_by(Article.published_at.desc(), Article.id.desc())
            .options(
                selectinload(Article.event_links)
                .selectinload(EventArticle.event)
                .selectinload(Event.theme_links)
                .selectinload(EventTheme.theme),
                selectinload(Article.event_links)
                .selectinload(EventArticle.event)
                .selectinload(Event.impacts)
                .selectinload(EventSecurityImpact.security),
            )
        )
        if source:
            query = query.where(Article.source.ilike(f"%{source}%"))
        articles = session.scalars(query.limit(limit * 5)).unique().all()
        result = [_article_dict(item) for item in articles]
        if symbol:
            expected = symbol.upper()
            result = [
                item
                for item in result
                if any(
                    security["symbol"].upper() == expected
                    for event in item["events"]
                    for security in event["securities"]
                )
            ]
        if direction:
            article_ids = {
                link.article_id
                for impact in session.scalars(
                    select(EventSecurityImpact).where(EventSecurityImpact.is_current.is_(True))
                )
                if any(
                    value.get("direction") == direction for value in impact.impacts.values()
                )
                for link in impact.event.article_links
            }
            result = [item for item in result if item["id"] in article_ids]
        return result[:limit]


@api_router.get("/news/{article_id}")
def get_news(article_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        article = session.scalar(
            select(Article)
            .where(Article.id == article_id)
            .options(
                selectinload(Article.event_links)
                .selectinload(EventArticle.event)
                .selectinload(Event.theme_links)
                .selectinload(EventTheme.theme),
                selectinload(Article.event_links)
                .selectinload(EventArticle.event)
                .selectinload(Event.impacts)
                .selectinload(EventSecurityImpact.security),
            )
        )
        if article is None:
            raise HTTPException(status_code=404, detail="新闻不存在")
        return _article_dict(article)


@api_router.get("/metrics")
def metrics(
    request: Request,
    security_id: int | None = None,
    market: str | None = Query(default=None, pattern="^(A|HK|US)$"),
    horizon: int | None = None,
    since: datetime | None = None,
    top_k: int = Query(default=10, ge=1, le=100),
) -> dict[str, Any]:
    if horizon is not None and horizon not in VALID_HORIZONS:
        raise HTTPException(status_code=422, detail="horizon 只能是 1、5 或 20")
    with _session(request) as session:
        return build_metrics(
            session,
            security_id=security_id,
            market=market,
            horizon=horizon,
            since=since,
            top_k=top_k,
        )


@api_router.get("/watchlist")
def get_watchlist(request: Request) -> list[dict[str, Any]]:
    with _session(request) as session:
        items = session.scalars(
            select(Watchlist)
            .options(selectinload(Watchlist.security))
            .order_by(Watchlist.created_at)
        ).all()
        return [
            {
                "security_id": item.security_id,
                "active": item.active,
                "security": _security_brief(item.security),
            }
            for item in items
        ]


@api_router.put("/watchlist")
def replace_watchlist(payload: WatchlistReplace, request: Request) -> list[dict[str, Any]]:
    security_ids = [item.security_id for item in payload.items]
    if len(security_ids) != len(set(security_ids)):
        raise HTTPException(status_code=422, detail="自选证券不能重复")
    with _session(request) as session:
        existing = set(
            session.scalars(select(Security.id).where(Security.id.in_(security_ids)))
        )
        if existing != set(security_ids):
            raise HTTPException(status_code=422, detail="包含不存在的 security_id")
        session.execute(delete(Watchlist))
        session.add_all([Watchlist(**item.model_dump()) for item in payload.items])
        session.commit()
    return get_watchlist(request)


@api_router.get("/health")
def health(request: Request) -> dict[str, Any]:
    with _session(request) as session:
        sources = session.scalars(select(SourceHealth).order_by(SourceHealth.source)).all()
        latest_run = session.scalar(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
        broad_markets = {
            market
            for item in sources
            if item.capability == "news" and item.coverage == "broad" and item.last_success_at
            for market in item.markets
        }
        return {
            "status": "ok" if broad_markets == VALID_MARKETS else "degraded",
            "coverage_warning": (
                None
                if broad_markets == VALID_MARKETS
                else "新闻源覆盖不完整；未采集到新闻不代表没有市场影响。"
            ),
            "market_coverage": {
                market: ("broad" if market in broad_markets else "partial")
                for market in sorted(VALID_MARKETS)
            },
            "llm_configured": request.app.state.settings.llm_configured,
            "tushare_configured": request.app.state.settings.tushare_configured,
            "scheduler_enabled": request.app.state.settings.scheduler_enabled,
            "pipeline_busy": _coordinator(request).busy,
            "latest_run": (
                RunResponse.model_validate(latest_run).model_dump() if latest_run else None
            ),
            "sources": [
                {
                    "source": item.source,
                    "capability": item.capability,
                    "markets": item.markets,
                    "coverage": item.coverage,
                    "last_attempt_at": item.last_attempt_at,
                    "last_success_at": item.last_success_at,
                    "last_error": item.last_error,
                    "consecutive_failures": item.consecutive_failures,
                    "items_last_run": item.items_last_run,
                }
                for item in sources
            ],
        }


@web_router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    market: str | None = None,
    theme: str | None = None,
    horizon: int = 5,
) -> HTMLResponse:
    opportunities = list_opportunities(request, market, theme, horizon, 50)
    watchlist = get_watchlist(request)
    themes = list_themes(request)
    coverage = health(request)
    with _session(request) as session:
        ids = {item["security_id"] for item in watchlist}
        snapshots = _latest_snapshot_map(session, horizon, ids)
        watchlist_rows = [
            {**item, "signal": _signal_dict(snapshots.get(item["security_id"]))}
            for item in watchlist
        ]
        events = session.scalars(
            _event_query().order_by(Event.occurred_at.desc(), Event.id.desc()).limit(12)
        ).unique().all()
        recent_events = [_event_dict(item) for item in events]
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "opportunities": opportunities,
            "watchlist": watchlist_rows,
            "themes": themes,
            "events": recent_events,
            "coverage": coverage,
            "selected_market": market,
            "selected_theme": theme,
            "horizon": horizon,
        },
    )


@web_router.get("/securities/{security_id}", response_class=HTMLResponse)
def security_page(security_id: int, request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="security.html",
        context={"security": get_security(security_id, request)},
    )


@web_router.get("/themes/{theme_id}", response_class=HTMLResponse)
def theme_page(theme_id: int, request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="theme.html", context={"theme": get_theme(theme_id, request)}
    )


@web_router.get("/events/{event_id}", response_class=HTMLResponse)
def event_page(event_id: int, request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="event.html", context={"event": get_event(event_id, request)}
    )


@web_router.get("/news/{article_id}", response_class=HTMLResponse)
def news_detail(article_id: int, request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="detail.html", context={"article": get_news(article_id, request)}
    )


@web_router.get("/metrics", response_class=HTMLResponse)
def metrics_page(
    request: Request, market: str | None = None, horizon: int | None = None
) -> HTMLResponse:
    data = metrics(request, market=market, horizon=horizon, top_k=10)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="metrics.html",
        context={"metrics": data, "market": market, "horizon": horizon},
    )


@web_router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="watchlist.html",
        context={
            "items": get_watchlist(request),
            "securities": list_securities(request, market=None, q=None, limit=500),
        },
    )


@web_router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="status.html", context={"health": health(request)}
    )
