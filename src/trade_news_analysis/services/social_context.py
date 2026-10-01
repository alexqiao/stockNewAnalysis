"""Read-only X research context, kept separate from scored event evidence."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Event,
    EventSecurityImpact,
    EventSecurityImpactTheme,
    Security,
    SourceHealth,
    XAccount,
    XPost,
)

STANCE_LABELS = {
    "bullish": "博主观点偏多",
    "bearish": "博主观点偏空",
    "mixed": "博主观点存在分歧",
    "neutral": "博主未表达明确方向",
    "unavailable": "暂无明确个股观点",
}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _identity(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().removeprefix("$")).casefold()


def _strings(value: Any) -> list[str]:
    return (
        [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if (isinstance(value, list))
        else []
    )


def collection_status(
    session: Session, now: datetime, *, enabled: bool, stale_after_hours: int
) -> dict[str, Any]:
    accounts = session.scalars(select(XAccount).where(XAccount.active.is_(True))).all()
    health_by_source = {
        item.source.casefold(): item
        for item in session.scalars(
            select(SourceHealth).where(SourceHealth.capability == "x_posts")
        )
    }
    fresh = 0
    failed: list[str] = []
    missing: list[str] = []
    stale: list[str] = []
    limited: list[str] = []
    for account in accounts:
        health = health_by_source.get(f"x:@{account.handle.casefold()}")
        if health is not None and health.last_error:
            failed.append(account.handle)
        if health is None or health.last_success_at is None:
            missing.append(account.handle)
        elif now - _utc(health.last_success_at) > timedelta(hours=stale_after_hours):
            stale.append(account.handle)
        elif not health.last_error:
            fresh += 1
            if health.coverage == "public_page":
                limited.append(account.handle)
    gaps = []
    if not enabled:
        gaps.append("X 自动采集未开启，已保存的帖子仍可作历史参考")
    if not accounts:
        gaps.append("没有启用的 X 账号")
    if missing:
        gaps.append(f"{len(missing)} 个账号尚无成功采集记录")
    if failed:
        gaps.append(f"{len(failed)} 个账号最近采集失败，请检查 X 来源状态")
    if stale:
        gaps.append(f"{len(stale)} 个账号超过 {stale_after_hours} 小时未成功更新")
    if limited:
        gaps.append(f"{len(limited)} 个账号仅采集公开主页可见帖子，可能漏帖，非完整时间段记录")
    return {
        "enabled": enabled,
        "active_accounts": len(accounts),
        "fresh_accounts": fresh,
        "failed_accounts": failed,
        "missing_accounts": missing,
        "stale_accounts": stale,
        "limited_accounts": limited,
        "gaps": gaps,
    }


def batch_social_context(
    session: Session,
    security_ids: Sequence[int],
    *,
    now: datetime | None = None,
    lookback_days: int = 7,
    enabled: bool = True,
    stale_after_hours: int = 24,
) -> dict[int, dict[str, Any]]:
    """Associate recent posts without promoting claims or changing research scores.

    Exact identities are resolved against the whole security master to reject
    ambiguous tickers. Theme-only posts remain background and do not vote on a
    company's direction. Each author's current stance contributes at most once.
    """
    if not security_ids:
        return {}
    if lookback_days <= 0 or stale_after_hours <= 0:
        raise ValueError("社交上下文时间窗口必须大于零")
    as_of = _utc(now or datetime.now(UTC))
    requested = set(security_ids)
    securities = session.scalars(select(Security)).all()
    targets = {item.id for item in securities if item.id in requested}
    if not targets:
        return {}
    identities: dict[str, set[int]] = defaultdict(set)
    for security in securities:
        for value in [security.name, security.symbol, *(security.aliases or [])]:
            if isinstance(value, str) and _identity(value):
                identities[_identity(value)].add(security.id)
        for qualifier in (security.market, security.exchange):
            identities[_identity(f"{qualifier}:{security.symbol}")].add(security.id)
            identities[_identity(f"{security.symbol}.{qualifier}")].add(security.id)

    event_securities: dict[int, set[int]] = defaultdict(set)
    theme_securities: dict[str, set[int]] = defaultdict(set)
    impacts = session.scalars(
        select(EventSecurityImpact)
        .join(Event, Event.id == EventSecurityImpact.event_id)
        .where(
            EventSecurityImpact.security_id.in_(targets),
            EventSecurityImpact.is_current.is_(True),
            EventSecurityImpact.status == "complete",
            Event.status.in_(["complete", "partial"]),
        )
        .options(
            selectinload(EventSecurityImpact.theme_links).selectinload(
                EventSecurityImpactTheme.theme
            )
        )
    ).all()
    for impact in impacts:
        event_securities[impact.event_id].add(impact.security_id)
        for link in impact.theme_links:
            for value in (link.theme.name, link.theme.slug):
                theme_securities[_identity(value)].add(impact.security_id)

    collection = collection_status(
        session, as_of, enabled=enabled, stale_after_hours=stale_after_hours
    )
    grouped: dict[int, list[dict[str, Any]]] = {security_id: [] for security_id in targets}
    posts = session.scalars(
        select(XPost)
        .where(
            XPost.published_at >= as_of - timedelta(days=lookback_days),
            XPost.published_at <= as_of,
            XPost.screening_status.not_in(["ignore", "ignored"]),
            XPost.post_type.in_(["original", "quote"]),
        )
        .options(selectinload(XPost.account))
        .order_by(XPost.published_at.desc(), XPost.id.desc())
    ).all()
    for post in posts:
        screening = post.screening if isinstance(post.screening, dict) else {}
        classification = str(screening.get("classification") or "unscreened")
        if classification in {"irrelevant", "promotion"}:
            continue
        matches: dict[int, str] = {}
        if post.related_event_id is not None:
            matches.update(
                (security_id, "event")
                for security_id in event_securities.get(post.related_event_id, set())
            )
        direct_securities: set[int] = set()
        for entity in _strings(screening.get("entities")):
            found = identities.get(_identity(entity), set())
            if len(found) == 1:
                direct_securities.update(found)
                for security_id in found & targets:
                    matches.setdefault(security_id, "entity")
        for theme in _strings(screening.get("themes")):
            for security_id in theme_securities.get(_identity(theme), set()):
                matches.setdefault(security_id, "theme")
        for security_id, basis in matches.items():
            raw_stance = str(screening.get("stance") or "unavailable")
            # An event/theme can affect several companies in opposite directions.
            direct_entity = security_id in direct_securities
            directional = direct_securities == {security_id} and raw_stance in {
                "bullish",
                "bearish",
                "neutral",
                "mixed",
            }
            grouped[security_id].append(
                {
                    "id": post.id,
                    "post_id": post.post_id,
                    "url": post.url,
                    "author": post.account.handle,
                    "account_type": post.account.account_type,
                    "published_at": _utc(post.published_at).isoformat(),
                    "summary": str(screening.get("claim_summary") or post.text)[:500],
                    "classification": classification,
                    "screening_status": post.screening_status,
                    "related_event_id": post.related_event_id,
                    "stance": raw_stance if directional else "unavailable",
                    "post_stance": raw_stance if raw_stance in STANCE_LABELS else "unavailable",
                    "match_basis": basis,
                    "is_direct": direct_entity,
                    "relation_label": {
                        "event": "关联事件",
                        "entity": "直接提及",
                        "theme": "主题参考",
                    }[basis],
                    "factual_claims": _strings(screening.get("factual_claims")),
                    "verification_needs": _strings(screening.get("verification_needs")),
                    "external_links": _strings(post.external_links),
                    "is_confirmed_fact": False,
                }
            )
    result: dict[int, dict[str, Any]] = {}
    for security_id, related in grouped.items():
        author_stances: dict[str, str] = {}
        for context_post in related:
            if context_post["stance"] != "unavailable":
                author_stances.setdefault(
                    context_post["author"].casefold(), context_post["stance"]
                )
        counts = {
            stance: sum(value == stance for value in author_stances.values())
            for stance in ("bullish", "bearish", "neutral", "mixed")
        }
        conflict = bool(counts["mixed"] or (counts["bullish"] and counts["bearish"]))
        stance = (
            "mixed"
            if conflict
            else "bullish"
            if counts["bullish"]
            else "bearish"
            if counts["bearish"]
            else "neutral"
            if counts["neutral"]
            else "unavailable"
        )
        needs = list(dict.fromkeys(need for post in related for need in post["verification_needs"]))
        claims = [
            {"claim": claim, "author": post["author"], "url": post["url"], "status": "unverified"}
            for post in related
            for claim in post["factual_claims"]
        ]
        if claims and not needs:
            needs.append("核对帖子主张对应的公司公告、监管披露或独立报道")
        gaps = list(collection["gaps"])
        if not related:
            gaps.append(f"近 {lookback_days} 天没有可明确关联的 X 帖子，不代表没有风险或市场关注")
        result[security_id] = {
            "status": "available" if related else "unavailable",
            "label": STANCE_LABELS[stance],
            "stance": stance,
            "stance_counts": counts,
            "post_count": len(related),
            "direct_post_count": sum(post["is_direct"] for post in related),
            "theme_post_count": sum(post["match_basis"] == "theme" for post in related),
            "event_post_count": sum(post["match_basis"] == "event" for post in related),
            "author_count": len({post["author"].casefold() for post in related}),
            "directional_author_count": len(author_stances),
            "verification_needs": needs[:12],
            "factual_claims": claims[:12],
            "conflicts": ["直接提及该公司的博主观点存在多空分歧，需核对各自论据"]
            if conflict
            else [],
            "posts": related[:12],
            "collection": collection,
            "gaps": gaps,
            "lookback_days": lookback_days,
            "as_of": as_of.isoformat(),
            "formal_score_impact": 0,
            "summary": (
                f"近 {lookback_days} 天 {len(related)} 条相关帖子、"
                f"{len({post['author'].casefold() for post in related})} 位作者；"
                f"{STANCE_LABELS[stance]}。观点和待核验主张仅作研究线索。"
            ),
        }
    return result
