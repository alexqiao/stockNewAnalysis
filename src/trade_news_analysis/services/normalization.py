"""News normalization and conservative entity matching."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Security

TRACKING_QUERY_PREFIXES = ("utm_", "guccounter", "soc_src", "soc_trk")
TOKEN_RE = re.compile(r"[a-z0-9]+")
CJK_RE = re.compile(r"[\u3400-\u9fff]+")
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "with",
}


@dataclass(slots=True)
class NormalizedArticle:
    source: str
    title: str
    summary: str
    url: str
    published_at: datetime | None
    hinted_symbols: set[str] = field(default_factory=set)
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return article_fingerprint(self.source, self.title, self.published_at, self.url)


def json_safe(value: Any) -> dict[str, Any]:
    serialized = json.dumps(value, default=str, ensure_ascii=False)
    loaded = json.loads(serialized)
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def clean_text(value: Any, limit: int | None = None) -> str:
    text = html.unescape(TAG_RE.sub(" ", str(value or "")))
    text = SPACE_RE.sub(" ", text).strip()
    return text[:limit] if limit else text


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, int | float):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        return ensure_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        try:
            return ensure_aware(parsedate_to_datetime(text))
        except (TypeError, ValueError, OverflowError):
            return None


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(TRACKING_QUERY_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), "")
    )


def normalize_title(title: str) -> str:
    return " ".join(TOKEN_RE.findall(clean_text(title).casefold()))


def title_tokens(title: str) -> set[str]:
    normalized = clean_text(title).casefold()
    tokens = TOKEN_RE.findall(normalized)
    result = {
        token[:-1] if len(token) > 4 and token.endswith("s") else token
        for token in tokens
        if token not in STOPWORDS
    }
    for run in CJK_RE.findall(normalized):
        result.update(run[index : index + 2] for index in range(max(1, len(run) - 1)))
    return result


def title_similarity(left: str, right: str) -> float:
    left_tokens = title_tokens(left)
    right_tokens = title_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def article_fingerprint(
    source: str, title: str, published_at: datetime | None, canonical_url: str
) -> str:
    normalized_url = normalize_url(canonical_url)
    if normalized_url:
        basis = f"url:{normalized_url}"
    else:
        date = ensure_aware(published_at).date().isoformat() if published_at else "unknown"
        basis = f"fallback:{source.casefold()}:{normalize_title(title)}:{date}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _nested_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("url") or "")
    return ""


def normalize_yfinance_item(item: dict[str, Any], symbol: str) -> NormalizedArticle | None:
    raw_content = item.get("content")
    content: dict[str, Any] = raw_content if isinstance(raw_content, dict) else item
    title = clean_text(content.get("title"))
    if not title:
        return None
    provider = content.get("provider")
    if isinstance(provider, dict):
        provider = provider.get("displayName") or provider.get("name")
    source = clean_text(
        provider or content.get("publisher") or item.get("publisher") or "Yahoo Finance"
    )
    summary = clean_text(
        content.get("summary") or content.get("description") or item.get("summary"), limit=4000
    )
    url = (
        _nested_url(content.get("canonicalUrl"))
        or _nested_url(content.get("clickThroughUrl"))
        or _nested_url(content.get("link"))
        or _nested_url(item.get("link"))
        or _nested_url(item.get("url"))
    )
    published = parse_datetime(
        content.get("pubDate")
        or content.get("providerPublishTime")
        or item.get("providerPublishTime")
        or item.get("pubDate")
    )
    return NormalizedArticle(
        source=source,
        title=title,
        summary=summary,
        url=normalize_url(url),
        published_at=published,
        hinted_symbols={symbol.upper()},
        raw_data=json_safe(item),
    )


def normalize_feed_entry(entry: dict[str, Any], source: str) -> NormalizedArticle | None:
    title = clean_text(entry.get("title"))
    url = normalize_url(str(entry.get("link") or entry.get("id") or ""))
    if not title or not url:
        return None
    return NormalizedArticle(
        source=source,
        title=title,
        summary=clean_text(entry.get("summary") or entry.get("description"), limit=4000),
        url=url,
        published_at=parse_datetime(entry.get("published") or entry.get("updated")),
        raw_data=json_safe(entry),
    )


def match_watchlist(
    article: NormalizedArticle, watchlist: list[Security]
) -> dict[str, tuple[str, bool]]:
    """Return explicit matches only; short/ambiguous aliases are intentionally ignored."""
    matches: dict[str, tuple[str, bool]] = {
        symbol: ("source_query", symbol.casefold() in article.title.casefold())
        for symbol in article.hinted_symbols
    }
    title = article.title.casefold()
    haystack = f"{article.title} {article.summary}".casefold()
    for item in watchlist:
        candidates = [item.symbol, item.name, *(item.aliases or [])]
        for candidate in candidates:
            candidate = clean_text(candidate)
            if len(candidate) < 4:
                continue
            pattern = rf"(?<![a-z0-9]){re.escape(candidate.casefold())}(?![a-z0-9])"
            if re.search(pattern, haystack):
                matches.setdefault(item.symbol, ("explicit_alias", bool(re.search(pattern, title))))
                break
    return matches
