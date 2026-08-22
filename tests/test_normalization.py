from __future__ import annotations

from datetime import UTC

from sqlalchemy.orm import Session

from trade_news_analysis.models import Security
from trade_news_analysis.services.normalization import (
    NormalizedArticle,
    match_watchlist,
    normalize_feed_entry,
    normalize_url,
    normalize_yfinance_item,
    title_similarity,
)


def test_normalizes_legacy_yfinance_news() -> None:
    result = normalize_yfinance_item(
        {
            "title": "Apple expands paid AI service",
            "summary": "Customers are adopting the service.",
            "publisher": "Example Wire",
            "link": "https://example.com/news?utm_source=yahoo&id=7",
            "providerPublishTime": 1_700_000_000,
        },
        "aapl",
    )
    assert result is not None
    assert result.source == "Example Wire"
    assert result.hinted_symbols == {"AAPL"}
    assert result.url == "https://example.com/news?id=7"
    assert result.published_at is not None
    assert result.published_at.tzinfo == UTC


def test_normalizes_nested_yfinance_news() -> None:
    result = normalize_yfinance_item(
        {
            "content": {
                "title": "NVIDIA announces new shipment",
                "summary": "Suppliers started shipping.",
                "provider": {"displayName": "Publisher"},
                "canonicalUrl": {"url": "https://example.com/nvda#section"},
                "pubDate": "2026-08-01T12:00:00Z",
            }
        },
        "NVDA",
    )
    assert result is not None
    assert result.url == "https://example.com/nvda"
    assert result.source == "Publisher"


def test_feed_requires_title_and_link() -> None:
    assert normalize_feed_entry({"title": "Only a title"}, "SEC") is None
    result = normalize_feed_entry(
        {
            "title": "SEC charges Example Corp",
            "link": "https://sec.gov/release/1",
            "summary": "<b>Enforcement</b> action",
            "published": "Fri, 21 Aug 2026 12:00:00 GMT",
        },
        "SEC",
    )
    assert result is not None
    assert result.summary == "Enforcement action"


def test_explicit_entity_matching_is_per_company(session: Session) -> None:
    watchlist = [
        Security(
            market="US",
            exchange="NASDAQ",
            symbol="AAPL",
            name="Apple Inc.",
            aliases=["Apple"],
        ),
        Security(
            market="US",
            exchange="NASDAQ",
            symbol="MSFT",
            name="Microsoft Corporation",
            aliases=["Microsoft"],
        ),
    ]
    article = NormalizedArticle(
        source="Macro",
        title="Apple signs cloud agreement with Microsoft",
        summary="The two companies disclosed the contract.",
        url="https://example.com/deal",
        published_at=None,
    )
    matches = match_watchlist(article, watchlist)
    assert set(matches) == {"AAPL", "MSFT"}
    assert matches["AAPL"] == ("explicit_alias", True)
    assert matches["MSFT"] == ("explicit_alias", True)


def test_url_and_story_similarity() -> None:
    assert normalize_url("HTTPS://Example.COM/a/?utm_medium=x&b=2&a=1#top") == (
        "https://example.com/a?a=1&b=2"
    )
    assert (
        title_similarity(
            "Nvidia launches a new AI chip for data centers",
            "Nvidia launches new AI chip for the data center",
        )
        >= 0.85
    )
    assert title_similarity("朱雀三号成功完成火箭回收", "朱雀三号成功完成火箭回收！") == 1
