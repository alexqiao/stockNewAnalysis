from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import (
    Event,
    EventSecurityImpact,
    EventSecurityImpactTheme,
    Security,
    SourceHealth,
    Theme,
    XAccount,
    XPost,
)
from trade_news_analysis.services.social_context import batch_social_context, collection_status

NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def test_public_page_success_preserves_limited_coverage_warning(session: Session) -> None:
    session.add(XAccount(handle="limited", account_type="commentator", active=True))
    session.add(SourceHealth(
        source="X:@limited", capability="x_posts", coverage="public_page", last_success_at=NOW
    ))
    session.flush()
    status = collection_status(session, NOW, enabled=True, stale_after_hours=24)
    assert status["fresh_accounts"] == 1
    assert status["limited_accounts"] == ["limited"]
    assert "limited" not in status["failed_accounts"]
    assert any("可能漏帖" in gap for gap in status["gaps"])


def security(session: Session, symbol: str = "AAPL") -> Security:
    result = session.scalar(select(Security).where(Security.symbol == symbol))
    assert result is not None
    return result


def post(
    session: Session,
    identifier: str,
    *,
    author: str = "test_author",
    entities: list[str] | None = None,
    themes: list[str] | None = None,
    stance: str = "bullish",
    classification: str = "opinion",
    hours_ago: int = 1,
    status: str = "context",
    event_id: int | None = None,
) -> XPost:
    account = session.scalar(select(XAccount).where(XAccount.handle == author))
    if account is None:
        account = XAccount(handle=author, account_type="commentator")
        session.add(account)
        session.flush()
    item = XPost(
        account_id=account.id,
        post_id=identifier,
        url=f"https://x.com/{author}/status/{identifier}",
        post_type="original",
        text="AAPL 观点原文",
        published_at=NOW - timedelta(hours=hours_ago),
        screening_status=status,
        screening={
            "classification": classification,
            "entities": entities if entities is not None else ["AAPL"],
            "themes": themes or [],
            "stance": stance,
            "claim_summary": "作者对业务前景的判断",
            "factual_claims": ["帖子声称订单增长"],
            "verification_needs": ["核对公司订单披露"],
        },
        related_event_id=event_id,
    )
    session.add(item)
    session.flush()
    return item


def test_context_uses_exact_entities_and_preserves_unverified_claims(session: Session) -> None:
    apple = security(session)
    apple.aliases = ["苹果公司"]
    item = post(session, "101", entities=["苹果公司"])

    context = batch_social_context(session, [apple.id], now=NOW)[apple.id]

    assert context["status"] == "available"
    assert context["post_count"] == context["direct_post_count"] == 1
    assert context["stance"] == "bullish"
    assert context["posts"][0]["id"] == item.id
    assert context["posts"][0]["match_basis"] == "entity"
    assert context["posts"][0]["is_confirmed_fact"] is False
    assert context["posts"][0]["screening_status"] == "context"
    assert context["factual_claims"][0]["status"] == "unverified"
    assert context["factual_claims"][0]["url"] == item.url
    assert context["verification_needs"] == ["核对公司订单披露"]
    assert context["formal_score_impact"] == 0
    assert item.promoted_article_id is None
    assert item.related_event_id is None


def test_latest_author_view_does_not_turn_repeated_posts_into_consensus(session: Session) -> None:
    apple = security(session)
    post(session, "201", author="author_one", stance="bullish", hours_ago=5)
    post(session, "202", author="author_one", stance="bullish", hours_ago=4)
    post(session, "203", author="author_one", stance="bearish", hours_ago=2)
    post(session, "204", author="author_two", stance="bullish", hours_ago=1)

    context = batch_social_context(session, [apple.id], now=NOW)[apple.id]

    assert context["post_count"] == 4
    assert context["author_count"] == context["directional_author_count"] == 2
    assert context["stance_counts"] == {"bullish": 1, "bearish": 1, "neutral": 0, "mixed": 0}
    assert context["stance"] == "mixed"
    assert context["conflicts"]


def test_ambiguous_ticker_rejected_even_when_other_market_is_not_requested(
    session: Session,
) -> None:
    apple = security(session)
    other = Security(market="HK", exchange="HKEX", symbol="AAPL", name="Another Company")
    session.add(other)
    session.flush()
    post(session, "301", entities=["$AAPL"])
    post(session, "302", entities=["US:AAPL"])
    post(session, "303", entities=["AAPL services"])

    context = batch_social_context(session, [apple.id], now=NOW)[apple.id]

    assert context["post_count"] == 1
    assert context["posts"][0]["post_id"] == "302"


def test_theme_and_event_context_do_not_inherit_post_direction(session: Session) -> None:
    apple = security(session)
    event = Event(event_key="social-theme", title="供应链事件", status="complete")
    theme = Theme(slug="supply-chain", name="供应链")
    session.add_all([event, theme])
    session.flush()
    impact = EventSecurityImpact(
        event_id=event.id,
        security_id=apple.id,
        status="complete",
        is_current=True,
        opportunity_score=72,
    )
    session.add(impact)
    session.flush()
    session.add(EventSecurityImpactTheme(impact_id=impact.id, theme_id=theme.id))
    post(session, "401", entities=[], themes=["供应链"], stance="bearish")
    post(session, "402", entities=[], event_id=event.id, stance="bullish")

    context = batch_social_context(session, [apple.id], now=NOW)[apple.id]

    assert context["post_count"] == 2
    assert context["direct_post_count"] == 0
    assert context["theme_post_count"] == context["event_post_count"] == 1
    assert context["stance"] == "unavailable"
    assert context["directional_author_count"] == 0
    assert not context["conflicts"]
    assert impact.opportunity_score == 72
    assert {item["match_basis"] for item in context["posts"]} == {"event", "theme"}
    assert context["posts"][0]["related_event_id"] == event.id


def test_multi_company_post_remains_context_without_company_specific_stance(
    session: Session,
) -> None:
    apple = security(session)
    microsoft = security(session, "MSFT")
    post(session, "501", entities=["AAPL", "MSFT"], stance="bullish")

    contexts = batch_social_context(session, [apple.id, microsoft.id], now=NOW)

    assert all(context["direct_post_count"] == 1 for context in contexts.values())
    assert all(context["stance"] == "unavailable" for context in contexts.values())


def test_ignored_stale_and_future_posts_are_excluded(session: Session) -> None:
    apple = security(session)
    for identifier, kwargs in [
        ("601", {"status": "ignore"}),
        ("602", {"status": "ignored"}),
        ("603", {"classification": "irrelevant"}),
        ("604", {"classification": "promotion"}),
        ("605", {"hours_ago": 169}),
        ("606", {"hours_ago": -1}),
    ]:
        post(session, identifier, **kwargs)  # type: ignore[arg-type]
    post(session, "607")

    context = batch_social_context(session, [apple.id], now=NOW)[apple.id]

    assert context["post_count"] == 1
    assert context["posts"][0]["post_id"] == "607"


def test_missing_collection_is_distinct_from_no_matching_opinion(session: Session) -> None:
    apple = security(session)
    context = batch_social_context(session, [apple.id], now=NOW, enabled=False)[apple.id]

    assert context["status"] == "unavailable"
    assert context["collection"]["enabled"] is False
    assert context["collection"]["missing_accounts"]
    assert any("未开启" in gap for gap in context["gaps"])
    assert any("不代表" in gap for gap in context["gaps"])


def test_collection_reports_failures_and_stale_success_without_exposing_error(
    session: Session,
) -> None:
    apple = security(session)
    post(session, "701", author="healthy")
    post(session, "702", author="failed")
    post(session, "703", author="stale")
    session.add_all(
        [
            SourceHealth(source="X:@healthy", capability="x_posts", last_success_at=NOW),
            SourceHealth(
                source="X:@failed",
                capability="x_posts",
                last_success_at=NOW,
                last_error="internal error details are not copied",
            ),
            SourceHealth(
                source="X:@stale",
                capability="x_posts",
                last_success_at=NOW - timedelta(hours=13),
            ),
        ]
    )
    context = batch_social_context(
        session, [apple.id], now=NOW.replace(tzinfo=None), stale_after_hours=12
    )[apple.id]

    assert context["collection"]["fresh_accounts"] == 1
    assert context["collection"]["failed_accounts"] == ["failed"]
    assert context["collection"]["stale_accounts"] == ["stale"]
    assert "internal error" not in str(context)


def test_empty_and_unknown_security_requests_return_empty(session: Session) -> None:
    assert batch_social_context(session, [], now=NOW) == {}
    assert batch_social_context(session, [-1], now=NOW) == {}
