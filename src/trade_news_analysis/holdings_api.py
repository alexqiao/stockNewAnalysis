"""Manual account discovery and atomic imports from a local IBKR session."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .daily_bars_api import queue_daily_bar_refresh
from .models import Security
from .services.holdings import HoldingsBusyError, read_holdings
from .services.ibkr import BrokerError, IBKRReadOnlyClient

router = APIRouter(prefix="/api/v1/holdings")


class IBKRConnectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(default=7496, ge=1, le=65535)
    client_id: int = Field(default=72, ge=1, le=2147483647)

    @field_validator("port")
    @classmethod
    def real_account_port(cls, value: int) -> int:
        if value in {7497, 4002}:
            raise ValueError("请选择实盘端口，模拟账户不能覆盖真实持仓")
        return value


class IBKRSyncInput(IBKRConnectionInput):
    account_key: str = Field(pattern=r"^[a-f0-9]{64}$")


@router.get("")
def get_holdings(request: Request) -> dict[str, Any]:
    with request.app.state.session_factory() as session:
        return {**read_holdings(session), "sdk_available": IBKRReadOnlyClient.available()}


@router.post("/ibkr/accounts")
def get_accounts(payload: IBKRConnectionInput, request: Request) -> dict[str, Any]:
    try:
        accounts = request.app.state.holdings.accounts(payload.port, payload.client_id)
    except HoldingsBusyError as exc:
        raise HTTPException(409, str(exc)) from None
    except BrokerError as exc:
        raise HTTPException(503, str(exc)) from None
    return {"accounts": accounts}


@router.post("/ibkr/sync")
def sync_holdings(payload: IBKRSyncInput, request: Request) -> dict[str, Any]:
    try:
        result = request.app.state.holdings.sync(
            payload.account_key, payload.port, payload.client_id,
        )
    except HoldingsBusyError as exc:
        raise HTTPException(409, str(exc)) from None
    except BrokerError as exc:
        raise HTTPException(503, str(exc)) from None
    position_ids = [
        position["security_id"] for position in result["positions"]
        if position["security_id"] is not None and position["quantity"] != 0
    ]
    with request.app.state.session_factory() as session:
        supported_ids = list(session.scalars(select(Security.id).where(
            Security.id.in_(position_ids), Security.market.in_(["US", "HK"]),
        )))
    queue_daily_bar_refresh(request, supported_ids)
    return result
