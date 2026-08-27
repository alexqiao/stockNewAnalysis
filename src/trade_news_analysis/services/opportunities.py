"""Shared cross-market opportunity queries and aggregation."""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Article,
    EventArticle,
    EventSecurityImpact,
    EventSecurityImpactTheme,
    EventTheme,
    Security,
    SecuritySignalSnapshot,
    Theme,
)

DASHBOARD_OPPORTUNITY_LIMIT = 10
DASHBOARD_OPPORTUNITY_SOURCE_LIMIT = 200
DASHBOARD_MARKET_LIMITS = {"US": 7, "A": 3}
NEGATIVE_THEME_MARKERS = (
    "无直接",
    "不相关",
    "不纳入",
    "不构成",
    "没有直接",
    "无明确",
)


def security_brief(security: Security) -> dict[str, Any]:
    provider_data = security.provider_data or {}
    return {
        "id": security.id,
        "market": security.market,
        "exchange": security.exchange,
        "symbol": security.symbol,
        "name": security.name,
        "industry": security.industry,
        "market_cap": security.market_cap,
        "currency": security.currency,
        "opportunity_group": provider_data.get("opportunity_group"),
        "opportunity_scope": provider_data.get("opportunity_scope"),
    }


def latest_snapshot_map(
    session: Session, horizon: int, security_ids: set[int] | None = None
) -> dict[int, SecuritySignalSnapshot]:
    query = (
        select(SecuritySignalSnapshot)
        .where(SecuritySignalSnapshot.horizon == horizon)
        .order_by(SecuritySignalSnapshot.as_of.desc(), SecuritySignalSnapshot.id.desc())
    )
    if security_ids is not None:
        if not security_ids:
            return {}
        query = query.where(SecuritySignalSnapshot.security_id.in_(security_ids))
    result: dict[int, SecuritySignalSnapshot] = {}
    for snapshot in session.scalars(query):
        result.setdefault(snapshot.security_id, snapshot)
    return result


def signal_dict(snapshot: SecuritySignalSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "id": snapshot.id,
        "as_of": snapshot.as_of,
        "horizon": snapshot.horizon,
        "score": snapshot.score,
        "direction": snapshot.direction,
        "confidence": snapshot.confidence,
        "conflict": snapshot.conflict,
        "rank": snapshot.rank,
        "evidence_event_ids": snapshot.evidence_event_ids,
        "components": snapshot.components,
    }


def list_security_opportunities(
    session: Session,
    market: str | None,
    theme: str | None,
    horizon: int,
    limit: int,
) -> list[dict[str, Any]]:
    allowed_ids: set[int] | None = None
    if theme:
        event_ids = set(
            session.scalars(
                select(EventTheme.event_id)
                .join(Theme)
                .where((Theme.slug == theme) | (Theme.name == theme))
            )
        )
        allowed_ids = (
            set(
                session.scalars(
                    select(EventSecurityImpact.security_id).where(
                        EventSecurityImpact.event_id.in_(event_ids),
                        EventSecurityImpact.is_current.is_(True),
                    )
                )
            )
            if event_ids
            else set()
        )
    snapshots = latest_snapshot_map(session, horizon, allowed_ids)
    query = select(Security).where(Security.id.in_(snapshots))
    if market:
        query = query.where(Security.market == market)
    securities = {item.id: item for item in session.scalars(query)}
    rows: list[dict[str, Any]] = []
    for security_id, snapshot in snapshots.items():
        if security_id not in securities or snapshot.score <= 0:
            continue
        signal = signal_dict(snapshot)
        if signal is not None:
            rows.append({"security": security_brief(securities[security_id]), "signal": signal})
    rows.sort(key=lambda item: item["signal"]["score"], reverse=True)
    return rows[:limit]


def _normalize_theme_text(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff]+", value.casefold()))


def _theme_relevance_score(theme: str, context: str) -> int:
    normalized_theme = _normalize_theme_text(theme)
    if len(normalized_theme) < 2:
        return 0
    total = 0
    for segment in re.split(r"[，。；;\n]", context):
        normalized_segment = _normalize_theme_text(segment)
        if not normalized_segment:
            continue
        multiplier = -1 if any(marker in segment for marker in NEGATIVE_THEME_MARKERS) else 1
        segment_score = 0
        for size in range(2, min(8, len(normalized_theme)) + 1):
            grams = {
                normalized_theme[index : index + size]
                for index in range(len(normalized_theme) - size + 1)
            }
            segment_score += size * size * sum(
                min(normalized_segment.count(gram), 3) for gram in grams
            )
        total += multiplier * segment_score
    return max(total, 0)


def infer_impact_themes(
    impact: EventSecurityImpact, event_themes: list[str]
) -> list[str]:
    context = "；".join(
        [impact.thesis or "", *(impact.financial_channels or []), *(impact.catalysts or [])]
    )
    ranked = sorted(
        ((_theme_relevance_score(theme, context), theme) for theme in set(event_themes)),
        key=lambda item: (-item[0], item[1]),
    )
    return [ranked[0][1]] if ranked and ranked[0][0] > 0 else []


def _scope_signal_to_events(
    signal: dict[str, Any], event_ids: set[int]
) -> dict[str, Any]:
    components = (signal.get("components") or {}).get("events")
    if not isinstance(components, list):
        return {**signal, "evidence_event_ids": sorted(event_ids)}
    selected = [
        item
        for item in components
        if isinstance(item, dict) and item.get("event_id") in event_ids
    ]
    if not selected:
        return {**signal, "evidence_event_ids": []}
    signed_total = sum(float(item.get("contribution") or 0.0) for item in selected)
    absolute_total = sum(abs(float(item.get("contribution") or 0.0)) for item in selected)
    normalizer = sum(
        float(item.get("confidence") or 0.0) * float(item.get("decay") or 0.0)
        for item in selected
    )
    score = signed_total / normalizer if normalizer else 0.0
    conflict = 1.0 - abs(signed_total) / absolute_total if absolute_total else 0.0
    confidence = normalizer / len(selected) * (1 - conflict)
    return {
        **signal,
        "score": round(score, 2),
        "confidence": round(max(0.0, min(1.0, confidence)), 4),
        "conflict": round(max(0.0, min(1.0, conflict)), 4),
        "evidence_event_ids": sorted(event_ids),
        "components": {"events": selected},
    }


def themes_are_similar(left: str, right: str) -> bool:
    normalized_left = _normalize_theme_text(left)
    normalized_right = _normalize_theme_text(right)
    if normalized_left == normalized_right:
        return True
    if min(len(normalized_left), len(normalized_right)) < 4:
        return False
    if normalized_left in normalized_right or normalized_right in normalized_left:
        return True
    return SequenceMatcher(None, normalized_left, normalized_right).ratio() >= 0.62


def _dominant_impact_theme(
    security_id: int,
    event_ids: set[int],
    themes_by_impact: dict[tuple[int, int], list[str]],
    signal: dict[str, Any],
) -> tuple[str, set[int]] | None:
    assignments = {
        event_id: set(themes_by_impact.get((security_id, event_id), []))
        for event_id in event_ids
    }
    unique_themes = sorted({theme for themes in assignments.values() for theme in themes})
    if not unique_themes:
        return None

    clusters: list[set[str]] = []
    for theme in unique_themes:
        matching = [
            cluster
            for cluster in clusters
            if any(themes_are_similar(theme, item) for item in cluster)
        ]
        if not matching:
            clusters.append({theme})
            continue
        merged = {theme}
        for cluster in matching:
            merged.update(cluster)
            clusters.remove(cluster)
        clusters.append(merged)

    component_weights: dict[int, float] = {}
    components = (signal.get("components") or {}).get("events")
    if isinstance(components, list):
        for item in components:
            if not isinstance(item, dict) or not isinstance(item.get("event_id"), int):
                continue
            component_weights[item["event_id"]] = (
                float(item.get("opportunity_score") or 0.0)
                * float(item.get("confidence") or 0.0)
                * float(item.get("decay") or 0.0)
            )

    ranked: list[tuple[int, float, str, set[int]]] = []
    for cluster in clusters:
        related_event_ids = {
            event_id for event_id, themes in assignments.items() if themes & cluster
        }
        theme_counts = Counter(
            theme for themes in assignments.values() for theme in themes if theme in cluster
        )
        representative = min(
            cluster,
            key=lambda theme: (
                -theme_counts[theme],
                len(_normalize_theme_text(theme)),
                theme,
            ),
        )
        ranked.append(
            (
                len(related_event_ids),
                sum(component_weights.get(event_id, 0.0) for event_id in related_event_ids),
                representative,
                related_event_ids,
            )
        )
    _, _, representative, related_event_ids = min(
        ranked,
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    return representative, related_event_ids


def aggregate_trend_opportunities(
    rows: list[dict[str, Any]],
    themes_by_impact: dict[tuple[int, int], list[str]],
    limit: int | None = DASHBOARD_OPPORTUNITY_LIMIT,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        security = row["security"]
        signal = row["signal"]
        macro_group = str(security.get("opportunity_group") or "").strip()
        industry = str(security.get("industry") or "").strip()
        evidence_event_ids = set(signal.get("evidence_event_ids", []))
        dominant_theme = _dominant_impact_theme(
            security["id"], evidence_event_ids, themes_by_impact, signal
        )
        if macro_group:
            opportunity_kind = "macro"
            opportunity_type = "宏观资产"
            name = macro_group
            related_event_ids = evidence_event_ids
        elif dominant_theme:
            opportunity_kind = "theme"
            opportunity_type = "产业主题"
            name, related_event_ids = dominant_theme
        elif industry:
            opportunity_kind = "industry"
            opportunity_type = "行业"
            name = industry
            related_event_ids = evidence_event_ids
        else:
            opportunity_kind = "industry"
            opportunity_type = "行业"
            name = f"{security['market']} 其他行业"
            related_event_ids = evidence_event_ids
        scoped_signal = _scope_signal_to_events(signal, related_event_ids)

        key = (opportunity_kind, name)
        group = groups.setdefault(
            key,
            {
                "name": name,
                "kind": opportunity_kind,
                "type": opportunity_type,
                "markets": set(),
                "market_weights": {},
                "security_ids": set(),
                "event_ids": set(),
                "weight": 0.0,
                "weighted_score": 0.0,
                "weighted_confidence": 0.0,
                "weighted_conflict": 0.0,
                "as_of": None,
            },
        )
        group["markets"].add(security.get("opportunity_scope") or security["market"])
        group["security_ids"].add(security["id"])
        group["event_ids"].update(related_event_ids)
        signal_as_of = signal.get("as_of")
        if signal_as_of is not None and (
            group["as_of"] is None or signal_as_of > group["as_of"]
        ):
            group["as_of"] = signal_as_of
        weight = max(float(scoped_signal["confidence"]), 0.01)
        market_weight = abs(float(scoped_signal["score"])) * weight
        group["market_weights"][security["market"]] = (
            group["market_weights"].get(security["market"], 0.0) + market_weight
        )
        group["weight"] += weight
        group["weighted_score"] += float(scoped_signal["score"]) * weight
        group["weighted_confidence"] += float(scoped_signal["confidence"]) * weight
        group["weighted_conflict"] += float(scoped_signal["conflict"]) * weight

    result: list[dict[str, Any]] = []
    for group in groups.values():
        weight = group["weight"]
        score = group["weighted_score"] / weight
        if score <= 0:
            continue
        primary_market = max(
            group["market_weights"],
            key=lambda market: (group["market_weights"][market], market),
        )
        result.append(
            {
                "name": group["name"],
                "kind": group["kind"],
                "type": group["type"],
                "markets": sorted(group["markets"]),
                "primary_market": primary_market,
                "score": round(score, 2),
                "direction": (
                    "bullish" if score > 5 else "bearish" if score < -5 else "neutral"
                ),
                "confidence": round(group["weighted_confidence"] / weight, 4),
                "conflict": round(group["weighted_conflict"] / weight, 4),
                "evidence_count": len(group["security_ids"]),
                "security_ids": sorted(group["security_ids"]),
                "event_ids": sorted(group["event_ids"]),
                "as_of": group["as_of"],
            }
        )
    result.sort(key=lambda item: item["score"], reverse=True)
    visible = result if limit is None else result[:limit]
    for rank, item in enumerate(visible, 1):
        item["rank"] = rank
    return visible


def trend_opportunities(
    session: Session,
    market: str | None,
    theme: str | None,
    horizon: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    rows = list_security_opportunities(
        session, market, theme, horizon, DASHBOARD_OPPORTUNITY_SOURCE_LIMIT
    )
    event_ids = {
        event_id
        for row in rows
        if not row["security"].get("opportunity_group")
        for event_id in row["signal"].get("evidence_event_ids", [])
    }
    themes_by_impact: dict[tuple[int, int], list[str]] = {}
    if event_ids:
        themes_by_event: dict[int, list[str]] = {}
        for event_id, theme_name in session.execute(
            select(EventTheme.event_id, Theme.name)
            .join(Theme, EventTheme.theme_id == Theme.id)
            .where(EventTheme.event_id.in_(event_ids))
        ):
            themes_by_event.setdefault(event_id, []).append(theme_name)
        security_ids = {row["security"]["id"] for row in rows}
        impacts = session.scalars(
            select(EventSecurityImpact)
            .where(
                EventSecurityImpact.event_id.in_(event_ids),
                EventSecurityImpact.security_id.in_(security_ids),
                EventSecurityImpact.is_current.is_(True),
                EventSecurityImpact.status == "complete",
            )
            .options(
                selectinload(EventSecurityImpact.theme_links).selectinload(
                    EventSecurityImpactTheme.theme
                )
            )
        )
        for impact in impacts:
            assigned = [link.theme.name for link in impact.theme_links]
            key = (impact.security_id, impact.event_id)
            themes_by_impact[key] = assigned or infer_impact_themes(
                impact, themes_by_event.get(impact.event_id, [])
            )
    opportunities = aggregate_trend_opportunities(rows, themes_by_impact, limit)
    attach_latest_source_links(session, opportunities)
    return opportunities


def attach_latest_source_links(
    session: Session, opportunities: list[dict[str, Any]]
) -> None:
    event_ids = {
        event_id
        for opportunity in opportunities
        for event_id in opportunity.get("event_ids") or []
        if isinstance(event_id, int)
    }
    if not event_ids:
        return
    article_rows = list(
        session.execute(
            select(EventArticle.event_id, Article)
            .join(Article, EventArticle.article_id == Article.id)
            .where(
                EventArticle.event_id.in_(event_ids),
                Article.canonical_url != "",
            )
            .order_by(
                Article.published_at.desc(),
                Article.fetched_at.desc(),
                Article.id.desc(),
            )
        )
    )
    for opportunity in opportunities:
        supporting_event_ids = set(opportunity.get("event_ids") or [])
        source = next(
            (
                article
                for event_id, article in article_rows
                if event_id in supporting_event_ids
            ),
            None,
        )
        if source is None:
            continue
        opportunity["source_url"] = source.canonical_url
        opportunity["source_title"] = source.title
        opportunity["source_name"] = source.source


def opportunity_url(
    opportunity: dict[str, Any],
    horizon: int,
    market: str | None,
    theme: str | None,
) -> str:
    params: dict[str, str | int] = {"name": opportunity["name"], "horizon": horizon}
    if market:
        params["market"] = market
    if theme:
        params["theme"] = theme
    return f"/opportunities/{opportunity['kind']}?{urlencode(params)}"


def select_dashboard_opportunities(
    all_opportunities: list[dict[str, Any]],
    market: str | None,
    theme: str | None,
    horizon: int,
) -> list[dict[str, Any]]:
    if market:
        opportunities = all_opportunities[:DASHBOARD_OPPORTUNITY_LIMIT]
    else:
        opportunities = [
            opportunity
            for quota_market, quota_limit in DASHBOARD_MARKET_LIMITS.items()
            for opportunity in [
                item
                for item in all_opportunities
                if item["primary_market"] == quota_market
            ][:quota_limit]
        ]
        opportunities.sort(key=lambda item: item["score"], reverse=True)
        for rank, opportunity in enumerate(opportunities, 1):
            opportunity["rank"] = rank
    for opportunity in opportunities:
        opportunity["detail_url"] = opportunity_url(opportunity, horizon, market, theme)
    return opportunities


def dashboard_opportunities(
    session: Session,
    market: str | None,
    theme: str | None,
    horizon: int,
) -> list[dict[str, Any]]:
    all_opportunities = trend_opportunities(session, market, theme, horizon, None)
    return select_dashboard_opportunities(all_opportunities, market, theme, horizon)
