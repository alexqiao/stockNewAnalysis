from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import SecretStr, ValidationError

from trade_news_analysis.config import Settings
from trade_news_analysis.models import Security
from trade_news_analysis.services.sources import (
    FinnhubCompanyNewsSource,
    GdeltNewsSource,
    SecEdgarFilingsSource,
    SecTickerDirectory,
    build_default_sources,
)

NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def apple() -> Security:
    return Security(
        market="US",
        exchange="NASDAQ",
        symbol="AAPL",
        name="Apple Inc.",
        aliases=["Apple"],
        active=True,
    )


def test_finnhub_company_news_uses_header_key_and_filters_lookback(
    settings: Settings,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_fetcher(url: str, _timeout: float, headers: dict[str, str]) -> Any:
        calls.append((url, headers))
        return [
            {
                "headline": "Apple signs a new supply agreement",
                "summary": "The supplier disclosed a multi-year agreement.",
                "source": "Example Wire",
                "url": "https://example.com/apple?utm_source=finnhub",
                "datetime": int(datetime(2026, 8, 27, 10, tzinfo=UTC).timestamp()),
            },
            {
                "headline": "Old Apple story",
                "source": "Example Wire",
                "url": "https://example.com/old",
                "datetime": int(datetime(2026, 8, 20, 10, tzinfo=UTC).timestamp()),
            },
        ]

    configured = settings.model_copy(
        update={
            "finnhub_api_key": SecretStr("secret-key"),
            "finnhub_news_enabled": True,
            "news_lookback_hours": 48,
        }
    )
    result = FinnhubCompanyNewsSource(
        apple(), configured, fetcher=fake_fetcher, now=lambda: NOW
    ).fetch()

    assert len(result.articles) == 1
    assert result.articles[0].hinted_symbols == {"AAPL"}
    assert result.articles[0].url == "https://example.com/apple"
    assert "secret-key" not in calls[0][0]
    assert calls[0][1]["X-Finnhub-Token"] == "secret-key"


def test_sec_edgar_emits_only_recent_material_filings(settings: Settings) -> None:
    def fake_fetcher(url: str, _timeout: float, _headers: dict[str, str]) -> Any:
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        assert url.endswith("CIK0000320193.json")
        return {
            "filings": {
                "recent": {
                    "form": ["8-K", "4", "10-Q/A", "10-K"],
                    "acceptanceDateTime": [
                        "2026-08-27T10:30:00Z",
                        "2026-08-27T10:00:00Z",
                        "2026-08-26T09:00:00Z",
                        "2026-08-01T09:00:00Z",
                    ],
                    "filingDate": ["2026-08-27", "2026-08-27", "2026-08-26", "2026-08-01"],
                    "accessionNumber": [
                        "0000320193-26-000001",
                        "0000320193-26-000002",
                        "0000320193-26-000003",
                        "0000320193-26-000004",
                    ],
                    "primaryDocument": ["aapl-8k.htm", "form4.xml", "aapl-10q.htm", "aapl-10k.htm"],
                }
            }
        }

    configured = settings.model_copy(update={"news_lookback_hours": 48})
    directory = SecTickerDirectory(configured, fetcher=fake_fetcher)
    result = SecEdgarFilingsSource(
        apple(),
        configured,
        directory,
        fetcher=fake_fetcher,
        now=lambda: NOW,
    ).fetch()

    assert [article.title for article in result.articles] == [
        "Apple Inc. filed 8-K with the SEC",
        "Apple Inc. filed 10-Q with the SEC",
    ]
    assert all(article.hinted_symbols == {"AAPL"} for article in result.articles)
    assert result.articles[0].url.endswith("/000032019326000001/aapl-8k.htm")


def test_gdelt_company_query_normalizes_compact_timestamp(settings: Settings) -> None:
    requested_url = ""
    requested_query = ""

    def fake_fetcher(url: str, _timeout: float, _headers: dict[str, str]) -> Any:
        nonlocal requested_query, requested_url
        requested_url = url
        requested_query = parse_qs(urlsplit(url).query)["query"][0]
        return {
            "articles": [
                {
                    "title": "Apple supplier expands production capacity",
                    "url": "https://example.com/supply-chain?utm_medium=feed",
                    "domain": "example.com",
                    "seendate": "20260827T103000Z",
                    "language": "English",
                }
            ]
        }

    configured = settings.model_copy(
        update={
            "gdelt_base_url": "http://api.gdeltproject.org/api/v2/doc/doc"
        }
    )
    result = GdeltNewsSource([apple()], configured, fetcher=fake_fetcher).fetch()

    assert requested_url.startswith("http://api.gdeltproject.org/")
    assert requested_query == '"Apple Inc."'
    assert len(result.articles) == 1
    assert result.articles[0].source == "GDELT:example.com"
    assert result.articles[0].hinted_symbols == {"AAPL"}
    assert result.articles[0].published_at == datetime(2026, 8, 27, 10, 30, tzinfo=UTC)


def test_gdelt_base_url_rejects_non_official_hosts() -> None:
    with pytest.raises(ValidationError, match="GDELT_BASE_URL"):
        Settings(
            gdelt_base_url="http://127.0.0.1/api/v2/doc/doc",
        )


def test_default_source_factory_adds_optional_providers(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "finnhub_api_key": SecretStr("secret-key"),
            "finnhub_news_enabled": True,
            "sec_edgar_enabled": True,
            "gdelt_news_enabled": True,
        }
    )
    names = {source.name for source in build_default_sources([apple()], configured)}

    assert "Finnhub:AAPL" in names
    assert "SEC EDGAR:AAPL" in names
    assert "GDELT DOC" in names
