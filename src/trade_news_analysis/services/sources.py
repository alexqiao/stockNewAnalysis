"""Keyless news sources with failure isolation at ticker/feed granularity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.request import Request, urlopen

import feedparser
from investormate import Stock

from ..config import Settings
from ..models import Watchlist
from .normalization import NormalizedArticle, normalize_feed_entry, normalize_yfinance_item


@dataclass(slots=True)
class SourceResult:
    source: str
    articles: list[NormalizedArticle] = field(default_factory=list)


class NewsSource(Protocol):
    name: str

    def fetch(self) -> SourceResult: ...


class YFinanceTickerSource:
    def __init__(self, symbol: str, stock_factory: Callable[[str], Any] = Stock):
        self.symbol = symbol
        self.name = f"yfinance:{symbol}"
        self._stock_factory = stock_factory

    def fetch(self) -> SourceResult:
        stock = self._stock_factory(self.symbol)
        articles = []
        for item in stock.news or []:
            if not isinstance(item, dict):
                continue
            normalized = normalize_yfinance_item(item, self.symbol)
            if normalized:
                articles.append(normalized)
        return SourceResult(source=self.name, articles=articles)


class RssNewsSource:
    def __init__(self, name: str, url: str, timeout: float, user_agent: str):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.user_agent = user_agent

    def fetch(self) -> SourceResult:
        request = Request(self.url, headers={"User-Agent": self.user_agent})
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - fixed trusted URLs
            payload = response.read()
        parsed = feedparser.parse(payload)
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(f"invalid RSS: {parsed.get('bozo_exception', 'unknown error')}")
        articles = []
        for entry in parsed.entries:
            normalized = normalize_feed_entry(dict(entry), self.name)
            if normalized:
                articles.append(normalized)
        return SourceResult(source=self.name, articles=articles)


def build_default_sources(watchlist: list[Watchlist], settings: Settings) -> list[NewsSource]:
    sources: list[NewsSource] = [
        YFinanceTickerSource(item.symbol) for item in watchlist if item.active
    ]
    sources.extend(
        [
            RssNewsSource(
                "SEC Press Releases",
                "https://www.sec.gov/news/pressreleases.rss",
                settings.request_timeout_seconds,
                settings.http_user_agent,
            ),
            RssNewsSource(
                "Federal Reserve Press Releases",
                "https://www.federalreserve.gov/feeds/press_all.xml",
                settings.request_timeout_seconds,
                settings.http_user_agent,
            ),
        ]
    )
    return sources
