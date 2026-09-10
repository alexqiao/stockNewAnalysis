"""Stock-first JSON API and server-rendered research pages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from .config import OPPORTUNITY_ASSET_SYMBOLS
from .models import (
    Article,
    Event,
    EventArticle,
    EventSecurityImpact,
    EventTheme,
    IngestionRun,
    PEAnalysisProfile,
    Security,
    SecuritySignalSnapshot,
    SourceHealth,
    Theme,
    Watchlist,
    XAccount,
    XPost,
)
from .schemas import (
    PEAnalysisUpdate,
    RunResponse,
    WatchlistInput,
    WatchlistReplace,
    XAccountInput,
    XAccountUpdate,
    XPostDecision,
)
from .services import opportunities as opportunity_service
from .services.coordinator import PipelineBusyError, PipelineCoordinator
from .services.judgment import build_watchlist_judgment
from .services.metrics import build_metrics
from .services.pe_analysis import (
    analysis_response,
    apply_snapshot,
    apply_update,
    get_or_create_profile,
    record_refresh_error,
)
from .services.providers import lookup_security_record
from .services.research_quality import company_data_quality
from .services.scoring import DIRECTION_SIGN

api_router = APIRouter(prefix="/api/v1")
web_router = APIRouter()
VALID_HORIZONS = {1, 5, 20}
VALID_MARKETS = {"A", "HK", "US"}
OPPORTUNITY_KINDS = {"industry", "macro", "theme"}


def _session(request: Request) -> Session:
    return request.app.state.session_factory()


def _coordinator(request: Request) -> PipelineCoordinator:
    return request.app.state.coordinator


def _require_watchlisted_security(session: Session, security_id: int) -> Security:
    security = session.get(Security, security_id)
    if security is None:
        raise HTTPException(status_code=404, detail="证券不存在")
    if session.scalar(select(Watchlist.id).where(Watchlist.security_id == security_id)) is None:
        raise HTTPException(status_code=409, detail="请先将证券加入自选股")
    return security


def _security_brief(security: Security) -> dict[str, Any]:
    provider_data = security.provider_data or {}
    data_quality, data_gaps = company_data_quality(security)
    return {
        "id": security.id,
        "market": security.market,
        "exchange": security.exchange,
        "symbol": security.symbol,
        "name": security.name,
        "industry": security.industry,
        "market_cap": security.market_cap,
        "currency": security.currency,
        "opportunity_group": provider_data.get("opportunity_group"),
        "opportunity_scope": provider_data.get("opportunity_scope"),
        "data_quality": data_quality,
        "data_gaps": data_gaps,
    }


def _normalized_symbol(value: str) -> str:
    symbol = value.strip().upper().split(".", maxsplit=1)[0]
    if symbol.isdigit():
        return symbol.lstrip("0") or "0"
    return symbol


def _security_matches_exactly(security: Security, value: str) -> bool:
    expected = value.strip().casefold()
    if security.symbol.casefold() == expected or security.name.casefold() == expected:
        return True
    if "." not in value and _normalized_symbol(security.symbol) == _normalized_symbol(value):
        return True
    return any(alias.strip().casefold() == expected for alias in (security.aliases or []))


def _resolve_watchlist_security(session: Session, item: WatchlistInput) -> Security:
    if item.security_id is not None:
        security = session.get(Security, item.security_id)
        if security is None or not security.active:
            raise HTTPException(
                status_code=422,
                detail=f"不存在或已停用的 security_id：{item.security_id}",
            )
        return security

    assert item.query is not None
    query = select(Security).where(Security.active.is_(True))
    if item.market:
        query = query.where(Security.market == item.market)
    matches = [
        security
        for security in session.scalars(query)
        if _security_matches_exactly(security, item.query)
    ]
    if not matches and item.market:
        try:
            record = lookup_security_record(item.market, item.query)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"无法核验证券：{item.query}（{item.market}），请稍后重试",
            ) from exc
        if record is not None:
            security = Security(
                market=record.market,
                exchange=record.exchange,
                symbol=record.symbol,
                name=record.name,
                aliases=record.aliases,
                industry=record.industry,
                business_summary=record.business_summary,
                market_cap=record.market_cap,
                currency=record.currency,
                timezone=record.timezone,
                calendar=record.calendar,
                provider_data=record.provider_data or {},
            )
            session.add(security)
            session.flush()
            return security
    if not matches:
        market_hint = f"（{item.market}）" if item.market else ""
        raise HTTPException(
            status_code=422,
            detail=f"未找到证券：{item.query}{market_hint}，请输入精确代码或公司名称",
        )
    if len(matches) > 1:
        choices = "、".join(
            f"{security.market} {security.symbol} {security.name}" for security in matches[:5]
        )
        raise HTTPException(
            status_code=422,
            detail=f"证券输入存在歧义：{item.query}；请指定市场或完整代码。匹配项：{choices}",
        )
    return matches[0]


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
        "demand_status": event.demand_status,
        "evidence_grade": event.evidence_grade,
        "evidence_score": event.evidence_score,
        "missing_proof": event.missing_proof,
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
                "content_kind": link.article.content_kind,
                "evidence_role": link.article.evidence_role,
                "author_handle": link.article.author_handle,
                "analysis_eligible": link.article.analysis_eligible,
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
        "content_kind": article.content_kind,
        "evidence_role": article.evidence_role,
        "author_handle": article.author_handle,
        "analysis_eligible": article.analysis_eligible,
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


def _x_account_dict(account: XAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "handle": account.handle,
        "display_name": account.display_name,
        "account_type": account.account_type,
        "tags": account.tags,
        "priority": account.priority,
        "active": account.active,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }


def _x_post_dict(post: XPost) -> dict[str, Any]:
    return {
        "id": post.id,
        "account": _x_account_dict(post.account),
        "post_id": post.post_id,
        "url": post.url,
        "post_type": post.post_type,
        "text": post.text,
        "quoted_post_id": post.quoted_post_id,
        "quoted_author_handle": post.quoted_author_handle,
        "quoted_text": post.quoted_text,
        "external_links": post.external_links,
        "media": post.media,
        "public_metrics": post.public_metrics,
        "published_at": post.published_at,
        "fetched_at": post.fetched_at,
        "screening_status": post.screening_status,
        "screening": post.screening,
        "promoted_article_id": post.promoted_article_id,
        "related_event_id": post.related_event_id,
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
    components = snapshot.components or {}
    return {
        "id": snapshot.id,
        "as_of": snapshot.as_of,
        "horizon": snapshot.horizon,
        "score": snapshot.score,
        "research_score": components.get("research_score", snapshot.score),
        "decision_score": components.get("decision_score", snapshot.score),
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
    direction: str = Query(default="bullish", pattern="^(bullish|bearish)$"),
) -> list[dict[str, Any]]:
    if horizon not in VALID_HORIZONS:
        raise HTTPException(status_code=422, detail="horizon 只能是 1、5 或 20")
    with _session(request) as session:
        return opportunity_service.list_security_opportunities(
            session, market, theme, horizon, limit, direction
        )


# Keep the existing private module surface while using one shared implementation.
_aggregate_trend_opportunities = opportunity_service.aggregate_trend_opportunities
_infer_impact_themes = opportunity_service.infer_impact_themes
_themes_are_similar = opportunity_service.themes_are_similar
_opportunity_url = opportunity_service.opportunity_url


def _trend_opportunities(
    request: Request,
    market: str | None,
    theme: str | None,
    horizon: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    with _session(request) as session:
        return opportunity_service.trend_opportunities(
            session, market, theme, horizon, limit
        )


def _dashboard_opportunities(
    request: Request,
    market: str | None,
    theme: str | None,
    horizon: int,
) -> list[dict[str, Any]]:
    with _session(request) as session:
        return opportunity_service.dashboard_opportunities(
            session, market, theme, horizon
        )


def _unique_impact_values(
    impacts: list[EventSecurityImpact], attribute: str, limit: int = 5
) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for impact in sorted(impacts, key=lambda item: item.opportunity_score, reverse=True):
        raw_values = getattr(impact, attribute)
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            normalized = str(value or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
            if len(result) == limit:
                return result
    return result


def _related_stock_rows(
    impacts: list[EventSecurityImpact], horizon: int, limit: int = 5
) -> list[dict[str, Any]]:
    groups: dict[int, dict[str, Any]] = {}
    for impact in impacts:
        if (
            (impact.security.provider_data or {}).get("research_asset")
            or impact.security.symbol in OPPORTUNITY_ASSET_SYMBOLS
        ):
            continue
        horizon_impact = (impact.impacts or {}).get(str(horizon))
        if not isinstance(horizon_impact, dict):
            continue
        confidence = float(horizon_impact.get("confidence") or 0.0)
        direction = str(horizon_impact.get("direction") or "neutral")
        contribution = (
            DIRECTION_SIGN.get(direction, 0.0) * impact.opportunity_score * confidence
        )
        group = groups.setdefault(
            impact.security_id,
            {
                "security": impact.security,
                "signed_total": 0.0,
                "absolute_total": 0.0,
                "confidence_total": 0.0,
                "impact_count": 0,
                "event_ids": set(),
                "best_thesis": "",
                "best_thesis_weight": -1.0,
            },
        )
        group["signed_total"] += contribution
        group["absolute_total"] += abs(contribution)
        group["confidence_total"] += confidence
        group["impact_count"] += 1
        group["event_ids"].add(impact.event_id)
        thesis_weight = impact.opportunity_score * confidence
        if impact.thesis and thesis_weight > group["best_thesis_weight"]:
            group["best_thesis"] = impact.thesis
            group["best_thesis_weight"] = thesis_weight

    result = []
    for group in groups.values():
        normalizer = group["confidence_total"]
        if normalizer <= 0:
            continue
        score = group["signed_total"] / normalizer
        conflict = (
            1.0 - abs(group["signed_total"]) / group["absolute_total"]
            if group["absolute_total"]
            else 0.0
        )
        mean_confidence = normalizer / group["impact_count"]
        confidence = max(0.0, min(1.0, mean_confidence * (1 - conflict)))
        direction = "bullish" if score > 5 else "bearish" if score < -5 else "neutral"
        result.append(
            {
                "security": _security_brief(group["security"]),
                "score": round(score, 2),
                "direction": direction,
                "confidence": round(confidence, 4),
                "conflict": round(max(0.0, min(1.0, conflict)), 4),
                "evidence_event_count": len(group["event_ids"]),
                "thesis": group["best_thesis"],
                "relevance": abs(score) * confidence,
            }
        )
    result.sort(
        key=lambda item: (
            item["relevance"],
            abs(item["score"]),
            item["security"]["symbol"],
        ),
        reverse=True,
    )
    return result[:limit]


def _opportunity_detail(
    request: Request,
    kind: str,
    name: str,
    horizon: int,
    market: str | None,
    theme: str | None,
) -> dict[str, Any]:
    if kind not in OPPORTUNITY_KINDS:
        raise HTTPException(status_code=404, detail="机会不存在")
    opportunities = _trend_opportunities(request, market, theme, horizon, None)
    opportunity = next(
        (
            item
            for item in opportunities
            if item["kind"] == kind and item["name"] == name
        ),
        None,
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="机会不存在或已无有效信号")

    event_ids = set(opportunity["event_ids"])
    security_ids = set(opportunity["security_ids"])
    with _session(request) as session:
        events = list(
            session.scalars(_event_query().where(Event.id.in_(event_ids))).unique().all()
        ) if event_ids else []
        events.sort(
            key=lambda item: item.occurred_at or item.created_at,
            reverse=True,
        )
        impacts = [
            impact
            for event in events
            for impact in event.impacts
            if impact.is_current and impact.status == "complete"
        ]
        defining_impacts = [
            impact for impact in impacts if impact.security_id in security_ids
        ]
        event_rows = []
        for event in events[:10]:
            row = _event_dict(event)
            relevant_impacts = [
                impact for impact in defining_impacts if impact.event_id == event.id
            ]
            row["themes"] = [
                item
                for item in row["themes"]
                if _themes_are_similar(item["name"], opportunity["name"])
            ]
            row["opportunity_evidence"] = _unique_impact_values(
                relevant_impacts, "evidence", 3
            )
            row["opportunity_theses"] = _unique_impact_values(
                relevant_impacts, "thesis", 3
            )
            event_rows.append(row)
        stocks = _related_stock_rows(defining_impacts, horizon)

    back_params: dict[str, str | int] = {"horizon": horizon}
    if market:
        back_params["market"] = market
    if theme:
        back_params["theme"] = theme
    return {
        **opportunity,
        "horizon": horizon,
        "events": event_rows,
        "event_total": len(events),
        "stocks": stocks,
        "analysis": {
            "theses": _unique_impact_values(defining_impacts, "thesis"),
            "catalysts": _unique_impact_values(defining_impacts, "catalysts"),
            "risks": _unique_impact_values(defining_impacts, "risks"),
            "falsifiers": _unique_impact_values(defining_impacts, "falsifiers"),
        },
        "back_url": f"/?{urlencode(back_params)}",
    }


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
            q = q.strip()
            query = query.where(
                Security.name.ilike(f"%{q}%") | Security.symbol.ilike(f"%{q}%")
            )
        securities = list(
            session.scalars(
                query.order_by(Security.market, Security.symbol).limit(limit * 5)
            )
        )
        if q:
            expected = q.casefold()
            securities.sort(
                key=lambda item: (
                    0
                    if item.symbol.casefold() == expected
                    or item.name.casefold() == expected
                    else 1,
                    item.market,
                    item.symbol,
                )
            )
        return [_security_brief(item) for item in securities[:limit]]


@api_router.get("/securities/{security_id}")
def get_security(security_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        security = session.scalar(
            select(Security)
            .where(Security.id == security_id)
            .options(
                selectinload(Security.impacts).selectinload(EventSecurityImpact.event),
                selectinload(Security.snapshots),
                selectinload(Security.watchlist_entry),
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
            "watchlisted": security.watchlist_entry is not None,
            "signals": {str(key): _signal_dict(value) for key, value in latest.items()},
            "impacts": [
                {**_impact_dict(item), "event_title": item.event.title}
                for item in impacts
            ],
        }


@api_router.get("/securities/{security_id}/pe-analysis")
def get_pe_analysis(security_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        security = _require_watchlisted_security(session, security_id)
        profile = session.scalar(
            select(PEAnalysisProfile).where(PEAnalysisProfile.security_id == security_id)
        )
        return analysis_response(security, profile)


@api_router.put("/securities/{security_id}/pe-analysis")
def update_pe_analysis(
    security_id: int, payload: PEAnalysisUpdate, request: Request
) -> dict[str, Any]:
    with _session(request) as session:
        security = _require_watchlisted_security(session, security_id)
        profile = get_or_create_profile(session, security)
        apply_update(profile, payload)
        session.commit()
        return analysis_response(security, profile)


@api_router.post("/securities/{security_id}/pe-analysis/refresh")
def refresh_pe_analysis(security_id: int, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        security = _require_watchlisted_security(session, security_id)
        profile = get_or_create_profile(session, security)
        try:
            snapshot = request.app.state.fundamental_provider.fetch(
                security.market,
                security.symbol,
                security.provider_data or {},
            )
        except Exception as exc:
            record_refresh_error(profile, exc)
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="自动基础数据刷新失败，已保留上次成功数据",
            ) from exc
        apply_snapshot(profile, snapshot)
        if snapshot.market_cap is not None:
            security.market_cap = snapshot.market_cap
        session.commit()
        return analysis_response(security, profile)


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
        result = _event_dict(event)
        social_posts = session.scalars(
            select(XPost)
            .where(XPost.related_event_id == event_id)
            .options(selectinload(XPost.account))
            .order_by(XPost.published_at.desc())
        ).all()
        result["x_posts"] = [_x_post_dict(item) for item in social_posts]
        return result


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


@api_router.get("/x/accounts")
def list_x_accounts(request: Request) -> list[dict[str, Any]]:
    with _session(request) as session:
        accounts = session.scalars(
            select(XAccount).order_by(XAccount.priority, XAccount.id)
        ).all()
        return [_x_account_dict(item) for item in accounts]


@api_router.post("/x/accounts", status_code=status.HTTP_201_CREATED)
def create_x_account(payload: XAccountInput, request: Request) -> dict[str, Any]:
    with _session(request) as session:
        existing = session.scalar(
            select(XAccount).where(XAccount.handle.ilike(payload.handle))
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="该 X 账号已存在")
        account = XAccount(**payload.model_dump())
        if not account.display_name:
            account.display_name = account.handle
        session.add(account)
        session.commit()
        return _x_account_dict(account)


@api_router.patch("/x/accounts/{account_id}")
def update_x_account(
    account_id: int, payload: XAccountUpdate, request: Request
) -> dict[str, Any]:
    with _session(request) as session:
        account = session.get(XAccount, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="X 账号不存在")
        for name, value in payload.model_dump(exclude_unset=True).items():
            setattr(account, name, value)
        account.updated_at = datetime.now(UTC)
        session.commit()
        return _x_account_dict(account)


@api_router.get("/x/posts")
def list_x_posts(
    request: Request,
    account_id: int | None = Query(default=None, gt=0),
    screening_status: str | None = Query(default=None, max_length=20),
    classification: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    with _session(request) as session:
        query = (
            select(XPost)
            .options(selectinload(XPost.account))
            .order_by(XPost.published_at.desc(), XPost.id.desc())
        )
        if account_id is not None:
            query = query.where(XPost.account_id == account_id)
        if screening_status:
            query = query.where(XPost.screening_status == screening_status)
        posts = session.scalars(query.limit(limit * 3)).all()
        if classification:
            posts = [
                item
                for item in posts
                if str((item.screening or {}).get("classification")) == classification
            ]
        return [_x_post_dict(item) for item in posts[:limit]]


@api_router.post("/x/posts/{post_id}/decision")
def decide_x_post(
    post_id: int, payload: XPostDecision, request: Request
) -> dict[str, Any]:
    coordinator = _coordinator(request)
    with _session(request) as session:
        try:
            event_id = coordinator.x_ingestion.apply_decision(
                session,
                post_id,
                payload.decision,
                event_id=payload.event_id,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        session.commit()
    if event_id is not None:
        coordinator.submit_analysis(event_id)
    return {"post_id": post_id, "decision": payload.decision, "event_id": event_id}


@api_router.post("/runs/x-ingest", status_code=status.HTTP_202_ACCEPTED)
def start_x_ingestion(request: Request) -> dict[str, str]:
    try:
        _coordinator(request).submit_x_ingestion()
    except PipelineBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "queued"}


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
            .options(
                selectinload(Watchlist.security).selectinload(Security.pe_analysis_profile)
            )
            .order_by(Watchlist.position, Watchlist.id)
        ).all()
        return [
            {
                "security_id": item.security_id,
                "active": item.active,
                "position": item.position,
                "security": _security_brief(item.security),
                "pe_analysis": analysis_response(
                    item.security, item.security.pe_analysis_profile
                )["summary"],
            }
            for item in items
        ]


@api_router.put("/watchlist")
def replace_watchlist(payload: WatchlistReplace, request: Request) -> list[dict[str, Any]]:
    with _session(request) as session:
        resolved = [
            (_resolve_watchlist_security(session, item), item)
            for item in payload.items
        ]
        security_ids = [security.id for security, _ in resolved]
        if len(security_ids) != len(set(security_ids)):
            raise HTTPException(status_code=422, detail="自选证券不能重复")
        session.execute(delete(Watchlist))
        session.add_all(
            [
                Watchlist(
                    security_id=security.id,
                    active=item.active,
                    position=position,
                )
                for position, (security, item) in enumerate(resolved)
            ]
        )
        session.commit()
    return get_watchlist(request)


@api_router.get("/health")
def health(request: Request) -> dict[str, Any]:
    semantic_clustering = _coordinator(request).ingestion.semantic_matcher.status()
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
            "semantic_clustering": semantic_clustering,
            "scheduler_enabled": request.app.state.settings.scheduler_enabled,
            "pipeline_busy": _coordinator(request).busy,
            "x_browser_enabled": request.app.state.settings.x_browser_enabled,
            "x_fetch_interval_hours": request.app.state.settings.x_fetch_interval_hours,
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
    opportunities = _dashboard_opportunities(request, market, theme, horizon)
    watchlist = get_watchlist(request)
    themes = list_themes(request)
    coverage = health(request)
    with _session(request) as session:
        ids = {item["security_id"] for item in watchlist}
        snapshots = _latest_snapshot_map(session, horizon, ids)
        signals = {
            security_id: _signal_dict(snapshot)
            for security_id, snapshot in snapshots.items()
        }
        evidence_event_ids = {
            int(event_id)
            for signal in signals.values()
            if signal is not None
            for event_id in signal.get("evidence_event_ids") or []
        }
        event_titles: dict[int, str] = (
            {
                event_id: title
                for event_id, title in session.execute(
                    select(Event.id, Event.title).where(Event.id.in_(evidence_event_ids))
                )
            }
            if evidence_event_ids
            else {}
        )
        macro_row = session.execute(
            select(Event.id, Article.title, Article.summary, Article.published_at)
            .join(EventArticle, EventArticle.event_id == Event.id)
            .join(Article, Article.id == EventArticle.article_id)
            .where(
                Article.source == "BLS Employment Situation",
                Article.published_at >= datetime.now(UTC) - timedelta(days=7),
            )
            .order_by(Article.published_at.desc(), Article.id.desc())
            .limit(1)
        ).first()
        macro_context = (
            {
                "event_id": macro_row.id,
                "title": macro_row.title,
                "summary": macro_row.summary,
                "published_at": macro_row.published_at,
                "direction_note": "已纳入宏观背景；缺少市场一致预期，暂不计入个股方向",
            }
            if macro_row is not None
            else None
        )
        watchlist_rows = []
        for item in watchlist:
            signal = signals.get(item["security_id"])
            watchlist_rows.append(
                {
                    **item,
                    "signal": signal,
                    "judgment": build_watchlist_judgment(
                        signal, item["pe_analysis"], event_titles, macro_context
                    ),
                }
            )
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


@web_router.get("/opportunities/{kind}", response_class=HTMLResponse)
def opportunity_page(
    kind: str,
    request: Request,
    name: str = Query(min_length=1, max_length=160),
    horizon: int = Query(default=5),
    market: str | None = Query(default=None, pattern="^(A|HK|US)$"),
    theme: str | None = None,
) -> HTMLResponse:
    opportunity = _opportunity_detail(
        request, kind, name, horizon, market, theme
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="opportunity.html",
        context={"opportunity": opportunity},
    )


@web_router.get("/securities/{security_id}", response_class=HTMLResponse)
def security_page(security_id: int, request: Request) -> HTMLResponse:
    security = get_security(security_id, request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="security.html",
        context={
            "security": security,
            "pe_analysis": get_pe_analysis(security_id, request)
            if security["watchlisted"]
            else None,
        },
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
        context={"items": get_watchlist(request)},
    )


@web_router.get("/x", response_class=HTMLResponse)
def x_posts_page(
    request: Request,
    account_id: int | None = None,
    screening_status: str | None = None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="x_posts.html",
        context={
            "posts": list_x_posts(
                request,
                account_id=account_id,
                screening_status=screening_status,
                classification=None,
                limit=200,
            ),
            "accounts": list_x_accounts(request),
            "selected_account_id": account_id,
            "selected_status": screening_status,
        },
    )


@web_router.get("/x/accounts", response_class=HTMLResponse)
def x_accounts_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="x_accounts.html",
        context={"accounts": list_x_accounts(request)},
    )


@web_router.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name="status.html", context={"health": health(request)}
    )
