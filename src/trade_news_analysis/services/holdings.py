"""Atomic broker imports and a shared view of the effective holdings."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionFactory
from ..holding_models import HoldingPosition, HoldingSnapshot, HoldingSyncRun
from ..models import Security, Watchlist
from .ibkr import BrokerError, BrokerPosition, BrokerSnapshot, IBKRReadOnlyClient, numeric

HOLDINGS_MAX_AGE = timedelta(hours=24)
HOLDING_FACT_FIELDS = frozenset({"current_quantity", "average_cost", "current_weight"})
# The app is deployed with one worker, like the existing pipeline coordinator.
HOLDINGS_LOCK = threading.Lock()


class HoldingsBusyError(RuntimeError):
    pass


def latest_snapshot(session: Session) -> HoldingSnapshot | None:
    return session.scalar(select(HoldingSnapshot).order_by(HoldingSnapshot.id.desc()).limit(1))


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def holding_status(quantity: float) -> str:
    return "long" if quantity > 0 else "short" if quantity < 0 else "flat"


def read_holdings(session: Session, now: datetime | None = None) -> dict[str, Any]:
    snapshot = latest_snapshot(session)
    run = session.scalar(select(HoldingSyncRun).order_by(HoldingSyncRun.id.desc()).limit(1))
    result: dict[str, Any] = {
        "source": "ibkr" if snapshot else "manual",
        "active": snapshot is not None,
        "snapshot_id": None,
        "account_key": None,
        "account_label": None,
        "captured_at": None,
        "stale": False,
        "using_cached": False,
        "warnings": [],
        "note": None,
        "portfolio": None,
        "positions": [],
        "blockers": [],
        "data_gaps": [],
        "last_sync": {
            "id": run.id,
            "status": run.status,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error": run.error,
        }
        if run
        else None,
    }
    if snapshot is None:
        return result
    current = utc(now or datetime.now(UTC))
    stale = current - utc(snapshot.captured_at) > HOLDINGS_MAX_AGE
    blockers = []
    warnings = []
    if utc(snapshot.captured_at) > current + timedelta(minutes=5):
        blockers.append("真实持仓快照时间异常，请重新同步")
    if stale:
        warnings.append("快照超过 24 小时")
    if run and run.id > snapshot.run_id and run.status in {"failed", "running"}:
        warnings.append("最近同步失败" if run.status == "failed" else "新一轮同步进行中")
    # Imports are atomic: a later attempt must not invalidate the last successful snapshot.
    note = (
        f"持仓沿用 {utc(snapshot.captured_at):%Y-%m-%d %H:%M} UTC 成功快照"
        + (f"（{'；'.join(warnings)}）" if warnings else "")
        + "；仓位、资金按该快照估算。"
        if not blockers else "持仓快照时间异常，相关计算已暂停。"
    )
    positions = session.scalars(
        select(HoldingPosition)
        .where(HoldingPosition.snapshot_id == snapshot.id)
        .order_by(HoldingPosition.id)
    ).all()
    rows = []
    for position in positions:
        row = {column.name: getattr(position, column.name) for column in position.__table__.columns}
        row["holding_status"] = holding_status(position.quantity)
        rows.append(row)
    settled, funds = snapshot.settled_cash, snapshot.available_funds
    available_cash = (
        max(0.0, min(settled, funds)) if (settled is not None and funds is not None) else None
    )
    result.update(
        {
            "snapshot_id": snapshot.id,
            "account_key": snapshot.account_key,
            "account_label": snapshot.account_label,
            "captured_at": snapshot.captured_at,
            "stale": stale,
            "using_cached": bool(warnings) and not blockers,
            "warnings": warnings,
            "note": note,
            "positions": rows,
            "blockers": blockers,
            "data_gaps": snapshot.data_gaps,
            "portfolio": {
                "total_value": snapshot.net_liquidation,
                "currency": snapshot.currency,
                "cash_balance": snapshot.cash_balance,
                "settled_cash": settled,
                "available_funds": funds,
                "available_cash": available_cash,
            },
        }
    )
    return result


def effective_facts(state: dict[str, Any], security_id: int) -> dict[str, Any]:
    position = next(
        (item for item in state["positions"] if item["security_id"] == security_id), None
    )
    return {
        "current_quantity": position["quantity"] if position else 0.0,
        "average_cost": position["average_cost"] if position else None,
        "current_weight": position["weight"] if position else 0.0,
        "holding_status": position["holding_status"] if position else "flat",
    }


US_EXCHANGES = {
    "NASDAQ",
    "NASDAQ.NMS",
    "NASDAQ.NGS",
    "NASDAQ.NCM",
    "ISLAND",
    "NASD",
    "NMS",
    "NGM",
    "NCM",
    "NYSE",
    "ARCA",
    "NYSEARCA",
    "AMEX",
    "BATS",
    "CBOE",
    "IEX",
    "EDGEA",
    "EDGX",
    "PINK",
    "OTCQB",
    "OTCQX",
    "OTC",
}


def security_identity(position: BrokerPosition) -> tuple[str, str, str] | None:
    exchange, symbol = position.exchange.upper(), position.symbol.upper().strip()
    if position.security_type not in {"STK", "ETF"}:
        return None
    if exchange in US_EXCHANGES:
        exchange = (
            "NASDAQ"
            if exchange
            in {"NASDAQ.NMS", "NASDAQ.NGS", "NASDAQ.NCM", "ISLAND", "NASD", "NMS", "NGM", "NCM"}
            else "NYSEARCA"
            if exchange == "ARCA"
            else exchange
        )
        return "US", exchange, symbol.replace(" ", ".")
    if exchange in {"SEHK", "HK"} and symbol.isdigit():
        return "HK", "HK", f"{int(symbol):05d}.HK"
    if exchange in {"SEHKNTL", "SEHKSZSE", "SSE", "SZSE"} and symbol.isdigit():
        exchange = "SH" if exchange in {"SEHKNTL", "SSE"} else "SZ"
        return "A", exchange, f"{int(symbol):06d}.{exchange}"
    # SMART is a routing destination, not enough evidence of a listing market.
    if exchange in {"", "SMART"}:
        raise BrokerError("股票合约缺少上市交易所，无法确认市场，原持仓已保留")
    return None


def resolve_security(session: Session, position: BrokerPosition) -> Security | None:
    identity = security_identity(position)
    if identity is None:
        return None
    market, exchange, symbol = identity
    securities = list(session.scalars(select(Security)))
    linked = [
        s for s in securities if (s.provider_data or {}).get("ibkr_con_id") == position.con_id
    ]
    if len(linked) > 1:
        raise BrokerError("IBKR 合约关联了多个证券，原持仓已保留")
    if linked:
        security = linked[0]
        if security.market != market or security.currency != position.currency:
            raise BrokerError("IBKR 合约与已有证券市场或币种不一致，原持仓已保留")
        return security
    normalized = symbol.replace("-", ".") if market == "US" else symbol
    candidates = [
        s
        for s in securities
        if s.market == market
        and (s.symbol.upper().replace("-", ".") if market == "US" else s.symbol.upper())
        == normalized
    ]
    exact = [s for s in candidates if s.exchange.upper() == exchange]
    matches = exact or candidates
    if len(matches) > 1:
        raise BrokerError("持仓代码对应多个证券，无法唯一匹配，原持仓已保留")
    if matches:
        security = matches[0]
        old_id = (security.provider_data or {}).get("ibkr_con_id")
        if security.currency != position.currency or old_id not in {None, position.con_id}:
            raise BrokerError("持仓与已有证券的合约或币种冲突，原持仓已保留")
    else:
        timezone, calendar = {
            "US": ("America/New_York", "US"),
            "HK": ("Asia/Hong_Kong", "HK"),
            "A": ("Asia/Shanghai", "CN"),
        }[market]
        security = Security(
            market=market,
            exchange=exchange,
            symbol=symbol,
            name=position.name or symbol,
            currency=position.currency,
            timezone=timezone,
            calendar=calendar,
        )
        session.add(security)
        session.flush()
    security.provider_data = {**(security.provider_data or {}), "ibkr_con_id": position.con_id}
    return security


def save_snapshot(session: Session, run: HoldingSyncRun, data: BrokerSnapshot) -> None:
    if data.account_key != run.account_key:
        raise BrokerError("读取结果与所选账户不一致，原持仓已保留")
    snapshot = HoldingSnapshot(
        run_id=run.id,
        account_key=data.account_key,
        account_label=data.account_label,
        captured_at=data.captured_at,
        currency=data.currency,
        net_liquidation=numeric(data.net_liquidation),
        cash_balance=numeric(data.cash_balance),
        settled_cash=numeric(data.settled_cash),
        available_funds=numeric(data.available_funds),
        exchange_rates=data.exchange_rates,
        data_gaps=list(data.data_gaps),
    )
    session.add(snapshot)
    session.flush()
    gaps = list(data.data_gaps)
    for field, label in (
        ("net_liquidation", "账户净值"),
        ("cash_balance", "现金余额"),
        ("settled_cash", "已结算现金"),
        ("available_funds", "可用资金"),
    ):
        if getattr(snapshot, field) is None:
            gaps.append(f"缺少{label}")
    states: dict[int, str] = {}
    contracts: set[int] = set()
    for position in data.positions:
        if numeric(position.quantity) is None or position.con_id <= 0:
            raise BrokerError("无效持仓数量或合约，原持仓已保留")
        if position.con_id in contracts:
            raise BrokerError("收到重复持仓合约，原持仓已保留")
        contracts.add(position.con_id)
        if position.quantity == 0:
            continue
        security = resolve_security(session, position)
        if security is not None and security.id in states:
            raise BrokerError("多个持仓合约映射到同一证券，原持仓已保留")
        fields = asdict(position)
        for field in ("average_cost", "market_price", "market_value", "unrealized_pnl"):
            fields[field] = numeric(fields[field])
        for field in ("average_cost", "market_price"):
            if fields[field] is not None and fields[field] <= 0:
                fields[field] = None
        value = fields["market_value"]
        rate = (
            1.0
            if position.currency == data.currency
            else numeric(data.exchange_rates.get(position.currency))
        )
        nav = snapshot.net_liquidation
        weight = (
            value * rate / nav
            if (value is not None and rate is not None and rate > 0 and nav is not None and nav > 0)
            else None
        )
        reason = None if security else "暂不支持此资产类型或上市市场"
        if fields["market_price"] is None or value is None:
            gaps.append(f"{position.symbol} 缺少持仓行情")
        if weight is None:
            gaps.append(f"{position.symbol} 缺少有效净值、市值或汇率，仓位比例未知")
        if reason:
            gaps.append(f"{position.symbol}：{reason}")
        session.add(
            HoldingPosition(
                snapshot_id=snapshot.id,
                security_id=security.id if security else None,
                weight=weight,
                unsupported_reason=reason,
                **fields,
            )
        )
        if security:
            states[security.id] = holding_status(position.quantity)
    snapshot.data_gaps = gaps
    entries = list(session.scalars(select(Watchlist).order_by(Watchlist.position, Watchlist.id)))
    for entry in entries:
        entry.holding_status = states.get(entry.security_id, "flat")
    existing = {entry.security_id for entry in entries}
    order = max((entry.position for entry in entries), default=-1) + 1
    for security_id, state in states.items():
        if security_id not in existing:
            session.add(Watchlist(security_id=security_id, position=order, holding_status=state))
            order += 1
    run.status = "completed"
    run.finished_at = datetime.now(UTC)


class HoldingService:
    def __init__(
        self,
        session_factory: SessionFactory,
        client_factory: Callable[..., Any] = IBKRReadOnlyClient,
    ) -> None:
        self.session_factory = session_factory
        self.client_factory = client_factory

    def recover_interrupted(self) -> None:
        """Recover only at application startup, never when constructing a reader."""
        with self.session_factory() as session:
            interrupted = session.scalars(
                select(HoldingSyncRun).where(HoldingSyncRun.status == "running")
            ).all()
            for run in interrupted:
                run.status = "failed"
                run.error = "服务重启中断了同步，请重新同步"
                run.finished_at = datetime.now(UTC)
            session.commit()

    def accounts(self, port: int, client_id: int) -> list[dict[str, str]]:
        if not HOLDINGS_LOCK.acquire(blocking=False):
            raise HoldingsBusyError("持仓连接或同步正在进行，请稍后重试")
        try:
            with self.client_factory(port=port, client_id=client_id) as client:
                return client.accounts()
        finally:
            HOLDINGS_LOCK.release()

    def sync(self, key: str, port: int, client_id: int) -> dict[str, Any]:
        if not HOLDINGS_LOCK.acquire(blocking=False):
            raise HoldingsBusyError("持仓连接或同步正在进行，请稍后重试")
        run_id = None
        run: HoldingSyncRun | None = None
        try:
            with self.session_factory() as session:
                run = HoldingSyncRun(account_key=key)
                session.add(run)
                session.commit()
                run_id = run.id
            with self.client_factory(port=port, client_id=client_id) as client:
                data = client.snapshot(key)
            with self.session_factory() as session:
                run = session.get(HoldingSyncRun, run_id)
                assert run is not None
                save_snapshot(session, run, data)
                session.commit()
                return read_holdings(session)
        except Exception as exc:
            message = str(exc) if isinstance(exc, BrokerError) else "持仓同步失败，原持仓已保留"
            if run_id is not None:
                with self.session_factory() as session:
                    run = session.get(HoldingSyncRun, run_id)
                    assert run is not None
                    run.status, run.error = "failed", message[:500]
                    run.finished_at = datetime.now(UTC)
                    session.commit()
            raise BrokerError(message) from None
        finally:
            HOLDINGS_LOCK.release()
