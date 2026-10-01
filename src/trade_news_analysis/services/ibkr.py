"""Small, short-lived read-only façade over the official TWS callback API."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.wrapper import EWrapper

    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

    class EWrapper:  # type: ignore[no-redef]
        pass

    class EClient:  # type: ignore[no-redef]
        def __init__(self, wrapper: Any) -> None:
            self.wrapper = wrapper

    Contract = Any


class BrokerError(RuntimeError):
    """Messages are safe to display: never include raw broker replies or account IDs."""


def account_key(account: str) -> str:
    return hashlib.sha256(("ibkr:" + account).encode()).hexdigest()


def account_label(account: str) -> str:
    return "IBKR ••••" + account[-4:]


def is_live_account(account: str) -> bool:
    return account.startswith("U") and account[1:].isdigit()


def numeric(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # TWS uses very large finite sentinels as well as missing values.
    return result if math.isfinite(result) and abs(result) < 1e100 else None


@dataclass(frozen=True)
class BrokerPosition:
    con_id: int
    symbol: str
    security_type: str
    exchange: str
    currency: str
    quantity: float
    average_cost: float | None
    name: str = ""
    market_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None


@dataclass(frozen=True)
class BrokerSnapshot:
    account_key: str
    account_label: str
    captured_at: datetime
    currency: str | None
    net_liquidation: float | None
    cash_balance: float | None
    settled_cash: float | None
    available_funds: float | None
    positions: list[BrokerPosition]
    exchange_rates: dict[str, float] = field(default_factory=dict)
    data_gaps: list[str] = field(default_factory=list)


class _CallbackApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.accounts_done = threading.Event()
        self.positions_done = threading.Event()
        self.summary_done = threading.Event()
        self.download_done = threading.Event()
        self.failed = threading.Event()
        self.failure = "IBKR 连接中断，请重新同步"
        self.accounts: list[str] = []
        self.selected_account = ""
        self.positions: dict[tuple[str, int], tuple[Any, float, float | None]] = {}
        self.portfolio: dict[int, tuple[float, float | None, float | None, float | None]] = {}
        self.summary: dict[tuple[str, str], str] = {}
        self.values: dict[tuple[str, str], str] = {}
        self.contract_events: dict[int, threading.Event] = {}
        self.contracts: dict[int, list[Any]] = {}

    def nextValidId(self, orderId: int) -> None:  # noqa: N802
        self.ready.set()

    def managedAccounts(self, accountsList: str) -> None:  # noqa: N802
        self.accounts = [item.strip() for item in accountsList.split(",") if item.strip()]
        self.accounts_done.set()

    def position(self, account: str, contract: Any, position: Decimal, avgCost: float) -> None:
        quantity = numeric(position)
        if quantity is None or not getattr(contract, "conId", 0):
            self.failure = "IBKR 返回无效持仓数量或合约，原持仓已保留"
            self.failed.set()
            return
        self.positions[(account, contract.conId)] = (contract, quantity, numeric(avgCost))

    def positionEnd(self) -> None:  # noqa: N802
        self.positions_done.set()

    def accountSummary(  # noqa: N802
        self, reqId: int, account: str, tag: str, value: str, currency: str
    ) -> None:
        if account == self.selected_account:
            self.summary[(tag, currency)] = value

    def accountSummaryEnd(self, reqId: int) -> None:  # noqa: N802
        self.summary_done.set()

    def updateAccountValue(  # noqa: N802
        self, key: str, val: str, currency: str, accountName: str
    ) -> None:
        if accountName != self.selected_account:
            return
        # Newer TWS can prefix per-currency ledger keys via an API setting.
        key = key.removeprefix("$LEDGER-")
        if key.lower() == "accountready" and val.lower() == "false":
            self.failure = "IBKR 账户尚未就绪，原持仓已保留"
            self.failed.set()
        self.values[(key, currency)] = val

    def updatePortfolio(  # noqa: N802
        self,
        contract: Any,
        position: Decimal,
        marketPrice: float,
        marketValue: float,
        averageCost: float,
        unrealizedPNL: float,
        realizedPNL: float,
        accountName: str,
    ) -> None:
        if accountName == self.selected_account:
            quantity = numeric(position)
            if quantity is None:
                self.failed.set()
                return
            self.portfolio[contract.conId] = (
                quantity,
                numeric(marketPrice),
                numeric(marketValue),
                numeric(unrealizedPNL),
            )

    def accountDownloadEnd(self, accountName: str) -> None:  # noqa: N802
        if accountName == self.selected_account:
            self.download_done.set()

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:  # noqa: N802
        self.contracts.setdefault(reqId, []).append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802
        if reqId in self.contract_events:
            self.contract_events[reqId].set()

    def connectionClosed(self) -> None:  # noqa: N802
        self.failed.set()

    def error(  # noqa: N802
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        if errorCode in {2104, 2106, 2107, 2108, 2158}:
            return
        self.failure = {
            326: "IBKR Client ID 已被占用，请更换后重试",
            502: "无法连接 IBKR，请检查 TWS / Gateway 和 Socket 端口",
            2100: "IBKR 账户订阅被其他客户端替换，请重试",
        }.get(errorCode, f"IBKR 返回错误 {errorCode}，本次未更新持仓")
        self.failed.set()


class IBKRReadOnlyClient:
    SUMMARY_TAGS = "NetLiquidation,TotalCashValue,SettledCash,AvailableFunds"

    def __init__(self, port: int = 7496, client_id: int = 72, timeout: float = 45) -> None:
        self.port = port
        self.client_id = client_id
        self.timeout = timeout
        self._app: _CallbackApp | None = None
        self._thread: threading.Thread | None = None
        self._deadline = 0.0

    @staticmethod
    def available() -> bool:
        return SDK_AVAILABLE

    def __enter__(self) -> IBKRReadOnlyClient:
        if not self.available():
            raise BrokerError("未安装官方 ibapi，请按 README 安装 TWS Python API")
        if self.port in {7497, 4002}:
            raise BrokerError("请选择实盘端口，模拟账户不能覆盖真实持仓")
        app = _CallbackApp()
        self._app = app
        self._deadline = time.monotonic() + self.timeout
        try:
            app.connect("127.0.0.1", self.port, self.client_id)
            self._thread = threading.Thread(target=app.run, name="ibkr-holdings", daemon=True)
            self._thread.start()
            self._wait(app.ready)
            self._wait(app.accounts_done)
        except Exception as exc:
            self.close()
            if isinstance(exc, BrokerError):
                raise
            raise BrokerError("无法连接 IBKR，请检查本机 TWS / Gateway 和端口") from None
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._app is not None:
            self._app.disconnect()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._app = None
        self._thread = None

    def _wait(self, event: threading.Event) -> None:
        assert self._app is not None
        while not event.is_set():
            if self._app.failed.is_set():
                raise BrokerError(self._app.failure)
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("IBKR 数据读取超时，未收到完整回调，原持仓已保留")
            event.wait(min(0.05, remaining))
        if self._app.failed.is_set():
            raise BrokerError(self._app.failure)

    def accounts(self) -> list[dict[str, str]]:
        assert self._app is not None
        return [
            {"account_key": account_key(account), "label": account_label(account)}
            for account in self._app.accounts
            if is_live_account(account)
        ]

    def snapshot(self, selected_key: str) -> BrokerSnapshot:
        app = self._app
        assert app is not None
        selected = next(
            (
                account
                for account in app.accounts
                if is_live_account(account) and account_key(account) == selected_key
            ),
            None,
        )
        if selected is None:
            raise BrokerError("所选实盘账户不在当前连接中，请重新读取账户")
        app.selected_account = selected
        try:
            app.reqPositions()
            app.reqAccountUpdates(True, selected)
            app.reqAccountSummary(9001, "All", self.SUMMARY_TAGS)
            for event in (app.positions_done, app.summary_done, app.download_done):
                self._wait(event)
            positions = self._positions(selected)
            currencies = {
                currency
                for (tag, currency) in app.summary
                if tag == "NetLiquidation" and len(currency) == 3 and currency != "BASE"
            }
            if len(currencies) != 1:
                raise BrokerError("IBKR 未明确返回账户基础币种，原持仓已保留")
            base_currency = currencies.pop()

            def summary(tag: str) -> float | None:
                return numeric(app.summary.get((tag, base_currency)))

            rates = {
                currency: value
                for (tag, currency), raw in app.values.items()
                if tag == "ExchangeRate" and len(currency) == 3
                and (value := numeric(raw)) is not None and value > 0
            }
            rates[base_currency] = 1.0
            self._wait(app.download_done)
            return BrokerSnapshot(
                account_key=selected_key,
                account_label=account_label(selected),
                captured_at=datetime.now(UTC),
                currency=base_currency,
                net_liquidation=summary("NetLiquidation"),
                cash_balance=summary("TotalCashValue"),
                settled_cash=summary("SettledCash"),
                available_funds=summary("AvailableFunds"),
                positions=positions,
                exchange_rates=rates,
            )
        finally:
            # Each cancellation is attempted even if an earlier one fails.
            for cancel in (
                app.cancelPositions,
                lambda: app.cancelAccountSummary(9001),
                lambda: app.reqAccountUpdates(False, selected),
            ):
                try:
                    cancel()
                except Exception:
                    pass

    def _positions(self, selected: str) -> list[BrokerPosition]:
        app = self._app
        assert app is not None
        # Contract requests can receive further portfolio callbacks. Work from copies,
        # then reject quantity changes instead of combining different account states.
        rows = dict(app.positions)
        result = []
        for (account, con_id), (contract, quantity, cost) in rows.items():
            if account != selected or quantity == 0:
                continue
            name = ""
            if contract.secType in {"STK", "ETF"}:
                req_id = len(app.contract_events) + 10000
                app.contract_events[req_id] = threading.Event()
                query = Contract()
                query.conId = con_id
                app.reqContractDetails(req_id, query)
                self._wait(app.contract_events[req_id])
                details = app.contracts.get(req_id, [])
                if len(details) != 1 or details[0].contract.conId != con_id:
                    raise BrokerError("IBKR 合约信息缺失或有歧义，原持仓已保留")
                contract = details[0].contract
                name = details[0].longName
            portfolio = app.portfolio.get(con_id)
            if portfolio is not None and portfolio[0] != quantity:
                raise BrokerError("读取期间持仓发生变化，请重新同步")
            price, value, pnl = portfolio[1:] if portfolio else (None, None, None)
            result.append(
                BrokerPosition(
                    con_id=con_id,
                    symbol=contract.symbol,
                    security_type=contract.secType,
                    exchange=contract.primaryExchange or contract.exchange,
                    currency=contract.currency,
                    quantity=quantity,
                    average_cost=cost,
                    name=name,
                    market_price=price,
                    market_value=value,
                    unrealized_pnl=pnl,
                )
            )
        if rows != app.positions:
            raise BrokerError("读取期间持仓发生变化，请重新同步")
        return result
