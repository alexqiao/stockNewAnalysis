"""Structured LLM analysis for one article-company pair."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from openai import OpenAI
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import ImpactAnalysis
from ..schemas import ImpactPayload

SYSTEM_PROMPT = """你是一名严谨的金融新闻研究员。你的输出是可验证的研究假设，不是投资建议。
只使用给定新闻中的事实，不补造数据。区分已经发生的需求变化与单纯叙事；如果没有可观察需求，明确说明。
把影响翻译到收入、利润率、现金流或估值，并给出未来可以确认或证伪的条件。
不要输出 BUY、SELL、仓位或保证收益。所有解释使用中文，最终只返回符合指定结构的 JSON。"""


def build_prompt(analysis: ImpactAnalysis) -> str:
    relation = analysis.article_symbol
    article = relation.article
    schema = json.dumps(ImpactPayload.model_json_schema(), ensure_ascii=False)
    return f"""分析以下新闻对 {relation.symbol} 的独立影响。

标题：{article.title}
摘要：{article.summary or "无摘要"}
来源：{article.source}
发布时间：{article.published_at or "未知"}

要求：
1. 对 1、5、20 个交易日分别判断 bullish、neutral 或 bearish，并给出 0-1 置信度。
2. observed_demand 必须说明需求证据已经发生，还是目前只有叙事。
3. thesis 必须写清“事件 → 财务科目 → 股价影响”的传导。
4. evidence 只能摘述输入中确实出现的事实；falsifiers 必须可观察。
5. impacts 的键必须严格为字符串 "1"、"5"、"20"。

JSON Schema：
{schema}
"""


def extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.removeprefix("```json").removeprefix("```")
        stripped = stripped.removesuffix("```").strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response contains no JSON object")
    return stripped[start : end + 1]


class ImpactAnalyzer:
    def __init__(
        self,
        settings: Settings,
        completion: Callable[[str, str], str] | None = None,
    ):
        self.settings = settings
        self._completion = completion

    def _complete(self, system: str, prompt: str) -> str:
        if self._completion:
            return self._completion(system, prompt)
        if not self.settings.llm_configured or not self.settings.llm_api_key:
            raise RuntimeError("LLM未配置：请设置 LLM_API_KEY、LLM_BASE_URL 和 LLM_MODEL")
        client = OpenAI(
            api_key=self.settings.llm_api_key.get_secret_value(),
            base_url=self.settings.llm_base_url,
            timeout=self.settings.request_timeout_seconds,
            max_retries=2,
        )
        response = client.chat.completions.create(
            model=self.settings.llm_model,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content or ""

    def _validated_completion(self, prompt: str) -> tuple[ImpactPayload, str]:
        raw = self._complete(SYSTEM_PROMPT, prompt)
        try:
            return ImpactPayload.model_validate_json(extract_json(raw)), raw
        except (ValidationError, ValueError) as first_error:
            repair = f"""以下输出未通过结构校验：{first_error}
请只修复格式和缺失字段，不添加原新闻没有的事实。返回完整 JSON。

原输出：
{raw}
"""
            repaired = self._complete(SYSTEM_PROMPT, repair)
            return ImpactPayload.model_validate_json(extract_json(repaired)), repaired

    def analyze_one(self, session: Session, analysis: ImpactAnalysis) -> ImpactAnalysis:
        if not self.settings.llm_configured and not self._completion:
            analysis.status = "unavailable"
            analysis.error = "LLM未配置"
            session.commit()
            return analysis
        try:
            payload, raw = self._validated_completion(build_prompt(analysis))
            session.execute(
                update(ImpactAnalysis)
                .where(
                    ImpactAnalysis.article_symbol_id == analysis.article_symbol_id,
                    ImpactAnalysis.id != analysis.id,
                )
                .values(is_current=False)
            )
            values: dict[str, Any] = payload.model_dump()
            analysis.status = "complete"
            analysis.is_current = True
            analysis.model = self.settings.llm_model
            analysis.event_type = values["event_type"]
            analysis.novelty = values["novelty"]
            analysis.priced_in = values["priced_in"]
            analysis.impacts = values["impacts"]
            analysis.financial_channels = values["financial_channels"]
            analysis.observed_demand = values["observed_demand"]
            analysis.thesis = values["thesis"]
            analysis.catalysts = values["catalysts"]
            analysis.risks = values["risks"]
            analysis.falsifiers = values["falsifiers"]
            analysis.evidence = values["evidence"]
            analysis.raw_response = raw
            analysis.error = None
        except Exception as exc:  # provider and schema errors are persisted per item
            analysis.status = "error"
            analysis.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.commit()
        return analysis

    def analyze_pending(self, session: Session, limit: int = 100) -> int:
        pending = session.scalars(
            select(ImpactAnalysis)
            .where(ImpactAnalysis.status == "pending")
            .order_by(ImpactAnalysis.created_at)
            .limit(limit)
        ).all()
        for analysis in pending:
            self.analyze_one(session, analysis)
        return len(pending)
