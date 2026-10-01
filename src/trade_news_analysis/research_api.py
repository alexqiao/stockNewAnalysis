"""Research workbench: explicit edits, read-only views and background refresh."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .api import _session
from .decision_models import EventTimingRevision, MacroObservationVintage
from .models import Article, Event, EventArticle, EventSecurityImpact, Security, Watchlist
from .research_data_models import ResearchCalendarRevision
from .services.coordinator import PipelineBusyError
from .services.normalization import ensure_aware

router = APIRouter()


class ResearchRefreshInput(BaseModel):
    security_ids: list[int] | None = Field(default=None, max_length=100)


class TaskUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    status: Literal["pending", "done", "dismissed", "expired"]
    note: str = Field(min_length=1, max_length=3000)
    review_due_at: datetime | None = None


class ClaimEvidenceInput(BaseModel):
    url: str = Field(max_length=2048)
    stance: Literal["support", "conflict"]
    note: str = Field(min_length=1, max_length=3000)
    source_url: str | None = Field(default=None, max_length=2048)
    is_official: bool = False


class ClaimUpdate(BaseModel):
    expected_revision: int = Field(ge=1)
    status: Literal["pending", "partially_supported", "verified", "refuted", "expired"]
    note: str = Field(min_length=1, max_length=3000)
    review_due_at: datetime | None = None
    evidence: list[ClaimEvidenceInput] | None = Field(default=None, max_length=20)


class PortfolioInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    total_value: float | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    available_cash: float | None = Field(default=None, ge=0)


class RiskInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    current_weight: float | None = Field(default=None, ge=0, le=1)
    current_quantity: float | None = Field(default=None, ge=0)
    sector_current_weight: float | None = Field(default=None, ge=0, le=1)
    average_cost: float | None = Field(default=None, gt=0)
    max_weight: float | None = Field(default=None, gt=0, le=1)
    risk_budget_pct: float | None = Field(default=None, gt=0, le=1)
    stop_price: float | None = Field(default=None, gt=0)
    sector_limit_pct: float | None = Field(default=None, gt=0, le=1)
    lot_size: int | None = Field(default=None, ge=1)
    max_participation_pct: float | None = Field(default=None, gt=0, le=1)
    fee_bps: float | None = Field(default=None, ge=0, le=1000)
    slippage_bps: float | None = Field(default=None, ge=0, le=1000)
    benchmark_market: Literal["US", "A", "HK"] | None = None
    benchmark_symbol: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.^-]{1,24}$")
    benchmark_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    benchmark_label: str | None = Field(default=None, max_length=160)


class FactTimingInput(BaseModel):
    first_disclosed_at: datetime
    financial_period: str | None = Field(default=None, max_length=80)
    source_url: str = Field(max_length=2048)
    note: str = Field(min_length=1, max_length=3000)

    @field_validator("first_disclosed_at")
    @classmethod
    def known_date(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("首次披露时间必须带时区且不能在未来")
        return value


class DisclosureInput(BaseModel):
    article_id: int | None = Field(default=None, gt=0)
    event_id: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(max_length=2048)
    excerpt: str | None = Field(default="", max_length=10000)
    published_at: datetime | None = None

    @field_validator("excerpt", mode="before")
    @classmethod
    def empty_excerpt(cls, value: Any) -> str:
        return str(value or "")

    @field_validator("published_at")
    @classmethod
    def explicit_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("发布时间必须包含时区")
        return value


class CalendarInput(BaseModel):
    event_key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=500)
    event_type: Literal["earnings", "dividend", "other"]
    scheduled_date: date
    scheduled_at: datetime | None = None
    timezone: str = Field(max_length=64)
    status: Literal["confirmed", "estimated", "cancelled"]
    source_url: str = Field(max_length=2048)

    @field_validator("scheduled_at")
    @classmethod
    def explicit_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("事件精确时间必须包含时区")
        return value


class ExpectationInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    event_key: str = Field(min_length=1, max_length=160)
    metric: str = Field(min_length=1, max_length=80)
    value: float
    unit: str = Field(min_length=1, max_length=40)
    financial_period: str = Field(min_length=1, max_length=64)
    source_url: str = Field(max_length=2048)
    expected_at: datetime | None = None


class OfficialSourceInput(BaseModel):
    official_ir_url: str = Field(max_length=2048)
    confirmed_company_site: Literal[True]


class ActualInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    event_key: str = Field(min_length=1, max_length=160)
    metric: Literal["eps", "revenue"]
    value: float
    unit: str = Field(min_length=1, max_length=40)
    financial_period: str = Field(min_length=1, max_length=64)
    source_url: str = Field(max_length=2048)
    published_at: datetime

    @field_validator("published_at")
    @classmethod
    def published(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(UTC):
            raise ValueError("实际发布时间必须包含时区且不能晚于当前时间")
        return value


def _row_dict(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def _http_error(exc: ValueError | LookupError) -> HTTPException:
    from .services.research_workflow import RevisionConflict

    if isinstance(exc, RevisionConflict):
        return HTTPException(409, {"message": str(exc), "latest": exc.latest})
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 422, detail=str(exc))


@router.post("/api/v1/research/refresh", status_code=202)
def refresh_research(payload: ResearchRefreshInput, request: Request) -> dict[str, Any]:
    if payload.security_ids is not None:
        if not payload.security_ids or any(value <= 0 for value in payload.security_ids):
            raise HTTPException(422, "security_ids 必须是非空正整数列表")
        with _session(request) as session:
            valid = set(
                session.scalars(select(Security.id).where(Security.id.in_(payload.security_ids)))
            )
            if valid != set(payload.security_ids):
                raise HTTPException(404, "证券不存在")
    try:
        run_id = request.app.state.coordinator.submit_research(payload.security_ids)
    except PipelineBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"run_id": run_id, "status": "queued"}


@router.post("/api/v1/research/capture")
def capture_research(request: Request) -> dict[str, Any]:
    from .services.research_cycle import capture_research_state

    with _session(request) as session:
        result = capture_research_state(session, request.app.state.settings)
        session.commit()
        return result


@router.patch("/api/v1/research/tasks/{task_id}")
def edit_task(task_id: int, payload: TaskUpdate, request: Request) -> dict[str, Any]:
    from .services.research_workflow import update_task

    with _session(request) as session:
        try:
            result = update_task(session, task_id, **payload.model_dump())
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        session.commit()
        return result


@router.get("/api/v1/research/claims")
def get_claims(request: Request, security_id: int | None = None) -> list[dict[str, Any]]:
    from .services.research_workflow import list_claims

    with _session(request) as session:
        return list_claims(session, security_id=security_id)


@router.get("/api/v1/research/claims/page")
def get_claim_page(
    request: Request, security_id: int | None = None, limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, gt=0), status: str | None = None,
) -> dict[str, Any]:
    from .services.research_workflow import claim_page

    with _session(request) as session:
        try:
            return claim_page(session, security_id, limit=limit, before_id=before_id, status=status)
        except ValueError as exc:
            raise _http_error(exc) from exc


@router.get("/api/v1/research/claims/{claim_id}")
def get_claim_detail(claim_id: int, request: Request) -> dict[str, Any]:
    from .services.research_workflow import get_claim

    with _session(request) as session:
        try:
            return get_claim(session, claim_id)
        except LookupError as exc:
            raise _http_error(exc) from exc


@router.patch("/api/v1/research/claims/{claim_id}")
def edit_claim(claim_id: int, payload: ClaimUpdate, request: Request) -> dict[str, Any]:
    from .services.research_workflow import update_claim

    with _session(request) as session:
        try:
            result = update_claim(session, claim_id, **payload.model_dump())
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        if result["invalidated_event_ids"]:
            from .services.scoring import rebuild_signal_snapshots

            rebuild_signal_snapshots(session)
        session.commit()
        return result


@router.put("/api/v1/research/portfolio")
def edit_portfolio(payload: PortfolioInput, request: Request) -> dict[str, Any]:
    from .services.risk import save_portfolio_risk

    with _session(request) as session:
        try:
            result = save_portfolio_risk(session, payload.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise _http_error(exc) from exc
        session.commit()
        return result


@router.put("/api/v1/research/securities/{security_id}/risk")
def edit_risk(security_id: int, payload: RiskInput, request: Request) -> dict[str, Any]:
    from .services.risk import save_security_risk

    with _session(request) as session:
        if session.get(Security, security_id) is None:
            raise HTTPException(404, "证券不存在")
        try:
            result = save_security_risk(
                session, security_id, payload.model_dump(exclude_unset=True)
            )
        except ValueError as exc:
            raise _http_error(exc) from exc
        session.commit()
        return result


@router.put("/api/v1/research/events/{event_id}/timing")
def edit_fact_time(event_id: int, payload: FactTimingInput, request: Request) -> dict[str, Any]:
    from .services.scoring import rebuild_signal_snapshots

    parsed = urlsplit(payload.source_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(422, "请填写可追溯的 HTTPS 原始披露链接")
    with _session(request) as session:
        event = session.get(Event, event_id)
        if event is None:
            raise HTTPException(404, "事件不存在")
        # A later report must not move a previously verified first disclosure forward.
        if (
            event.first_disclosed_at
            and ensure_aware(event.first_disclosed_at) < payload.first_disclosed_at
        ):
            raise HTTPException(422, "不能用后续报道推迟首次披露时间；新增事实应单独记录事件")
        changed = (
            ensure_aware(event.first_disclosed_at) if event.first_disclosed_at else None,
            event.fact_time_verified, event.financial_period, event.fact_source_url,
        ) != (payload.first_disclosed_at, True, payload.financial_period, payload.source_url)
        event.first_disclosed_at = payload.first_disclosed_at
        event.fact_time_verified = True
        event.financial_period = payload.financial_period
        event.fact_source_url = payload.source_url
        session.add(EventTimingRevision(event_id=event_id, payload=payload.model_dump(mode="json")))
        session.flush()
        if changed:
            from .services.analysis_versions import invalidate_event_analysis

            invalidate_event_analysis(session, [event_id], "首次披露依据已修订，请重新分析")
        rebuild_signal_snapshots(session)
        session.commit()
        return {"event_id": event_id, "fact_time_verified": True}


@router.post("/api/v1/research/securities/{security_id}/disclosures")
def add_disclosure(security_id: int, payload: DisclosureInput, request: Request) -> dict[str, Any]:
    from .services.analysis_versions import invalidate_event_analysis, reconcile_research_inputs
    from .services.research_data import record_manual_disclosure
    from .services.scoring import rebuild_signal_snapshots

    with _session(request) as session:
        if session.get(Security, security_id) is None:
            raise HTTPException(404, "证券不存在")
        article = session.get(Article, payload.article_id) if payload.article_id else None
        if payload.article_id and article is None:
            raise HTTPException(404, "报道不存在")
        if payload.event_id and article is None:
            raise HTTPException(422, "请选择该事件中需要关联原始出处的具体报道")
        linked_events: list[int] = []
        if article is not None:
            linked_events = list(session.scalars(select(EventArticle.event_id).where(
                EventArticle.article_id == article.id,
            )))
            allowed = set(session.scalars(select(EventSecurityImpact.event_id).where(
                EventSecurityImpact.security_id == security_id,
                EventSecurityImpact.event_id.in_(linked_events),
            )))
            if not allowed or (payload.event_id is not None and payload.event_id not in allowed):
                raise HTTPException(422, "所选报道、事件与当前证券不一致，请重新选择")
        try:
            result = record_manual_disclosure(session, security_id, **payload.model_dump(
                exclude={"article_id", "event_id"},
            ))
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        changed_events = []
        if article is not None and article.original_source_url != result.source_url:
            article.original_source_url = result.source_url
            changed_events = invalidate_event_analysis(
                session, linked_events, "已关联新的官方原始出处，请重新分析",
            )
        changed_events += reconcile_research_inputs(session, [security_id])
        if changed_events:
            rebuild_signal_snapshots(session)
        session.commit()
        return _row_dict(result)


@router.post("/api/v1/research/securities/{security_id}/calendar")
def add_calendar(security_id: int, payload: CalendarInput, request: Request) -> dict[str, Any]:
    from .services.research_data import record_manual_calendar

    with _session(request) as session:
        if session.get(Security, security_id) is None:
            raise HTTPException(404, "证券不存在")
        try:
            result = record_manual_calendar(session, security_id, **payload.model_dump())
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        session.commit()
        return _row_dict(result)


@router.post("/api/v1/research/securities/{security_id}/expectations")
def add_expectation(
    security_id: int,
    payload: ExpectationInput,
    request: Request,
) -> dict[str, Any]:
    from .services.research_data import record_manual_expectation

    with _session(request) as session:
        try:
            result = record_manual_expectation(session, security_id, **payload.model_dump())
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        from .services.analysis_versions import reconcile_research_inputs
        from .services.scoring import rebuild_signal_snapshots

        if reconcile_research_inputs(session, [security_id]):
            rebuild_signal_snapshots(session)
        session.commit()
        return _row_dict(result)


@router.put("/api/v1/research/securities/{security_id}/official-source")
def set_official_source(
    security_id: int,
    payload: OfficialSourceInput,
    request: Request,
) -> dict[str, Any]:
    import ipaddress

    try:
        parsed = urlsplit(payload.official_ir_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise ValueError
        if not host or "." not in host or host.endswith((".local", ".localhost")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "请填写已确认属于该公司的公开 HTTPS 投资者关系页面") from None
    with _session(request) as session:
        security = session.get(Security, security_id)
        if security is None:
            raise HTTPException(404, "证券不存在")
        security.provider_data = {
            **(security.provider_data or {}),
            "official_ir_url": payload.official_ir_url,
        }
        session.commit()
        return {"official_ir_url": payload.official_ir_url}


@router.post("/api/v1/research/securities/{security_id}/actuals")
def add_actual(security_id: int, payload: ActualInput, request: Request) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    from .services.research_data import record_manual_disclosure, upsert_calendar

    with _session(request) as session:
        latest = session.scalar(
            select(ResearchCalendarRevision)
            .where(
                ResearchCalendarRevision.security_id == security_id,
                ResearchCalendarRevision.event_key == payload.event_key,
            )
            .order_by(ResearchCalendarRevision.revision.desc())
            .limit(1)
        )
        if latest is None:
            raise HTTPException(404, "请先登记关联公司事件")
        period = latest.details.get("financial_period")
        if not period and latest.details.get("year") and latest.details.get("quarter"):
            period = f"{latest.details['year']}Q{latest.details['quarter']}"
        if period and period != payload.financial_period:
            raise HTTPException(422, "实际值的财务期间与关联事件不一致，请为新期间登记独立事件")
        try:
            record_manual_disclosure(
                session,
                security_id,
                title=f"{latest.title} · {payload.metric} 实际值核对",
                source_url=payload.source_url,
                published_at=payload.published_at,
                excerpt=(f"人工核对：{payload.financial_period} {payload.metric} = "
                         f"{payload.value} {payload.unit}"),
            )
            details = {
                key: value for key, value in latest.details.items() if key != "_content_fingerprint"
            }
            previous_published = details.get("actual_published_at")
            first_published = payload.published_at
            if isinstance(previous_published, str):
                try:
                    previous_time = datetime.fromisoformat(
                        previous_published.replace("Z", "+00:00")
                    )
                    if previous_time.tzinfo is not None:
                        first_published = min(first_published, previous_time)
                except ValueError:
                    pass
            for metric, actual_key in (("eps", "epsActual"), ("revenue", "revenueActual")):
                if details.get(actual_key) is not None:
                    details.setdefault(f"{metric}_source_url", latest.source_url)
                    if previous_published is not None:
                        details.setdefault(f"{metric}_published_at", previous_published)
            details.update(
                {
                    "epsActual" if payload.metric == "eps" else "revenueActual": payload.value,
                    f"{payload.metric}_unit": payload.unit,
                    f"{payload.metric}_source_url": payload.source_url,
                    f"{payload.metric}_published_at": payload.published_at.isoformat(),
                    "financial_period": payload.financial_period,
                    "actual_published_at": first_published.isoformat(),
                }
            )
            result = upsert_calendar(
                session,
                security_id=security_id,
                event_key=latest.event_key,
                title=latest.title,
                event_type=latest.event_type,
                scheduled_date=first_published.astimezone(ZoneInfo(latest.timezone)).date(),
                scheduled_at=first_published,
                timezone=latest.timezone,
                status="reported",
                source="manual_official",
                source_url=payload.source_url,
                details=details,
            )
        except (ValueError, LookupError) as exc:
            raise _http_error(exc) from exc
        from .services.analysis_versions import reconcile_research_inputs
        from .services.scoring import rebuild_signal_snapshots

        if reconcile_research_inputs(session, [security_id]):
            rebuild_signal_snapshots(session)
        session.commit()
        return _row_dict(result)


@router.get("/api/v1/research/calibration")
def get_calibration(request: Request) -> dict[str, Any]:
    from .services.action_evaluation import calibration_report

    with _session(request) as session:
        return calibration_report(session)


@router.get("/api/v1/research/securities/{security_id}")
def research_detail(security_id: int, request: Request) -> dict[str, Any]:
    from .services.financial_research import get_financial_research
    from .services.market_research import get_market_research
    from .services.research_data import get_research_data
    from .services.research_workflow import claim_page
    from .services.risk import build_risk_plan, get_risk_inputs

    settings = request.app.state.settings
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job("research-refresh") if scheduler is not None else None
    refresh_schedule = {
        "enabled": bool(job),
        "interval_hours": settings.research_refresh_interval_hours,
        "next_run_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
    }
    with _session(request) as session:
        security = session.get(Security, security_id)
        if security is None:
            raise HTTPException(404, "证券不存在")
        return {
            "security": {
                "id": security.id,
                "name": security.name,
                "symbol": security.symbol,
                "official_ir_url": (security.provider_data or {}).get("official_ir_url"),
            },
            "related_reports": [
                {"article_id": row.id, "title": row.title, "event_id": row.event_id,
                 "event_title": row.event_title, "original_source_url": row.original_source_url}
                for row in session.execute(select(
                    Article.id, Article.title, Article.original_source_url,
                    Event.id.label("event_id"), Event.title.label("event_title"),
                ).join(EventArticle, EventArticle.article_id == Article.id)
                  .join(Event, Event.id == EventArticle.event_id)
                  .where(Event.id.in_(select(EventSecurityImpact.event_id).where(
                      EventSecurityImpact.security_id == security_id,
                  ))).distinct().order_by(Event.id.desc(), Article.id.desc()).limit(100))
            ],
            "data": get_research_data(session, security_id),
            "market": get_market_research(session, security_id),
            "risk_inputs": get_risk_inputs(session, security_id),
            "risk": build_risk_plan(session, security_id),
            "refresh_schedule": refresh_schedule,
            "claims": claim_page(session, security_id)["items"],
            "financial_analysis": get_financial_research(session, security_id),
            "macro_vintages": [
                _row_dict(row)
                for row in session.scalars(
                    select(MacroObservationVintage)
                    .order_by(MacroObservationVintage.id.desc())
                    .limit(24)
                )
            ],
        }


@router.get("/research", response_class=HTMLResponse)
def research_page(
    request: Request, security_id: int | None = None,
    claims_scope: Literal["security", "all"] = "security",
    claim_status: str | None = None, before_id: int | None = Query(None, gt=0),
) -> HTMLResponse:
    with _session(request) as session:
        securities: list[dict[str, Any]] = [
            {"id": security.id, "name": security.name, "symbol": security.symbol}
            for security in session.scalars(
                select(Security)
                .join(Watchlist)
                .where(Watchlist.active.is_(True))
                .order_by(Watchlist.position)
            )
        ]
    chosen = security_id or (securities[0]["id"] if securities else None)
    detail = research_detail(chosen, request) if chosen else None
    page = get_claim_page(request, chosen if claims_scope == "security" else None,
                          limit=20, before_id=before_id, status=claim_status or None)
    pagination: dict[str, str] = {"claims_scope": claims_scope}
    if chosen:
        pagination["security_id"] = str(chosen)
    if claim_status:
        pagination["claim_status"] = claim_status
    next_url = "/research?" + urlencode({**pagination, "before_id": page["next_cursor"]}) \
        + "#claims" if page["next_cursor"] else None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="research.html",
        context={
            "securities": securities,
            "selected": chosen,
            "research": detail,
            "calibration": get_calibration(request),
            "all_claims": page["items"], "claims_scope": claims_scope,
            "claim_status": claim_status or "", "claims_next_url": next_url,
            "claims_first_url": "/research?" + urlencode(pagination) + "#claims",
        },
    )
