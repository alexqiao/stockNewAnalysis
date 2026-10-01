from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pytest import MonkeyPatch

from trade_news_analysis.services import ibkr
from trade_news_analysis.services.ibkr import (
    BrokerError,
    IBKRReadOnlyClient,
    _CallbackApp,
    account_key,
    numeric,
)


def test_facade_has_no_trading_methods_and_no_remote_host_input() -> None:
    names = set(dir(IBKRReadOnlyClient))
    assert not names & {"placeOrder", "cancelOrder", "reqOpenOrders", "place_order"}
    with pytest.raises(TypeError):
        IBKRReadOnlyClient(host="192.168.1.2")  # type: ignore[call-arg]


def test_optional_sdk_missing_is_actionable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(ibkr, "SDK_AVAILABLE", False)
    with pytest.raises(BrokerError, match="未安装"):
        with IBKRReadOnlyClient():
            pass


def test_paper_accounts_never_selected() -> None:
    client = IBKRReadOnlyClient()
    client._app = _CallbackApp()
    client._app.accounts = ["DU1234567", "U7654321"]
    assert client.accounts() == [{"account_key": account_key("U7654321"), "label": "IBKR ••••4321"}]
    with pytest.raises(BrokerError, match="所选实盘账户"):
        client.snapshot(account_key("DU1234567"))


def test_callback_deduplicates_and_filters_accounts() -> None:
    app = _CallbackApp()
    app.selected_account = "U1234567"
    contract = SimpleNamespace(conId=1)
    app.position("U1234567", contract, Decimal("1.5"), 100)
    app.position("U1234567", contract, Decimal("2.5"), 100)
    assert len(app.positions) == 1
    assert app.positions[("U1234567", 1)][1] == 2.5
    app.updateAccountValue("NetLiquidation", "10", "USD", "U9999999")
    assert app.values == {}
    app.updateAccountValue("$LEDGER-ExchangeRate", "0.128", "HKD", "U1234567")
    assert app.values[("ExchangeRate", "HKD")] == "0.128"
    app.accountDownloadEnd("U9999999")
    assert not app.download_done.is_set()
    app.updateAccountValue("AccountReady", "false", "", "U1234567")
    assert app.failed.is_set()


def test_error_messages_do_not_expose_raw_broker_details() -> None:
    app = _CallbackApp()
    app.error(-1, 123456789, 2104, "info", "")
    assert not app.failed.is_set()
    app.error(-1, 123456789, 326, "U1234567 raw account payload", "")
    assert app.failed.is_set()
    assert "Client ID" in app.failure
    assert "U1234567" not in app.failure


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 1.7976931348623157e308, None, "bad"])
def test_unset_numbers_do_not_become_zero(value: Any) -> None:
    assert numeric(value) is None
    assert numeric(0) == 0


def callback_client(
    monkeypatch: MonkeyPatch, *, complete: bool = True
) -> tuple[IBKRReadOnlyClient, list[str]]:
    client = IBKRReadOnlyClient(timeout=0)
    app = _CallbackApp()
    client._app = app
    app.accounts = ["U1234567"]
    app.positions_done.set()
    app.summary_done.set()
    if complete:
        app.download_done.set()
    app.summary = {
        ("NetLiquidation", "USD"): "100000",
        ("TotalCashValue", "USD"): "20000",
        ("SettledCash", "USD"): "15000",
        ("AvailableFunds", "USD"): "10000",
    }
    app.values = {("ExchangeRate", "HKD"): "0.128"}
    calls = []
    monkeypatch.setattr(app, "reqPositions", lambda: None)
    monkeypatch.setattr(app, "reqAccountSummary", lambda *_: None)
    monkeypatch.setattr(app, "reqAccountUpdates", lambda enabled, _: calls.append(str(enabled)))
    monkeypatch.setattr(app, "cancelPositions", lambda: calls.append("positions"))
    monkeypatch.setattr(app, "cancelAccountSummary", lambda _: calls.append("summary"))
    return client, calls


def test_complete_empty_account_is_distinct_from_timeout(monkeypatch: MonkeyPatch) -> None:
    client, calls = callback_client(monkeypatch)
    data = client.snapshot(account_key("U1234567"))
    assert data.positions == []
    assert data.net_liquidation == 100000
    assert data.exchange_rates["HKD"] == 0.128
    assert calls == ["True", "positions", "summary", "False"]


def test_incomplete_download_fails_and_cleans_all_subscriptions(monkeypatch: MonkeyPatch) -> None:
    client, calls = callback_client(monkeypatch, complete=False)
    with pytest.raises(BrokerError, match="完整回调"):
        client.snapshot(account_key("U1234567"))
    assert calls == ["True", "positions", "summary", "False"]


def test_account_not_ready_and_disconnect_fail_before_accepting_snapshot(
    monkeypatch: MonkeyPatch,
) -> None:
    client, _ = callback_client(monkeypatch)
    client._app.connectionClosed()
    with pytest.raises(BrokerError, match="连接中断"):
        client.snapshot(account_key("U1234567"))


def test_changed_quantity_between_callbacks_rejected(monkeypatch: MonkeyPatch) -> None:
    client, _ = callback_client(monkeypatch)
    contract = SimpleNamespace(
        conId=5,
        symbol="SPY",
        secType="STK",
        primaryExchange="ARCA",
        exchange="SMART",
        currency="USD",
    )
    app = client._app
    app.positions[("U1234567", 5)] = (contract, 10.0, 100.0)
    app.portfolio[5] = (11.0, 100.0, 1100.0, 0.0)

    def details(req_id: int, _query: Any) -> None:
        app.contractDetails(req_id, SimpleNamespace(contract=contract, longName="SPY"))
        app.contractDetailsEnd(req_id)

    monkeypatch.setattr(app, "reqContractDetails", details)
    monkeypatch.setattr(ibkr, "Contract", lambda: SimpleNamespace())
    with pytest.raises(BrokerError, match="持仓发生变化"):
        client.snapshot(account_key("U1234567"))
