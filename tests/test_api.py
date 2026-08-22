from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pandas as pd
from fastapi.testclient import TestClient

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.main import create_app
from trade_news_analysis.models import Watchlist
from trade_news_analysis.services.analysis import ImpactAnalyzer
from trade_news_analysis.services.coordinator import PipelineCoordinator
from trade_news_analysis.services.evaluation import OutcomeEvaluator
from trade_news_analysis.services.normalization import NormalizedArticle
from trade_news_analysis.services.sources import NewsSource, SourceResult

from .test_analysis import VALID_PAYLOAD


class APIFakeSource:
    name = "api-fake"

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
                    hinted_symbols={"AAPL"},
                )
            ],
        )


def api_sources(_watchlist: list[Watchlist], _settings: Settings) -> list[NewsSource]:
    return [APIFakeSource()]


def test_api_end_to_end(session_factory: SessionFactory, settings: Settings) -> None:
    analyzer = ImpactAnalyzer(
        settings, completion=lambda _system, _prompt: json.dumps(VALID_PAYLOAD, ensure_ascii=False)
    )
    evaluator = OutcomeEvaluator(lambda _symbol: pd.DataFrame())
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        analyzer=analyzer,
        evaluator=evaluator,
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
            news = client.get("/api/v1/news?symbol=AAPL").json()
            analysis = news[0]["symbols"][0]["analysis"]
            if analysis["status"] == "complete":
                break
            time.sleep(0.01)
        assert analysis["impacts"]["5"]["direction"] == "bullish"
        assert client.get("/").status_code == 200
        assert "新闻不是结论" in client.get("/").text
        assert client.get("/api/v1/health").json()["status"] == "ok"


def test_watchlist_validation(session_factory: SessionFactory, settings: Settings) -> None:
    coordinator = PipelineCoordinator(
        session_factory,
        settings,
        source_factory=api_sources,
        evaluator=OutcomeEvaluator(lambda _symbol: pd.DataFrame()),
    )
    app = create_app(settings, session_factory, coordinator)
    with TestClient(app) as client:
        assert len(client.get("/api/v1/watchlist").json()) == 2
        duplicate = {
            "items": [
                {"symbol": "AAPL", "company_name": "Apple", "aliases": [], "active": True},
                {"symbol": "AAPL", "company_name": "Apple", "aliases": [], "active": True},
            ]
        }
        assert client.put("/api/v1/watchlist", json=duplicate).status_code == 422
