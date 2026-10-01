"""Cached daily candles and explicitly queued, isolated market-data refreshes."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .models import Security
from .services.bollinger_context import get_bollinger_reference
from .services.bollinger_reference import BollingerParameters
from .services.coordinator import PipelineBusyError
from .services.daily_bars import get_daily_bars

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/securities")


class DailyBarOutput(BaseModel):
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None = None
    adj_open: float | None = None
    adj_high: float | None = None
    adj_low: float | None = None
    adj_close: float | None = None


class DailyBarCoverage(BaseModel):
    start: date | None = None
    end: date | None = None


class DailyBarsOutput(BaseModel):
    security_id: int
    market: str
    symbol: str
    source: str
    currency: str
    timezone: str
    volume_unit: Literal["shares"]
    available_adjustments: list[Literal["split_adjusted", "total_return_adjusted"]]
    bars: list[DailyBarOutput]
    coverage: DailyBarCoverage
    latest_trade_date: date | None
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    next_retry_at: datetime | None
    sync_status: str
    error: str | None
    needs_refresh: bool
    stale: bool


class BollingerQuery(BollingerParameters):
    adjustment: Literal["split_adjusted", "total_return_adjusted"] = "total_return_adjusted"


def _require_supported(security: Security | None) -> Security:
    if security is None:
        raise HTTPException(404, "证券不存在")
    if security.market not in {"US", "HK"}:
        raise HTTPException(422, "日 K 线目前支持美股和港股")
    return security


@router.get("/{security_id}/daily-bars", response_model=DailyBarsOutput)
def read_daily_bars(
    security_id: int, request: Request, start: date | None = None, end: date | None = None,
) -> dict[str, Any]:
    if start is not None and end is not None and start > end:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    with request.app.state.session_factory() as session:
        security = _require_supported(session.get(Security, security_id))
        return get_daily_bars(session, security, start=start, end=end)


@router.get("/{security_id}/bollinger-reference")
def read_bollinger_reference(
    security_id: int, request: Request,
    parameters: Annotated[BollingerQuery, Query()],
) -> dict[str, Any]:
    with request.app.state.session_factory() as session:
        security = _require_supported(session.get(Security, security_id))
        strategy = BollingerParameters(**parameters.model_dump(exclude={"adjustment"}))
        return get_bollinger_reference(session, security, strategy, parameters.adjustment)


@router.post("/{security_id}/daily-bars/refresh", status_code=202)
def refresh_daily_bars(
    security_id: int, request: Request, force: bool = False,
) -> dict[str, Any]:
    with request.app.state.session_factory() as session:
        security = _require_supported(session.get(Security, security_id))
        if not security.active:
            raise HTTPException(422, "证券已停用，仍可查看已保存日线")
    try:
        run_id = request.app.state.coordinator.submit_daily_bars([security_id], force=force)
    except PipelineBusyError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"run_id": run_id, "status": "queued"}


def queue_daily_bar_refresh(request: Request, security_ids: list[int]) -> None:
    """A committed watchlist/holding change must survive a queueing failure."""
    if not request.app.state.settings.daily_bars_enabled or not security_ids:
        return
    try:
        request.app.state.coordinator.submit_daily_bars(sorted(set(security_ids)))
    except Exception as exc:
        logger.warning("日线后台任务未入队，下次调度将补漏：%s", type(exc).__name__)
