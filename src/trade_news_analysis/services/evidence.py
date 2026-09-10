"""Deterministic evidence grading for event research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Article, Event

STRONG_SOURCE_MARKERS = (
    "bls employment situation",
    "bureau of labor statistics",
    "sec edgar",
    "federal reserve",
    "hkex",
    "sse",
    "szse",
    "beijing stock exchange",
    "上交所",
    "深交所",
    "北交所",
    "交易所公告",
    "company announcement",
    "regulator",
)
MEDIUM_SOURCE_MARKERS = (
    "reuters",
    "bloomberg",
    "financial times",
    "wall street journal",
    "barrons",
    "associated press",
    "nikkei",
    "财联社",
    "证券时报",
    "中国证券报",
)


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    score: float
    grade: str
    independent_sources: int
    strong_sources: int
    medium_sources: int
    weak_sources: int


def source_identity(article: Article) -> str:
    """Collapse repeated posts or spelling variants from the same publisher."""
    raw = article.author_handle or article.source
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", raw.casefold())
    aliases = {
        "yahoofinance": "yahoo",
        "yahoofinancevideo": "yahoo",
        "secedgar": "secedgar",
    }
    return aliases.get(normalized, normalized)


def source_level(article: Article) -> int:
    """Return 3 for strong, 2 for medium, 1 for weak, and 0 when ineligible."""
    if not article.analysis_eligible:
        return 0
    if article.evidence_role == "official_primary":
        return 3
    if article.evidence_role == "social_lead":
        return 1
    source = article.source.casefold()
    if any(marker in source for marker in STRONG_SOURCE_MARKERS):
        return 3
    if any(marker in source for marker in MEDIUM_SOURCE_MARKERS):
        return 2
    if article.content_kind == "social_post":
        return 1
    return 1


def assess_articles(articles: list[Article]) -> EvidenceAssessment:
    levels_by_source: dict[str, int] = {}
    for article in articles:
        level = source_level(article)
        if level:
            identity = source_identity(article)
            levels_by_source[identity] = max(level, levels_by_source.get(identity, 0))
    levels = list(levels_by_source.values())
    strong = sum(level == 3 for level in levels)
    medium = sum(level == 2 for level in levels)
    weak = sum(level == 1 for level in levels)
    if strong:
        score = min(5.0, 4.5 + 0.25 * min(2, len(levels) - 1))
    elif medium:
        score = min(4.0, 2.5 + 0.5 * min(3, medium - 1) + 0.25 * min(2, weak))
    elif weak:
        score = min(2.0, 1.0 + 0.25 * min(4, weak - 1))
    else:
        score = 0.0
    grade = (
        "strong"
        if score >= 4.5
        else "medium"
        if score >= 2.5
        else "weak"
        if score
        else "none"
    )
    return EvidenceAssessment(
        score=round(score, 2),
        grade=grade,
        independent_sources=len(levels),
        strong_sources=strong,
        medium_sources=medium,
        weak_sources=weak,
    )


def assess_event(event: Event) -> EvidenceAssessment:
    return assess_articles([link.article for link in event.article_links])


def is_narrative_only(event: Event) -> bool:
    return event.demand_status == "narrative_only" or "仅有叙事" in event.observed_demand
