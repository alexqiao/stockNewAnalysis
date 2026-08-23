"""Two-stage event discovery and verified security-impact analysis."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..config import Settings
from ..models import (
    Event,
    EventArticle,
    EventSecurityImpact,
    EventTheme,
    Security,
    Theme,
    utc_now,
)
from ..schemas import CandidateCompany, EventPayload, ImpactPayload
from .scoring import calculate_opportunity_score, evidence_quality

SYSTEM_PROMPT = """你是一名严谨的金融事件研究员。输出是可验证的研究假设，不是投资建议。
先区分已发生的需求变化与单纯叙事，再映射产业链角色和可能受影响的上市证券。
不得发明新闻事实；候选证券可以作为待核实假设，但必须明确传导角色。
不要输出 BUY、SELL、仓位或保证收益。所有解释使用中文，只返回指定结构的 JSON。"""

PayloadT = TypeVar("PayloadT", bound=BaseModel)
Completion = Callable[[str, str], str]


def extract_json(text: str) -> str:
    stripped = text.strip()
    fence = chr(96) * 3
    if stripped.startswith(fence):
        stripped = stripped.removeprefix(f"{fence}json").removeprefix(fence)
        stripped = stripped.removesuffix(fence).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response contains no JSON object")
    return stripped[start : end + 1]


def _slug(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "-", value.casefold()).strip("-")
    return normalized[:120] or "uncategorized"


def build_event_prompt(event: Event) -> str:
    schema = json.dumps(EventPayload.model_json_schema(), ensure_ascii=False)
    evidence = "\n\n".join(
        f"[{link.article.source}] {link.article.title}\n{link.article.summary or '无摘要'}"
        for link in event.article_links[:12]
    )
    return f"""将以下多篇报道视为同一候选事件进行聚合分析。

{evidence}

要求：
1. canonical_title 概括事件本身，不照抄媒体标题。
2. observed_demand 说明已经发生的采购、交付、使用、价格或产能变化；若没有，明确写“仅有叙事”。
3. themes 使用具体产业链主题。
4. candidates 可提出一至三阶受益或受损上市公司，每个候选都要写清传导角色。
5. 当事件直接影响黄金或美国国债时，可分别使用 GLD 或 GOVT 作为内部证据载体。
6. evidence 只能摘述输入确实包含的事实。

JSON Schema：
{schema}
"""


def build_impact_prompt(event: Event, security: Security, candidate: CandidateCompany) -> str:
    schema = json.dumps(ImpactPayload.model_json_schema(), ensure_ascii=False)
    return f"""分析事件对下列已验证证券的独立影响。

事件：{event.title}
事件摘要：{event.summary}
已发生需求：{event.observed_demand}

证券：{security.name} / {security.symbol}
市场与交易所：{security.market} / {security.exchange}
行业：{security.industry or "未知"}
业务简介：{security.business_summary or "暂无可靠简介"}
市值：{security.market_cap if security.market_cap is not None else "未知"}
候选供应链角色：{candidate.supply_chain_role}
产业链层级：{candidate.chain_level}

要求：
1. 对 1、5、20 个交易日分别判断 bullish、neutral 或 bearish。
2. 八个研究维度除 risk_penalty 外均按 0-5 分；risk_penalty 按 0-20 分。
3. thesis 必须写清“事件 → 财务科目 → 证券影响”。
4. evidence 只能使用事件或证券资料中已经给出的事实。
5. 缺少业务或市值资料时降低 business_purity、scale_elasticity 和置信度。

JSON Schema：
{schema}
"""


class EventAnalyzer:
    def __init__(self, settings: Settings, completion: Completion | None = None):
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
        extra_body = (
            {"thinking": {"type": self.settings.llm_thinking}}
            if self.settings.llm_thinking
            else None
        )
        response = client.chat.completions.create(
            model=self.settings.llm_model,
            temperature=0,
            extra_body=extra_body,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content or ""

    def _validated_completion(
        self, prompt: str, payload_type: type[PayloadT]
    ) -> tuple[PayloadT, str]:
        raw = self._complete(SYSTEM_PROMPT, prompt)
        try:
            return payload_type.model_validate_json(extract_json(raw)), raw
        except (ValidationError, ValueError) as first_error:
            repair = f"""以下输出未通过结构校验：{first_error}
请只修复格式和缺失字段，不添加新事实。返回完整 JSON。

原输出：
{raw}
"""
            repaired = self._complete(SYSTEM_PROMPT, repair)
            return payload_type.model_validate_json(extract_json(repaired)), repaired

    @staticmethod
    def _candidate_security(session: Session, candidate: CandidateCompany) -> Security | None:
        symbol = candidate.symbol.upper() if candidate.symbol else None
        query = select(Security).where(Security.active.is_(True))
        if candidate.market:
            query = query.where(Security.market == candidate.market)
        securities = session.scalars(query).all()
        if symbol:
            matches = [item for item in securities if item.symbol.upper() == symbol]
            if len(matches) == 1:
                return matches[0]
            base_symbol = symbol.split(".", 1)[0]
            matches = [
                item
                for item in securities
                if item.symbol.upper().split(".", 1)[0] == base_symbol
            ]
            if len(matches) == 1:
                return matches[0]
        name = candidate.name.casefold().strip()
        matches = [
            item
            for item in securities
            if name == item.name.casefold().strip()
            or name in {str(alias).casefold().strip() for alias in item.aliases or []}
        ]
        return matches[0] if len(matches) == 1 else None

    def _analyze_impact(
        self,
        session: Session,
        event: Event,
        security: Security,
        candidate: CandidateCompany,
        source_count: int,
    ) -> EventSecurityImpact:
        session.execute(
            update(EventSecurityImpact)
            .where(
                EventSecurityImpact.event_id == event.id,
                EventSecurityImpact.security_id == security.id,
                EventSecurityImpact.is_current.is_(True),
            )
            .values(is_current=False)
        )
        impact = EventSecurityImpact(
            event_id=event.id,
            security_id=security.id,
            status="pending",
            is_current=True,
            model=self.settings.llm_model,
            chain_level=candidate.chain_level,
        )
        session.add(impact)
        session.flush()
        try:
            payload, raw = self._validated_completion(
                build_impact_prompt(event, security, candidate), ImpactPayload
            )
            values = payload.model_dump()
            quality = evidence_quality(source_count)
            dimensions = {
                name: float(values[name])
                for name in (
                    "demand_certainty",
                    "transmission_clarity",
                    "business_purity",
                    "scale_elasticity",
                    "market_neglect",
                    "novelty_unpriced",
                    "verification_speed",
                )
            }
            dimensions["evidence_quality"] = quality
            impact.status = "complete"
            impact.impacts = values["impacts"]
            for name, value in dimensions.items():
                setattr(impact, name, value)
            impact.risk_penalty = values["risk_penalty"]
            impact.opportunity_score = calculate_opportunity_score(
                dimensions, values["risk_penalty"]
            )
            impact.financial_channels = values["financial_channels"]
            impact.thesis = values["thesis"]
            impact.catalysts = values["catalysts"]
            impact.risks = values["risks"]
            impact.falsifiers = values["falsifiers"]
            impact.evidence = values["evidence"]
            impact.raw_response = raw
        except Exception as exc:
            impact.status = "error"
            impact.error = f"{type(exc).__name__}: {exc}"[:2000]
        return impact

    def analyze_event(self, session: Session, event: Event) -> Event:
        if not self.settings.llm_configured and not self._completion:
            event.status = "unavailable"
            event.error = "LLM未配置"
            session.commit()
            return event
        try:
            payload, raw = self._validated_completion(build_event_prompt(event), EventPayload)
            event.title = payload.canonical_title
            event.event_type = payload.event_type
            event.observed_demand = payload.observed_demand
            event.summary = "；".join(payload.evidence)
            event.model = self.settings.llm_model
            event.raw_response = raw
            event.error = None
            event.updated_at = utc_now()
            for name in payload.themes:
                slug = _slug(name)
                theme = session.scalar(select(Theme).where(Theme.slug == slug))
                if theme is None:
                    theme = Theme(slug=slug, name=name)
                    session.add(theme)
                    session.flush()
                if not session.scalar(
                    select(EventTheme).where(
                        EventTheme.event_id == event.id, EventTheme.theme_id == theme.id
                    )
                ):
                    session.add(EventTheme(event_id=event.id, theme_id=theme.id))
            source_count = len({link.article.source for link in event.article_links})
            resolved: set[int] = set()
            unresolved: list[dict[str, object]] = []
            for candidate in payload.candidates:
                security = self._candidate_security(session, candidate)
                if security is None:
                    unresolved.append(candidate.model_dump())
                    continue
                if security.id in resolved:
                    continue
                resolved.add(security.id)
                self._analyze_impact(session, event, security, candidate, source_count)
            event.unresolved_candidates = unresolved
            event.status = "complete"
        except Exception as exc:
            event.status = "error"
            event.error = f"{type(exc).__name__}: {exc}"[:2000]
        session.commit()
        return event

    def analyze_pending(self, session: Session, limit: int = 100) -> int:
        statuses = (
            ["pending"]
            if not (self.settings.llm_configured or self._completion)
            else ["pending", "unavailable"]
        )
        events = session.scalars(
            select(Event)
            .where(Event.status.in_(statuses))
            .order_by(Event.created_at)
            .limit(limit)
            .options(selectinload(Event.article_links).selectinload(EventArticle.article))
        ).all()
        for event in events:
            self.analyze_event(session, event)
        return len(events)


ImpactAnalyzer = EventAnalyzer
