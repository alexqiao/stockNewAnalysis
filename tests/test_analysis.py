from __future__ import annotations

import json

from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.models import Article, ArticleSymbol, ImpactAnalysis
from trade_news_analysis.services.analysis import ImpactAnalyzer, extract_json

VALID_PAYLOAD = {
    "event_type": "product_launch",
    "novelty": 0.8,
    "priced_in": 0.3,
    "impacts": {
        "1": {"direction": "bullish", "confidence": 0.6, "reason": "短期需求信号"},
        "5": {"direction": "bullish", "confidence": 0.7, "reason": "订单可能传导"},
        "20": {"direction": "neutral", "confidence": 0.5, "reason": "需等待财务验证"},
    },
    "financial_channels": ["revenue", "gross_margin"],
    "observed_demand": "客户已开始付费，属于可观察需求。",
    "thesis": "付费客户增加可能提升收入，但利润率仍待验证。",
    "catalysts": ["订单披露"],
    "risks": ["竞争加剧"],
    "falsifiers": ["下一季度收入未增长"],
    "evidence": ["摘要称客户已开始付费"],
}


def add_pending_analysis(session: Session) -> ImpactAnalysis:
    article = Article(
        fingerprint="f" * 64,
        canonical_url="https://example.com/a",
        source="Wire",
        title="Apple launches paid service",
        summary="Customers started paying.",
        story_cluster_id="cluster",
    )
    relation = ArticleSymbol(article=article, symbol="AAPL", in_title=True)
    analysis = ImpactAnalysis(article_symbol=relation, status="pending", model="test")
    session.add(analysis)
    session.commit()
    return analysis


def test_extract_json_accepts_fenced_output() -> None:
    assert json.loads(extract_json(f"```json\n{json.dumps(VALID_PAYLOAD)}\n```"))["novelty"] == 0.8


def test_analyzer_repairs_invalid_json_once(session: Session, settings: Settings) -> None:
    analysis = add_pending_analysis(session)
    responses = iter(["not-json", json.dumps(VALID_PAYLOAD, ensure_ascii=False)])
    analyzer = ImpactAnalyzer(settings, completion=lambda _system, _prompt: next(responses))
    result = analyzer.analyze_one(session, analysis)
    assert result.status == "complete"
    assert result.impacts["5"]["direction"] == "bullish"
    assert result.observed_demand is not None
    assert result.observed_demand.startswith("客户")


def test_analyzer_without_key_is_unavailable(session: Session, settings: Settings) -> None:
    analysis = add_pending_analysis(session)
    result = ImpactAnalyzer(settings).analyze_one(session, analysis)
    assert result.status == "unavailable"
    assert result.error == "LLM未配置"
