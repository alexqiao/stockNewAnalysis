"""Deterministic evidence grading for event research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

EVIDENCE_RULE_VERSION = "original-sources-v2"

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
    report_sources: int
    report_count: int
    original_sources: int
    unknown_original_sources: int
    rule_version: str = EVIDENCE_RULE_VERSION


def original_source_identity(article: Article) -> str | None:
    raw = article.original_source_url
    if not raw and source_level(article) == 3:
        raw = article.canonical_url
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return None
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path,
                           parsed.query, ""))
    except ValueError:
        return None


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
    reporters: set[str] = set()
    originals: set[str] = set()
    unknown: set[str] = set()
    report_count = 0
    for article in articles:
        level = source_level(article)
        if level:
            report_count += 1
            publisher = source_identity(article)
            reporters.add(publisher)
            original = original_source_identity(article)
            if original:
                originals.add(original)
            else:
                unknown.add(publisher)
            # Unknown attribution cannot demonstrate independent corroboration.
            identity = original or "unknown-attribution"
            levels_by_source[identity] = max(level, levels_by_source.get(identity, 0))
    levels = list(levels_by_source.values())
    strong = sum(level == 3 for level in levels)
    medium = sum(level == 2 for level in levels)
    weak = sum(level == 1 for level in levels)
    if strong:
        score = min(5.0, 4.5 + 0.25 * min(2, max(0, len(originals) - 1)))
    elif medium:
        verified_medium = sum(levels_by_source[key] == 2 for key in originals)
        verified_weak = sum(levels_by_source[key] == 1 for key in originals)
        extra_weak = max(0, verified_weak - (0 if verified_medium else 1))
        score = min(4.0, 2.5 + 0.5 * min(3, max(0, verified_medium - 1))
                    + 0.25 * min(2, extra_weak))
    elif weak:
        score = min(2.0, 1.0 + 0.25 * min(4, max(0, len(originals) - 1)))
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
        independent_sources=len(originals),
        strong_sources=strong,
        medium_sources=medium,
        weak_sources=weak,
        report_sources=len(reporters),
        report_count=report_count,
        original_sources=len(originals),
        unknown_original_sources=len(unknown),
    )


def assess_event(event: Event) -> EvidenceAssessment:
    return assess_articles([link.article for link in event.article_links])


def is_narrative_only(event: Event) -> bool:
    return event.demand_status == "narrative_only" or "仅有叙事" in event.observed_demand
