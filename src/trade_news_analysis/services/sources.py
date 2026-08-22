"""Broad and security-scoped news sources with failure isolation."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.request import Request, urlopen

import feedparser
from investormate import Stock

from ..config import Settings
from ..models import Security
from .normalization import (
    NormalizedArticle,
    normalize_feed_entry,
    normalize_url,
    normalize_yfinance_item,
)
from .providers import TushareClient, YahooMarketDataProvider


@dataclass(slots=True)
class SourceResult:
    source: str
    articles: list[NormalizedArticle] = field(default_factory=list)


class NewsSource(Protocol):
    name: str
    markets: tuple[str, ...]
    coverage: str

    def fetch(self) -> SourceResult: ...


class YFinanceTickerSource:
    markets: tuple[str, ...] = ("HK", "US")
    coverage = "tracked"

    def __init__(self, security: Security, stock_factory: Callable[[str], Any] = Stock):
        self.security = security
        self.name = f"yfinance:{security.market}:{security.symbol}"
        self._stock_factory = stock_factory

    def fetch(self) -> SourceResult:
        symbol = YahooMarketDataProvider._symbol(self.security.market, self.security.symbol)
        stock = self._stock_factory(symbol)
        articles = []
        for item in stock.news or []:
            if not isinstance(item, dict):
                continue
            normalized = normalize_yfinance_item(item, self.security.symbol)
            if normalized:
                articles.append(normalized)
        return SourceResult(source=self.name, articles=articles)


class RssNewsSource:
    markets: tuple[str, ...] = ()
    coverage = "partial"

    def __init__(
        self,
        name: str,
        url: str,
        timeout: float,
        user_agent: str,
        markets: tuple[str, ...],
    ):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.user_agent = user_agent
        self.markets = markets

    def fetch(self) -> SourceResult:
        request = Request(self.url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed URLs
            payload = response.read()
        parsed = feedparser.parse(payload)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"invalid RSS: {parsed.get('bozo_exception', 'unknown error')}")
        articles = [
            item
            for entry in parsed.entries
            if (item := normalize_feed_entry(dict(entry), self.name)) is not None
        ]
        return SourceResult(source=self.name, articles=articles)


class TushareNewsSource:
    markets: tuple[str, ...] = ("A", "HK", "US")
    coverage = "broad"

    def __init__(self, settings: Settings):
        if not settings.tushare_token:
            raise RuntimeError("TUSHARE_TOKEN 未配置")
        self.name = "Tushare News"
        self.client = TushareClient(
            settings.tushare_token.get_secret_value(), settings.request_timeout_seconds
        )

    def fetch(self) -> SourceResult:
        end = datetime.now(UTC)
        start = end - timedelta(hours=2)
        articles: list[NormalizedArticle] = []
        for source in ("sina", "eastmoney", "cls"):
            rows = self.client.query(
                "news",
                {
                    "src": source,
                    "start_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_date": end.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "datetime,content,title,channels",
            )
            for row in rows:
                title = str(row.get("title") or row.get("content") or "")[:500].strip()
                if not title:
                    continue
                published = datetime.fromisoformat(str(row["datetime"])).replace(tzinfo=UTC)
                articles.append(
                    NormalizedArticle(
                        source=f"Tushare:{source}",
                        title=title,
                        summary=str(row.get("content") or "")[:4000],
                        url="",
                        published_at=published,
                        raw_data=row,
                    )
                )
        return SourceResult(source=self.name, articles=articles)


class AkShareBroadNewsSource:
    markets: tuple[str, ...] = ("A", "HK", "US")
    coverage = "partial"
    name = "AKShare:财经精选"

    def fetch(self) -> SourceResult:
        import akshare as ak

        frame = ak.stock_news_main_cx()
        articles = []
        for row in frame.to_dict("records"):
            summary = str(row.get("summary") or "").strip()
            title = str(row.get("tag") or summary[:120]).strip()
            url = normalize_url(str(row.get("url") or ""))
            if title and url:
                articles.append(
                    NormalizedArticle(
                        source=self.name,
                        title=title,
                        summary=summary[:4000],
                        url=url,
                        published_at=None,
                        raw_data=row,
                    )
                )
        return SourceResult(source=self.name, articles=articles)


def build_default_sources(
    tracked_securities: list[Security], settings: Settings
) -> list[NewsSource]:
    sources: list[NewsSource] = [
        YFinanceTickerSource(item)
        for item in tracked_securities
        if item.active and item.market in {"HK", "US"}
    ]
    if settings.tushare_configured and settings.tushare_news_enabled:
        sources.append(TushareNewsSource(settings))
    elif settings.akshare_enabled and importlib.util.find_spec("akshare") is not None:
        sources.append(AkShareBroadNewsSource())
    sources.extend(
        [
            RssNewsSource(
                "SEC Press Releases",
                "https://www.sec.gov/news/pressreleases.rss",
                settings.request_timeout_seconds,
                settings.http_user_agent,
                ("US",),
            ),
            RssNewsSource(
                "Federal Reserve Press Releases",
                "https://www.federalreserve.gov/feeds/press_all.xml",
                settings.request_timeout_seconds,
                settings.http_user_agent,
                ("A", "HK", "US"),
            ),
        ]
    )
    return sources
