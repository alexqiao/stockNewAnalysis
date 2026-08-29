"""Broad and security-scoped news sources with failure isolation."""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import feedparser
from investormate import Stock

from ..config import Settings
from ..models import Security
from .normalization import (
    NormalizedArticle,
    clean_text,
    json_safe,
    match_watchlist,
    normalize_feed_entry,
    normalize_url,
    normalize_yfinance_item,
    parse_datetime,
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


JsonFetcher = Callable[[str, float, dict[str, str]], Any]


def fetch_json(url: str, timeout: float, headers: dict[str, str]) -> Any:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed provider hosts
        body = response.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            content_type = response.headers.get_content_type()
            raise RuntimeError(
                f"JSON provider returned invalid {content_type} content"
            ) from exc


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


class FinnhubCompanyNewsSource:
    markets: tuple[str, ...] = ("US",)
    coverage = "tracked"
    base_url = "https://finnhub.io/api/v1/company-news"

    def __init__(
        self,
        security: Security,
        settings: Settings,
        fetcher: JsonFetcher = fetch_json,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.security = security
        self.settings = settings
        self.fetcher = fetcher
        self.now = now
        self.name = f"Finnhub:{security.symbol}"

    def fetch(self) -> SourceResult:
        if not self.settings.finnhub_api_key:
            raise RuntimeError("FINNHUB_API_KEY 未配置")
        current = self.now()
        cutoff = current - timedelta(hours=self.settings.news_lookback_hours)
        query = urlencode(
            {
                "symbol": self.security.symbol,
                "from": cutoff.date().isoformat(),
                "to": current.date().isoformat(),
            }
        )
        payload = self.fetcher(
            f"{self.base_url}?{query}",
            self.settings.request_timeout_seconds,
            {
                "User-Agent": self.settings.http_user_agent,
                "X-Finnhub-Token": self.settings.finnhub_api_key.get_secret_value(),
            },
        )
        if not isinstance(payload, list):
            raise RuntimeError("Finnhub company-news 返回了无效数据")
        articles = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            title = clean_text(row.get("headline"))
            published = parse_datetime(row.get("datetime"))
            if not title or (published is not None and published < cutoff):
                continue
            articles.append(
                NormalizedArticle(
                    source=clean_text(row.get("source")) or "Finnhub",
                    title=title,
                    summary=clean_text(row.get("summary"), limit=4000),
                    url=normalize_url(str(row.get("url") or "")),
                    published_at=published,
                    hinted_symbols={self.security.symbol},
                    raw_data=json_safe(row),
                )
            )
        return SourceResult(source=self.name, articles=articles)


class SecTickerDirectory:
    url = "https://www.sec.gov/files/company_tickers.json"

    def __init__(
        self,
        settings: Settings,
        fetcher: JsonFetcher = fetch_json,
    ):
        self.settings = settings
        self.fetcher = fetcher
        self._mapping: dict[str, int] | None = None

    def cik_for(self, symbol: str) -> int | None:
        if self._mapping is None:
            payload = self.fetcher(
                self.url,
                self.settings.request_timeout_seconds,
                {"User-Agent": self.settings.http_user_agent},
            )
            if not isinstance(payload, dict):
                raise RuntimeError("SEC ticker directory 返回了无效数据")
            mapping: dict[str, int] = {}
            for row in payload.values():
                if not isinstance(row, dict):
                    continue
                ticker = str(row.get("ticker") or "").strip().upper()
                cik = row.get("cik_str")
                if ticker and isinstance(cik, int):
                    mapping[ticker] = cik
            self._mapping = mapping
        normalized = symbol.strip().upper()
        return self._mapping.get(normalized) or self._mapping.get(normalized.replace(".", "-"))


class SecEdgarFilingsSource:
    markets: tuple[str, ...] = ("US",)
    coverage = "tracked"
    forms = frozenset({"8-K", "6-K", "10-Q", "10-K"})
    descriptions = {
        "8-K": "current report",
        "6-K": "foreign issuer report",
        "10-Q": "quarterly report",
        "10-K": "annual report",
    }

    def __init__(
        self,
        security: Security,
        settings: Settings,
        directory: SecTickerDirectory,
        fetcher: JsonFetcher = fetch_json,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.security = security
        self.settings = settings
        self.directory = directory
        self.fetcher = fetcher
        self.now = now
        self.name = f"SEC EDGAR:{security.symbol}"

    def fetch(self) -> SourceResult:
        cik = self.directory.cik_for(self.security.symbol)
        if cik is None:
            return SourceResult(source=self.name)
        payload = self.fetcher(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            self.settings.request_timeout_seconds,
            {"User-Agent": self.settings.http_user_agent},
        )
        try:
            recent = payload["filings"]["recent"]
            forms = recent["form"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("SEC submissions 返回了无效数据") from exc
        if not isinstance(forms, list):
            raise RuntimeError("SEC submissions 返回了无效数据")
        cutoff = self.now() - timedelta(hours=self.settings.news_lookback_hours)
        articles = []
        for index, raw_form in enumerate(forms):
            form = str(raw_form or "").removesuffix("/A")
            if form not in self.forms:
                continue
            row = {
                key: values[index]
                for key, values in recent.items()
                if isinstance(values, list) and index < len(values)
            }
            published = parse_datetime(
                row.get("acceptanceDateTime") or row.get("filingDate")
            )
            if published is not None and published < cutoff:
                continue
            accession = str(row.get("accessionNumber") or "").strip()
            primary_document = str(row.get("primaryDocument") or "").strip()
            if not accession:
                continue
            archive_dir = accession.replace("-", "")
            if primary_document:
                document = primary_document
            else:
                document = f"{accession}-index.html"
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{archive_dir}/{document}"
            description = self.descriptions[form]
            articles.append(
                NormalizedArticle(
                    source="SEC EDGAR",
                    title=f"{self.security.name} filed {form} with the SEC",
                    summary=f"{self.security.symbol} submitted a {description} ({form}).",
                    url=normalize_url(url),
                    published_at=published,
                    hinted_symbols={self.security.symbol},
                    raw_data=json_safe(row),
                )
            )
        return SourceResult(source=self.name, articles=articles)


class GdeltNewsSource:
    coverage = "tracked"

    def __init__(
        self,
        securities: list[Security],
        settings: Settings,
        fetcher: JsonFetcher = fetch_json,
    ):
        self.securities = securities
        self.settings = settings
        self.fetcher = fetcher
        self.markets: tuple[str, ...] = tuple(
            sorted({security.market for security in securities})
        )
        self.name = "GDELT DOC"

    @staticmethod
    def _is_specific_name(value: str) -> bool:
        return len(value) >= 6 or (
            len(value) >= 2 and any("\u3400" <= char <= "\u9fff" for char in value)
        )

    def fetch(self) -> SourceResult:
        terms = []
        for security in self.securities:
            candidates = [security.name, *(security.aliases or [])]
            for candidate in candidates:
                normalized = clean_text(candidate).replace('"', "")
                if (
                    normalized
                    and self._is_specific_name(normalized)
                    and normalized not in terms
                ):
                    terms.append(normalized)
        if not terms:
            return SourceResult(source=self.name)
        selected_terms = terms[:24]
        expression = " OR ".join(f'"{term}"' for term in selected_terms)
        query = f"({expression})" if len(selected_terms) > 1 else expression
        params = {
            "query": query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": 50,
            "sort": "datedesc",
            "timespan": f"{self.settings.news_lookback_hours}h",
        }
        url = f"{self.settings.gdelt_base_url}?{urlencode(params)}"
        payload = self.fetcher(
            url,
            self.settings.request_timeout_seconds,
            {"User-Agent": self.settings.http_user_agent},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise RuntimeError("GDELT DOC 返回了无效数据")
        articles = []
        for row in payload["articles"]:
            if not isinstance(row, dict):
                continue
            title = clean_text(row.get("title"))
            url = normalize_url(str(row.get("url") or ""))
            if not title or not url:
                continue
            domain = clean_text(row.get("domain"))
            article = NormalizedArticle(
                source=f"GDELT:{domain}" if domain else "GDELT",
                title=title,
                summary="",
                url=url,
                published_at=parse_datetime(row.get("seendate")),
                raw_data=json_safe(row),
            )
            article.hinted_symbols = set(match_watchlist(article, self.securities))
            articles.append(article)
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
    active_securities = [item for item in tracked_securities if item.active]
    sources: list[NewsSource] = [
        YFinanceTickerSource(item)
        for item in active_securities
        if item.market in {"HK", "US"}
    ]
    if settings.finnhub_configured and settings.finnhub_news_enabled:
        sources.extend(
            FinnhubCompanyNewsSource(item, settings)
            for item in active_securities
            if item.market == "US"
        )
    if settings.sec_edgar_enabled:
        sec_directory = SecTickerDirectory(settings)
        sources.extend(
            SecEdgarFilingsSource(item, settings, sec_directory)
            for item in active_securities
            if item.market == "US"
        )
    if settings.gdelt_news_enabled:
        sources.append(GdeltNewsSource(active_securities, settings))
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
