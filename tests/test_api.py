from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pandas as pd
from fastapi.testclient import TestClient

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import Security
from trade_news_analysis.services.analysis import EventAnalyzer
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.normalization import NormalizedArticle
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


def api_sources(_securities: list[Security], _settings: Settings) -> list[NewsSource]:
    return [APIFakeSource()]


def completion(_system: str, prompt: str) -> str:
    payload = VALID_EVENT_PAYLOAD if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    return json.dumps(payload, ensure_ascii=False)


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
        assert "从事件发现标的" in dashboard.text
        assert "Apple Inc." in dashboard.text
        assert client.get("/api/v1/metrics").status_code == 200
        assert "前向验证" in client.get("/metrics").text
        assert client.get("/api/v1/health").json()["status"] == "ok"


def test_watchlist_uses_security_ids(
    session_factory: SessionFactory, settings: Settings
) -> None:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        evaluator=OutcomeEvaluator(provider=EmptyProvider()),
    )
    app = create_app(settings, session_factory, coordinator)
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
