from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.models import (
    Article,
    Event,
    EventArticle,
    EventSecurityImpact,
    Theme,
)
from trade_news_analysis.services.analysis import EventAnalyzer, extract_json

VALID_EVENT_PAYLOAD = {
    "canonical_title": "企业开始采用苹果付费服务",
    "event_type": "product_adoption",
    "observed_demand": "企业客户已经开始付费，属于可观察需求。",
    "themes": ["企业软件"],
    "candidates": [
        {
            "name": "Apple Inc.",
            "symbol": "AAPL",
            "market": "US",
            "supply_chain_role": "直接提供付费服务",
            "chain_level": 1,
        },
        {
            "name": "Imaginary Rocket",
            "symbol": "FAKE",
            "market": "US",
            "supply_chain_role": "无法验证的候选",
            "chain_level": 2,
        },
    ],
    "evidence": ["摘要称企业客户已经开始付费"],
}

VALID_IMPACT_PAYLOAD = {
    "impacts": {
        "1": {"direction": "bullish", "confidence": 0.6, "reason": "短期需求信号"},
        "5": {"direction": "bullish", "confidence": 0.7, "reason": "订单可能传导"},
        "20": {"direction": "neutral", "confidence": 0.5, "reason": "需等待财务验证"},
    },
    "demand_certainty": 4,
    "transmission_clarity": 4,
    "business_purity": 3,
    "scale_elasticity": 2,
    "market_neglect": 2,
    "novelty_unpriced": 3,
    "verification_speed": 4,
    "risk_penalty": 5,
    "financial_channels": ["revenue", "gross_margin"],
    "thesis": "付费客户增加可能提升收入，但利润率仍待验证。",
    "catalysts": ["订单披露"],
    "risks": ["竞争加剧"],
    "falsifiers": ["下一季度收入未增长"],
    "evidence": ["摘要称客户已开始付费"],
}


def add_pending_event(session: Session) -> Event:
    article = Article(
        fingerprint="f" * 64,
        canonical_url="https://example.com/a",
        source="Wire",
        title="Apple launches paid service",
        summary="Customers started paying.",
        story_cluster_id="cluster",
    )
    event = Event(event_key="cluster", status="pending", title=article.title)
    session.add_all([article, event])
    session.flush()
    session.add(EventArticle(event_id=event.id, article_id=article.id))
    session.commit()
    return event


def completion(_system: str, prompt: str) -> str:
    payload = VALID_EVENT_PAYLOAD if "canonical_title" in prompt else VALID_IMPACT_PAYLOAD
    return json.dumps(payload, ensure_ascii=False)


def test_extract_json_accepts_fenced_output() -> None:
    fence = chr(96) * 3
    text = f"{fence}json\n{json.dumps(VALID_EVENT_PAYLOAD)}\n{fence}"
    assert json.loads(extract_json(text))["event_type"] == "product_adoption"


def test_analyzer_forwards_disabled_thinking_mode(settings: Settings) -> None:
    configured = settings.model_copy(
        update={"llm_api_key": SecretStr("test-key"), "llm_thinking": "disabled"}
    )
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])
    with patch("trade_news_analysis.services.analysis.OpenAI") as client_class:
        client_class.return_value.chat.completions.create.return_value = response
        assert EventAnalyzer(configured)._complete("system", "prompt") == "{}"
    request = client_class.return_value.chat.completions.create.call_args.kwargs
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}


def test_two_stage_analysis_resolves_only_known_security(
    session: Session, settings: Settings
) -> None:
    event = add_pending_event(session)
    result = EventAnalyzer(settings, completion=completion).analyze_event(session, event)
    assert result.status == "complete"
    assert result.observed_demand.startswith("企业客户")
    assert session.scalar(select(func.count()).select_from(Theme)) == 1
    assert session.scalar(select(func.count()).select_from(EventSecurityImpact)) == 1
    impact = session.scalar(select(EventSecurityImpact))
    assert impact is not None
    assert impact.security.symbol == "AAPL"
    assert impact.opportunity_score > 0
    assert result.unresolved_candidates[0]["symbol"] == "FAKE"


def test_analyzer_repairs_invalid_event_json_once(
    session: Session, settings: Settings
) -> None:
    event = add_pending_event(session)
    responses = iter(
        [
            "not-json",
            json.dumps(VALID_EVENT_PAYLOAD, ensure_ascii=False),
            json.dumps(VALID_IMPACT_PAYLOAD, ensure_ascii=False),
        ]
    )
    result = EventAnalyzer(
        settings, completion=lambda _system, _prompt: next(responses)
    ).analyze_event(session, event)
    assert result.status == "complete"


def test_analyzer_without_key_is_unavailable(session: Session, settings: Settings) -> None:
    event = add_pending_event(session)
    result = EventAnalyzer(settings).analyze_event(session, event)
    assert result.status == "unavailable"
    assert result.error == "LLM未配置"
