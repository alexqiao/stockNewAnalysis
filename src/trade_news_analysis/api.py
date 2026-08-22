"""JSON API and server-rendered pages."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from .models import (
    Article,
    ArticleSymbol,
    ImpactAnalysis,
    IngestionRun,
    SourceHealth,
    Watchlist,
)
from .schemas import RunResponse, WatchlistReplace
from .services.coordinator import PipelineBusyError, PipelineCoordinator
from .services.metrics import build_metrics

api_router = APIRouter(prefix="/api/v1")
web_router = APIRouter()


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _coordinator(request: Request) -> PipelineCoordinator:
    return request.app.state.coordinator


def _current_analysis(relation: ArticleSymbol) -> ImpactAnalysis | None:
    return next((item for item in relation.analyses if item.is_current), None)


def _analysis_dict(analysis: ImpactAnalysis | None) -> dict[str, Any] | None:
    if not analysis:
        return None
    return {
        "id": analysis.id,
        "status": analysis.status,
        "model": analysis.model,
        "created_at": analysis.created_at,
        "event_type": analysis.event_type,
        "novelty": analysis.novelty,
        "priced_in": analysis.priced_in,
        "impacts": analysis.impacts,
        "financial_channels": analysis.financial_channels,
        "observed_demand": analysis.observed_demand,
        "thesis": analysis.thesis,
        "catalysts": analysis.catalysts,
        "risks": analysis.risks,
        "falsifiers": analysis.falsifiers,
        "evidence": analysis.evidence,
        "error": analysis.error,
        "outcomes": [
            {
                "horizon": outcome.horizon,
                "return_pct": outcome.return_pct,
                "benchmark_return_pct": outcome.benchmark_return_pct,
                "excess_return_pct": outcome.excess_return_pct,
                "predicted_direction": outcome.predicted_direction,
                "actual_direction": outcome.actual_direction,
                "correct": outcome.correct,
            }
            for outcome in sorted(analysis.outcomes, key=lambda item: item.horizon)
        ],
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
        "story_cluster_id": article.story_cluster_id,
        "symbols": [
            {
                "symbol": relation.symbol,
                "match_method": relation.match_method,
                "in_title": relation.in_title,
                "analysis": _analysis_dict(_current_analysis(relation)),
            }
            for relation in sorted(article.symbols, key=lambda item: item.symbol)
        ],
    }


def _article_query() -> Any:
    return select(Article).options(
        selectinload(Article.symbols)
        .selectinload(ArticleSymbol.analyses)
        .selectinload(ImpactAnalysis.outcomes)
    )


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


@api_router.get("/news")
def list_news(
    request: Request,
    symbol: str | None = None,
    source: str | None = None,
    direction: str | None = Query(default=None, pattern="^(bullish|neutral|bearish)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[dict[str, Any]]:
    with _session(request) as session:
        query = _article_query().order_by(Article.published_at.desc(), Article.id.desc())
        if symbol:
            query = query.join(ArticleSymbol).where(ArticleSymbol.symbol == symbol.upper())
        if source:
            query = query.where(Article.source.ilike(f"%{source}%"))
        articles = session.scalars(query.limit(limit * 3 if direction else limit)).unique().all()
        result = [_article_dict(article) for article in articles]
        if direction:
            result = [
                article
                for article in result
                if any(
                    symbol_item["analysis"]
                    and any(
                        impact.get("direction") == direction
                        for impact in symbol_item["analysis"].get("impacts", {}).values()
                    )
                    for symbol_item in article["symbols"]
                )
            ]
        return result[:limit]


@api_router.get("/news/{article_id}")
def get_news(article_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        article = session.scalar(_article_query().where(Article.id == article_id))
        if not article:
            raise HTTPException(status_code=404, detail="新闻不存在")
        return _article_dict(article)


@api_router.post("/news/{article_id}/analyses", status_code=status.HTTP_202_ACCEPTED)
def reanalyze_news(article_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        article = session.scalar(_article_query().where(Article.id == article_id))
        if not article:
            raise HTTPException(status_code=404, detail="新闻不存在")
        if not article.symbols:
            raise HTTPException(status_code=400, detail="新闻尚未关联股票")
        analysis_ids = []
        for relation in article.symbols:
            analysis = ImpactAnalysis(
                article_symbol_id=relation.id,
                status="pending",
                model=request.app.state.settings.llm_model,
                is_current=False,
            )
            session.add(analysis)
            session.flush()
            analysis_ids.append(analysis.id)
        session.commit()
    for analysis_id in analysis_ids:
        _coordinator(request).submit_analysis(analysis_id)
    return {"analysis_ids": analysis_ids, "status": "queued"}


@api_router.get("/metrics")
def metrics(
    request: Request,
    symbol: str | None = None,
    horizon: int | None = Query(default=None, ge=1, le=20),
    since: datetime | None = None,
) -> dict[str, Any]:
    if horizon is not None and horizon not in {1, 5, 20}:
        raise HTTPException(status_code=422, detail="horizon 只能是 1、5 或 20")
    with _session(request) as session:
        return build_metrics(session, symbol=symbol, horizon=horizon, since=since)


@api_router.get("/watchlist")
def get_watchlist(request: Request) -> list[dict[str, Any]]:
    with _session(request) as session:
        items = session.scalars(select(Watchlist).order_by(Watchlist.symbol)).all()
        return [
            {
                "symbol": item.symbol,
                "company_name": item.company_name,
                "aliases": item.aliases,
                "active": item.active,
            }
            for item in items
        ]


@api_router.put("/watchlist")
def replace_watchlist(payload: WatchlistReplace, request: Request) -> list[dict[str, Any]]:
    symbols = [item.symbol for item in payload.items]
    if len(symbols) != len(set(symbols)):
        raise HTTPException(status_code=422, detail="自选股代码不能重复")
    with _session(request) as session:
        session.execute(delete(Watchlist))
        for item in payload.items:
            session.add(Watchlist(**item.model_dump()))
        session.commit()
    return get_watchlist(request)


@api_router.get("/health")
def health(request: Request) -> dict[str, Any]:
    with _session(request) as session:
        sources = session.scalars(select(SourceHealth).order_by(SourceHealth.source)).all()
        latest_run = session.scalar(select(IngestionRun).order_by(IngestionRun.id.desc()).limit(1))
        return {
            "status": "ok",
            "llm_configured": request.app.state.settings.llm_configured,
            "scheduler_enabled": request.app.state.settings.scheduler_enabled,
            "pipeline_busy": _coordinator(request).busy,
            "latest_run": RunResponse.model_validate(latest_run).model_dump()
            if latest_run
            else None,
            "sources": [
                {
                    "source": item.source,
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
def dashboard(request: Request, symbol: str | None = None) -> HTMLResponse:
    articles = list_news(request, symbol=symbol, limit=100)
    with _session(request) as session:
        symbols = session.scalars(
            select(Watchlist.symbol).where(Watchlist.active.is_(True)).order_by(Watchlist.symbol)
        ).all()
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"articles": articles, "symbols": symbols, "selected_symbol": symbol},
    )


@web_router.get("/news/{article_id}", response_class=HTMLResponse)
def news_detail(article_id: int, request: Request) -> HTMLResponse:
    article = get_news(article_id, request)
    return request.app.state.templates.TemplateResponse(
        request=request, name="detail.html", context={"article": article}
    )


@web_router.get("/metrics", response_class=HTMLResponse)
def metrics_page(request: Request, symbol: str | None = None) -> HTMLResponse:
    data = metrics(request, symbol=symbol)
    return request.app.state.templates.TemplateResponse(
        request=request, name="metrics.html", context={"metrics": data, "symbol": symbol}
    )


@web_router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="watchlist.html", context={"items": get_watchlist(request)}
    )


@web_router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="status.html", context={"health": health(request)}
    )
