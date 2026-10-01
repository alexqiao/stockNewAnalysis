from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pandas as pd
import pytest
from pydantic import SecretStr

from trade_news_analysis.config import Settings
from trade_news_analysis.services import providers


@pytest.fixture
def clock(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> list[float]:
    value = [1000.0]
    monkeypatch.setattr(providers.logger, "disabled", False)
    monkeypatch.setattr(providers.logger, "propagate", True)
    caplog.set_level(logging.WARNING, logger=providers.logger.name)
    monkeypatch.setattr(providers, "_tushare_gates", {})
    monkeypatch.setattr(providers, "monotonic", lambda: value[0])
    return value


def response(code: int = 0, message: str = "") -> io.BytesIO:
    return io.BytesIO(json.dumps({
        "code": code, "msg": message,
        "data": {"fields": ["close"], "items": [[100]]},
    }).encode())


@pytest.mark.parametrize(("message", "seconds"), [
    ("抱歉，您访问接口(us_daily)频率超限(1次/小时)", 3600),
    ("抱歉，您访问接口(us_daily)频率超限(1次/分钟)", 60),
    ("抱歉，每天最多访问该接口100次", 86400),
    ("频率超限：1次/分钟，10次/小时", 3600),
    ("rate limit exceeded: 1 request per hour", 3600),
    ("too many requests", 60),
    ("not authorized", None),
    ("empty response", None),
])
def test_reported_rate_window_is_respected(message: str, seconds: float | None) -> None:
    assert providers._tushare_rate_limit_seconds(message) == seconds


class Fallback:
    name = "fixture-yahoo"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.frame = pd.DataFrame({"Close": [100]}, index=pd.to_datetime(["2020-01-02"]))
        self.frame.attrs["source"] = "fixture-yahoo"

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        self.calls.append(symbol)
        return self.frame

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        return self.history(market, "SPY", period)


@pytest.mark.parametrize("benchmark_first", [False, True])
def test_hour_cooldown_shared_by_stocks_benchmark_and_new_instances(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, clock: list[float],
    caplog: pytest.LogCaptureFixture, benchmark_first: bool,
) -> None:
    calls = []

    def limited(request: Request, **_kwargs: Any) -> io.BytesIO:
        calls.append(json.loads(request.data or b"{}"))
        return response(-2001, "us_daily频率超限(1次/小时)，凭据不得输出：fixture-secret")

    monkeypatch.setattr(providers, "urlopen", limited)
    settings.tushare_token = SecretStr("fixture-secret")
    fallback = Fallback()

    def build() -> providers.FallbackMarketDataProvider:
        return providers.FallbackMarketDataProvider(
            providers.TushareProvider(settings), fallback, frozenset({"US", "HK"}),
        )

    first = build()
    if benchmark_first:
        first.benchmark_history("US")
    else:
        first.history("US", "AMZN")
    clock[0] += 61
    frame = build().history("US", "NVDA")
    first.benchmark_history("US")
    assert len(calls) == 1
    assert calls[0]["api_name"] == "us_daily"
    assert len(fallback.calls) == 3
    assert frame.index[0] == pd.Timestamp("2020-01-02")
    assert frame.attrs["source"] == "fixture-yahoo"
    assert frame.attrs["provider_fallback"]["retry_after_seconds"] == 3540
    assert "provider_fallback" not in fallback.frame.attrs
    assert len(caplog.records) == 1
    assert "fixture-secret" not in caplog.text


def test_cooldown_expires_and_failed_retry_uses_new_limit_then_recovers(
    monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    messages = iter(["频率超限(1次/分钟)", "频率超限(1次/小时)", ""])
    calls = []

    def fetch(_request: Request, **_kwargs: Any) -> io.BytesIO:
        message = next(messages)
        calls.append(message)
        return response(-2001 if message else 0, message)

    monkeypatch.setattr(providers, "urlopen", fetch)
    client = providers.TushareClient("one-account", 1)
    with pytest.raises(providers.TushareRateLimitError):
        client.query("us_daily")
    clock[0] += 60
    with pytest.raises(providers.TushareRateLimitError):
        client.query("us_daily")
    assert len(calls) == 1
    clock[0] += 1
    with pytest.raises(providers.TushareRateLimitError) as error:
        client.query("us_daily")
    assert error.value.retry_after_seconds == 3601
    clock[0] += 3601
    assert providers.TushareClient("one-account", 1).query("us_daily") == [{"close": 100}]
    assert len(calls) == 3


def test_credentials_and_api_quotas_are_independent(
    monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    calls = []

    def fetch(request: Request, **_kwargs: Any) -> io.BytesIO:
        body = json.loads(request.data or b"{}")
        calls.append(body)
        if body["token"] == "first" and body["api_name"] == "us_daily":
            return response(-2001, "频率超限(1次/小时)")
        return response()

    monkeypatch.setattr(providers, "urlopen", fetch)
    with pytest.raises(providers.TushareRateLimitError):
        providers.TushareClient("first", 1).query("us_daily")
    assert providers.TushareClient("second", 1).query("us_daily")
    assert providers.TushareClient("first", 1).query("hk_daily")
    assert providers.TushareClient("first", 1).query("daily")
    assert len(calls) == 4


def test_non_limit_failure_does_not_block_the_next_request(
    monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    bodies = iter([response(-2001, "invalid symbol"), response()])
    monkeypatch.setattr(providers, "urlopen", lambda *_args, **_kwargs: next(bodies))
    client = providers.TushareClient("one-account", 1)
    with pytest.raises(RuntimeError, match="invalid symbol"):
        client.query("us_daily")
    assert client.query("us_daily")


def test_http_429_uses_retry_after_and_does_not_repeat_request(
    monkeypatch: pytest.MonkeyPatch, clock: list[float],
) -> None:
    calls = []

    def fetch(_request: Request, **_kwargs: Any) -> io.BytesIO:
        calls.append(1)
        raise HTTPError("https://api.tushare.pro", 429, "limited", {"Retry-After": "120"}, None)

    monkeypatch.setattr(providers, "urlopen", fetch)
    client = providers.TushareClient("one-account", 1)
    for _ in range(2):
        with pytest.raises(providers.TushareRateLimitError) as error:
            client.query("us_daily")
        assert error.value.retry_after_seconds == 121
    assert len(calls) == 1


def test_concurrent_clients_share_one_limit_response_and_warning(
    monkeypatch: pytest.MonkeyPatch, clock: list[float], caplog: pytest.LogCaptureFixture,
) -> None:
    calls = []

    def fetch(_request: Request, **_kwargs: Any) -> io.BytesIO:
        calls.append(1)
        return response(-2001, "频率超限(1次/小时)")

    def query(_index: int) -> None:
        with pytest.raises(providers.TushareRateLimitError):
            providers.TushareClient("one-account", 1).query("us_daily")

    monkeypatch.setattr(providers, "urlopen", fetch)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(query, range(8)))
    assert len(calls) == 1
    assert len(caplog.records) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_limited_primary_does_not_hide_empty_or_failed_fallback(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, clock: list[float], failed: bool,
) -> None:
    monkeypatch.setattr(
        providers, "urlopen", lambda *_args, **_kwargs: response(-2001, "频率超限(1次/小时)")
    )
    settings.tushare_token = SecretStr("test-account")
    fallback = Fallback()
    fallback.frame = pd.DataFrame()

    def failure(*_args: Any) -> pd.DataFrame:
        raise RuntimeError("fallback unavailable")

    if failed:
        monkeypatch.setattr(fallback, "history", failure)
    provider = providers.FallbackMarketDataProvider(
        providers.TushareProvider(settings), fallback, frozenset({"US"}),
    )
    if failed:
        with pytest.raises(RuntimeError, match="fallback unavailable"):
            provider.history("US", "AMZN")
    else:
        assert provider.history("US", "AMZN").empty
