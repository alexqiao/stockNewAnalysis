from __future__ import annotations

import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from trade_news_analysis import api as api_module
from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import EventSecurityImpact, Security
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.normalization import NormalizedArticle
from trade_news_analysis.services.providers import FundamentalSnapshot, SecurityRecord
from trade_news_analysis.services.sources import NewsSource, SourceResult

from .test_analysis import VALID_EVENT_PAYLOAD, VALID_IMPACT_PAYLOAD


class APIFakeSource:
    name = "api-fake"
    markets: tuple[str, ...] = ("A", "HK", "US")
    coverage = "broad"

    def fetch(self) -> SourceResult:
        return SourceResult(
            source=self.name,
            articles=[
                NormalizedArticle(
                    source="API Wire",
                    title="Apple reports paid enterprise adoption",
                    summary="Enterprise customers started paying.",
                    url="https://example.com/api-story",
                    published_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
                )
            ],
        )


class EmptyProvider:
    name = "empty"

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        return pd.DataFrame()

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        return pd.DataFrame()


class FakeFundamentalProvider:
    name = "fake-fundamentals"

    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self,
        market: str,
        symbol: str,
        _provider_data: Mapping[str, Any] | None = None,
    ) -> FundamentalSnapshot:
        self.calls.append((market, symbol))
        if self.fail:
            raise RuntimeError("upstream unavailable")
        return FundamentalSnapshot(
            fiscal_year=2024,
            price=100,
            market_cap=1_000_000,
            shares_outstanding=100,
            revenue=1_000,
            net_income=100,
            source=self.name,
        )


def api_sources(_securities: list[Security], _settings: Settings) -> list[NewsSource]:
    return [APIFakeSource()]


def completion(_system: str, prompt: str) -> str:
    payload = VALID_EVENT_PAYLOAD if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    return json.dumps(payload, ensure_ascii=False)


def test_dashboard_opportunities_aggregate_industries_and_macro_assets() -> None:
    rows = [
        {
            "security": {"id": 1, "market": "US", "symbol": "CHIP1", "industry": "半导体"},
            "signal": {
                "score": 80,
                "confidence": 0.8,
                "conflict": 0.1,
                "evidence_event_ids": [1],
            },
        },
        {
            "security": {"id": 2, "market": "A", "symbol": "CHIP2", "industry": "半导体"},
            "signal": {
                "score": 60,
                "confidence": 0.6,
                "conflict": 0.2,
                "evidence_event_ids": [1],
            },
        },
        {
            "security": {
                "id": 3,
                "market": "US",
                "symbol": "GLD",
                "industry": "黄金",
                "opportunity_group": "黄金",
                "opportunity_scope": "全球",
            },
            "signal": {
                "score": 75,
                "confidence": 0.7,
                "conflict": 0.1,
                "evidence_event_ids": [2],
            },
        },
        {
            "security": {
                "id": 4,
                "market": "US",
                "symbol": "GOVT",
                "industry": "美债",
                "opportunity_group": "美债",
                "opportunity_scope": "US",
            },
            "signal": {
                "score": 70,
                "confidence": 0.7,
                "conflict": 0.1,
                "evidence_event_ids": [3],
            },
        },
        {
            "security": {"id": 5, "market": "HK", "symbol": "THEME", "industry": ""},
            "signal": {
                "score": 65,
                "confidence": 0.6,
                "conflict": 0.1,
                "evidence_event_ids": [4],
            },
        },
    ]

    trends = api_module._aggregate_trend_opportunities(rows, {4: ["机器人"]})

    assert {item["name"] for item in trends} == {"半导体", "黄金", "美债", "机器人"}
    assert all("security" not in item for item in trends)
    semiconductor = next(item for item in trends if item["name"] == "半导体")
    assert semiconductor["kind"] == "industry"
    assert semiconductor["markets"] == ["A", "US"]
    assert semiconductor["primary_market"] == "US"
    assert semiconductor["evidence_count"] == 2
    assert semiconductor["security_ids"] == [1, 2]
    assert semiconductor["event_ids"] == [1]
    assert next(item for item in trends if item["name"] == "黄金")["type"] == "宏观资产"
    assert next(item for item in trends if item["name"] == "美债")["type"] == "宏观资产"
    assert next(item for item in trends if item["name"] == "机器人")["type"] == "产业主题"
    gold = next(item for item in trends if item["name"] == "黄金")
    robot = next(item for item in trends if item["name"] == "机器人")
    assert api_module._opportunity_url(gold, 5, None, None).startswith(
        "/opportunities/macro?"
    )
    assert api_module._opportunity_url(robot, 5, None, None).startswith(
        "/opportunities/theme?"
    )


def test_dashboard_opportunities_keeps_selected_market(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, int]] = []

    def fake_list_opportunities(
        _request: Any,
        market: str | None,
        _theme: str | None,
        _horizon: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        calls.append((market, limit))
        return [
            {
                "security": {
                    "id": index,
                    "market": market,
                    "symbol": f"A{index}",
                    "industry": f"行业{index}",
                },
                "signal": {
                    "score": 100 - index,
                    "confidence": 0.8,
                    "conflict": 0.1,
                    "evidence_event_ids": [],
                },
            }
            for index in range(12)
        ]

    monkeypatch.setattr(api_module, "list_opportunities", fake_list_opportunities)

    request: Any = object()
    rows = api_module._dashboard_opportunities(request, "A", "机器人", 5)

    assert len(rows) == 10
    assert calls == [("A", 200)]
    assert rows[0]["detail_url"].startswith("/opportunities/industry?")
    assert "market=A" in rows[0]["detail_url"]
    assert "%E6%9C%BA%E5%99%A8%E4%BA%BA" in rows[0]["detail_url"]


def test_dashboard_opportunities_apply_us_a_quota_by_primary_market(
    monkeypatch: MonkeyPatch,
) -> None:
    def fake_list_opportunities(
        _request: Any,
        _market: str | None,
        _theme: str | None,
        _horizon: int,
        _limit: int,
    ) -> list[dict[str, Any]]:
        return [
            {
                "security": {
                    "id": index,
                    "market": item_market,
                    "symbol": f"{item_market}{index}",
                    "industry": f"{item_market}行业{index}",
                },
                "signal": {
                    "score": 100 - index,
                    "confidence": 0.8,
                    "conflict": 0.1,
                    "evidence_event_ids": [],
                },
            }
            for index, item_market in enumerate(["A"] * 12 + ["US"] * 12 + ["HK"] * 4)
        ]

    monkeypatch.setattr(api_module, "list_opportunities", fake_list_opportunities)
    request: Any = object()

    rows = api_module._dashboard_opportunities(request, None, None, 5)

    assert len(rows) == 10
    assert sum(item["primary_market"] == "US" for item in rows) == 7
    assert sum(item["primary_market"] == "A" for item in rows) == 3
    assert all(item["primary_market"] != "HK" for item in rows)


def test_related_stocks_include_both_directions_and_exclude_research_assets() -> None:
    def security(identifier: int, symbol: str, research_asset: bool = False) -> Security:
        return Security(
            id=identifier,
            market="US",
            exchange="NASDAQ",
            symbol=symbol,
            name=f"Company {symbol}",
            industry="测试行业",
            market_cap=None,
            currency="USD",
            provider_data={"research_asset": research_asset},
        )

    def impact(
        item: Security,
        event_id: int,
        direction: str,
        score: float,
    ) -> EventSecurityImpact:
        return EventSecurityImpact(
            event_id=event_id,
            security_id=item.id,
            security=item,
            status="complete",
            is_current=True,
            opportunity_score=score,
            impacts={
                "5": {
                    "direction": direction,
                    "confidence": 0.8,
                    "reason": "测试传导",
                }
            },
            thesis=f"{item.symbol} 传导假设",
        )

    stocks = [security(index, f"STOCK{index}") for index in range(1, 7)]
    research_asset = security(99, "GLD", research_asset=True)
    impacts = [
        impact(item, index, "bullish" if index % 2 else "bearish", 100 - index)
        for index, item in enumerate(stocks, 1)
    ]
    impacts.append(impact(research_asset, 99, "bullish", 100))

    rows = api_module._related_stock_rows(impacts, 5)

    assert len(rows) == 5
    assert {item["direction"] for item in rows} == {"bullish", "bearish"}
    assert all(item["security"]["symbol"] != "GLD" for item in rows)
    assert [item["relevance"] for item in rows] == sorted(
        (item["relevance"] for item in rows), reverse=True
    )
    assert api_module._related_stock_rows([impacts[-1]], 5) == []


def test_api_end_to_end(session_factory: SessionFactory, settings: Settings) -> None:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        analyzer=EventAnalyzer(settings, completion=completion),
        evaluator=OutcomeEvaluator(provider=EmptyProvider()),
    )
    app = create_app(settings, session_factory, coordinator)
    with TestClient(app) as client:
        response = client.post("/api/v1/runs/ingest")
        assert response.status_code == 202
        run_id = response.json()["id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}").json()
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert run["status"] == "complete"

        for _ in range(100):
            opportunities = client.get("/api/v1/opportunities?horizon=5").json()
            if opportunities:
                break
            time.sleep(0.01)
        assert opportunities[0]["security"]["symbol"] == "AAPL"
        assert opportunities[0]["signal"]["direction"] == "bullish"

        news = client.get("/api/v1/news?symbol=AAPL").json()
        assert news[0]["events"][0]["securities"][0]["symbol"] == "AAPL"
        event_id = news[0]["events"][0]["id"]
        event = client.get(f"/api/v1/events/{event_id}").json()
        assert event["themes"][0]["name"] == "企业软件"
        assert event["unresolved_candidates"][0]["symbol"] == "FAKE"
        security_id = opportunities[0]["security"]["id"]
        assert client.get(f"/api/v1/securities/{security_id}").status_code == 200
        assert client.get("/api/v1/themes").json()[0]["event_count"] == 1

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "从事件发现趋势" in dashboard.text
        assert "Apple Inc." in dashboard.text
        opportunity_section = dashboard.text.split("跨市场机会榜", 1)[1].split(
            "自选股当前研判", 1
        )[0]
        assert "消费电子" in opportunity_section
        assert "Apple Inc." not in opportunity_section
        assert "/securities/" not in opportunity_section
        assert "/opportunities/industry?" in opportunity_section

        detail = client.get(
            "/opportunities/industry",
            params={"name": "消费电子", "horizon": 5},
        )
        assert detail.status_code == 200
        assert "最可能涉及的股票" in detail.text
        assert "Apple Inc." in detail.text
        assert VALID_IMPACT_PAYLOAD["thesis"] in detail.text
        assert "订单披露" in detail.text
        assert "竞争加剧" in detail.text
        assert "下一季度收入未增长" in detail.text
        assert "企业开始采用苹果付费服务" in detail.text
        assert client.get(
            "/opportunities/industry",
            params={"name": "不存在的行业", "horizon": 5},
        ).status_code == 404
        assert client.get("/api/v1/metrics").status_code == 200
        assert "前向验证" in client.get("/metrics").text
        assert client.get("/api/v1/health").json()["status"] == "ok"


def test_watchlist_accepts_ids_and_manual_cross_market_input(
    session_factory: SessionFactory, settings: Settings, monkeypatch: MonkeyPatch
) -> None:
    with session_factory() as session:
        session.add_all(
            [
                Security(
                    market="HK",
                    exchange="HKEX",
                    symbol="00700.HK",
                    name="腾讯控股",
                    aliases=["Tencent"],
                    currency="HKD",
                    timezone="Asia/Hong_Kong",
                    calendar="HK",
                ),
                Security(
                    market="A",
                    exchange="SZSE",
                    symbol="000700.SZ",
                    name="模糊代码测试",
                    currency="CNY",
                    timezone="Asia/Shanghai",
                    calendar="CN",
                ),
            ]
        )
        session.commit()
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        evaluator=OutcomeEvaluator(provider=EmptyProvider()),
    )
    app = create_app(settings, session_factory, coordinator)
    monkeypatch.setattr(
        "trade_news_analysis.api.lookup_security_record",
        lambda market, value: SecurityRecord(
            market="HK",
            exchange="HKEX",
            symbol="03888.HK",
            name="金山软件",
            aliases=["Kingsoft"],
            currency="HKD",
            timezone="Asia/Hong_Kong",
            calendar="HK",
            provider_data={"source": "test"},
        )
        if market == "HK" and value == "03888.HK"
        else None,
    )
    with TestClient(app) as client:
        items = client.get("/api/v1/watchlist").json()
        assert len(items) == 2
        security_id = items[0]["security_id"]
        duplicate = {
            "items": [
                {"security_id": security_id, "active": True},
                {"security_id": security_id, "active": True},
            ]
        }
        assert client.put("/api/v1/watchlist", json=duplicate).status_code == 422
        assert client.put(
            "/api/v1/watchlist",
            json={"items": [{"security_id": 999999, "active": True}]},
        ).status_code == 422

        response = client.put(
            "/api/v1/watchlist",
            json={
                "items": [
                    {"query": "00700.HK", "active": True},
                    {"query": "AAPL", "market": "US", "active": True},
                ]
            },
        )
        assert response.status_code == 200
        assert [item["security"]["symbol"] for item in response.json()] == [
            "00700.HK",
            "AAPL",
        ]
        assert [item["position"] for item in response.json()] == [0, 1]

        verified_on_demand = client.put(
            "/api/v1/watchlist",
            json={"items": [{"query": "03888.HK", "market": "HK", "active": True}]},
        )
        assert verified_on_demand.status_code == 200
        assert verified_on_demand.json()[0]["security"]["name"] == "金山软件"

        ambiguous = client.put(
            "/api/v1/watchlist",
            json={"items": [{"query": "700", "active": True}]},
        )
        assert ambiguous.status_code == 422
        assert "歧义" in ambiguous.json()["detail"]
        missing = client.put(
            "/api/v1/watchlist",
            json={"items": [{"query": "NOT-A-SECURITY", "active": True}]},
        )
        assert missing.status_code == 422
        assert "未找到证券" in missing.json()["detail"]

        hk_results = client.get("/api/v1/securities?market=HK&q=腾讯&limit=12")
        assert hk_results.status_code == 200
        assert hk_results.json()[0]["symbol"] == "00700.HK"

        page = client.get("/watchlist")
        assert page.status_code == 200
        assert 'data-field="query"' in page.text
        assert 'data-field="market"' in page.text
        assert '<select data-field="security_id">' not in page.text
        assert 'class="move-up secondary"' in page.text
        assert 'class="move-down secondary"' in page.text


def test_pe_analysis_refresh_save_persistence_and_watchlist_access(
    session_factory: SessionFactory, settings: Settings
) -> None:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        evaluator=OutcomeEvaluator(provider=EmptyProvider()),
    )
    fundamentals = FakeFundamentalProvider()
    app = create_app(
        settings,
        session_factory,
        coordinator,
        fundamental_provider=fundamentals,
    )
    with TestClient(app) as client:
        watchlist = client.get("/api/v1/watchlist").json()
        ids = {item["security"]["symbol"]: item["security_id"] for item in watchlist}
        aapl_id = ids["AAPL"]
        msft_id = ids["MSFT"]
        endpoint = f"/api/v1/securities/{aapl_id}/pe-analysis"

        initial = client.get(endpoint)
        assert initial.status_code == 200
        assert initial.json()["status"] == "needs_data"
        assert initial.json()["refresh_recommended"] is True

        refreshed = client.post(f"{endpoint}/refresh")
        assert refreshed.status_code == 200
        assert refreshed.json()["source"]["status"] == "ready"
        assert refreshed.json()["status"] == "needs_input"
        assert fundamentals.calls == [("US", "AAPL")]

        assumptions = [
            {
                "year_offset": offset,
                "revenue_growth": 0.1,
                "net_income_growth": 0.1,
                "pe_low": 15,
                "pe_high": 20,
            }
            for offset in range(1, 5)
        ]
        saved = client.put(
            endpoint,
            json={"overrides": {"price": 80}, "assumptions": assumptions},
        )
        assert saved.status_code == 200
        assert saved.json()["status"] == "ready"
        assert saved.json()["effective_inputs"]["price"]["provenance"] == "manual"
        assert saved.json()["forecast"][0]["cagr_low"] is not None

        cleared = client.put(
            endpoint,
            json={"overrides": {}, "assumptions": assumptions},
        )
        assert cleared.status_code == 200
        assert cleared.json()["effective_inputs"]["price"] == {
            "value": 100,
            "provenance": "auto",
        }
        assert client.put(
            endpoint,
            json={
                "overrides": {},
                "assumptions": [
                    {**item, "pe_low": 30, "pe_high": 20} for item in assumptions
                ],
            },
        ).status_code == 422

        summary = client.get("/api/v1/watchlist").json()[0]["pe_analysis"]
        assert summary["status"] == "ready"
        page = client.get(f"/securities/{aapl_id}")
        assert page.status_code == 200
        assert "四年 PE 估值" in page.text
        assert 'data-assumption="revenue_growth"' in page.text

        assert client.put(
            "/api/v1/watchlist",
            json={"items": [{"security_id": aapl_id, "active": True}]},
        ).status_code == 200
        assert client.get(endpoint).json()["status"] == "ready"
        assert client.get(
            f"/api/v1/securities/{msft_id}/pe-analysis"
        ).status_code == 409

        fundamentals.fail = True
        failed_refresh = client.post(f"{endpoint}/refresh")
        assert failed_refresh.status_code == 502
        cached = client.get(endpoint).json()
        assert cached["source_data"]["price"] == 100
        assert cached["source"]["status"] == "error"
        assert "upstream unavailable" in cached["source"]["error"]
