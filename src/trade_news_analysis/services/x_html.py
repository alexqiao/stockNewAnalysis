"""Parse visible X profile HTML without depending on private timeline APIs."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

_STATUS_PATH = re.compile(r"/([A-Za-z0-9_]{1,15})/status/([0-9]{1,20})/?$")
_BODY_SELECTOR = '[data-testid="tweetText"], div.whitespace-pre-wrap.text-text'
_X_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.twitter.com"}
_FUTURE_TOLERANCE = timedelta(minutes=5)
# Twitter's archived IdWorker uses 12 sequence + 5 worker + 5 datacenter bits.
# https://github.com/twitter-archive/snowflake/blob/snowflake-2010/src/main/scala/com/twitter/service/snowflake/IdWorker.scala
_SNOWFLAKE_EPOCH_MS = 1288834974657


class XProfileUnavailableError(ValueError):
    """The profile explicitly reports that the account cannot be accessed."""


def check_profile_availability(html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("article, script, style"):
        node.decompose()
    headings = soup.select('h1, h2, h3, [role="heading"]')
    if any(node.get_text(" ", strip=True) in {
        "Account suspended", "账号已被冻结", "帳戶已停用"
    } for node in headings):
        raise XProfileUnavailableError(
            "X 页面显示账号已停用（Account suspended），无法采集帖子；"
            "请核对账号状态，恢复前可在博主配置中停用采集"
        )


def _status(anchor: Tag) -> tuple[str, str] | None:
    value = anchor.get("href")
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(urljoin("https://x.com", value))
    except ValueError:
        return None
    match = _STATUS_PATH.fullmatch(url.path)
    if url.scheme not in {"http", "https"} or url.hostname not in _X_HOSTS or not match:
        return None
    handle, post_id = match.groups()
    return (handle, post_id) if handle.casefold() != "i" else None


def _body_nodes(article: Tag) -> list[Tag]:
    candidates = article.select(_BODY_SELECTOR)
    candidate_ids = {id(node) for node in candidates}
    return [
        node for node in candidates
        if node.find_parent("article") is article and node.get_text(strip=True)
        and not any(id(parent) in candidate_ids for parent in node.parents)
    ]


def _status_anchors(article: Tag) -> list[Tag]:
    bodies = _body_nodes(article)
    return [
        anchor for anchor in article.select("a[href]")
        if anchor.find_parent("article") is article and _status(anchor)
        and not any(body is anchor or body in anchor.parents for body in bodies)
    ]


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    fragment = BeautifulSoup(str(node), "html.parser")
    for control in fragment.select("button, script, style"):
        control.decompose()
    for br in fragment.select("br"):
        br.replace_with("\n")
    for image in fragment.select("img[alt]"):
        image.replace_with(str(image.get("alt", "")))
    return fragment.get_text().strip()


def _published_at(anchor: Tag, post_id: str, now: datetime) -> datetime | None:
    snowflake = int(post_id)
    if not 0 < snowflake < 2**63:
        return None
    encoded = datetime.fromtimestamp(((snowflake >> 22) + _SNOWFLAKE_EPOCH_MS) / 1000, UTC)
    if encoded > now + _FUTURE_TOLERANCE:
        return None
    time = anchor.find("time")
    if time is not None and isinstance(value := time.get("datetime"), str):
        try:
            published = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if published.tzinfo is None:
                return None
            return published if published <= now + _FUTURE_TOLERANCE else None
        except ValueError:
            return None
    return encoded


def _truncated(article: Tag, body: Tag | None) -> bool:
    if article.select_one('a[href*="/superfollows/subscribe"]'):
        return True
    if body is None:
        return False
    classes = str(body.get("class", ""))
    return "mask-" in classes or any(
        button.get_text(strip=True).casefold() in {"show more", "显示更多", "顯示更多"}
        for button in body.select("button")
    )


def _links_and_media(article: Tag) -> tuple[list[str], list[dict[str, str]]]:
    links: list[str] = []
    for anchor in article.select("a[href]"):
        href = urljoin("https://x.com", str(anchor.get("href", "")))
        try:
            parsed = urlsplit(href)
        except ValueError:
            continue
        if parsed.scheme in {"http", "https"} and parsed.hostname and not any(
            parsed.hostname == host or parsed.hostname.endswith("." + host)
            for host in ("x.com", "twitter.com")
        ) and href not in links:
            links.append(href)
    media = []
    for node in article.select("img[src], video[poster]"):
        url = str(node.get("src") or node.get("poster") or "")
        if not url.startswith(("https://", "http://")) or "profile_images" in url:
            continue
        media.append({
            "type": "video" if node.name == "video" else "image",
            "url": url,
            "alt": str(node.get("alt", "")),
        })
    return links, media


def _parse_article(article: Tag, handle: str, now: datetime) -> dict[str, Any] | None:
    anchors = _status_anchors(article)
    if not anchors:
        return None
    anchor = next((item for item in anchors if item.find("time")), anchors[0])
    status = _status(anchor)
    if status is None:
        return None
    author, post_id = status
    published_at = _published_at(anchor, post_id, now)
    if published_at is None:
        return None
    bodies = _body_nodes(article)
    body = bodies[0] if bodies else None
    nested_quote = article.find("article")
    quote_anchors = _status_anchors(nested_quote) if nested_quote else anchors
    quote_anchor = next((item for item in quote_anchors if _status(item) != status), None)
    quote = _status(quote_anchor) if quote_anchor else None
    quote_bodies = _body_nodes(nested_quote) if nested_quote else bodies[1:]
    quote_body = quote_bodies[0] if quote_bodies else None
    text = _text(body)
    external_links, media = _links_and_media(article)
    if not text and not media and not quote:
        return None
    # Reply labels appear outside the post body; quoted text must not classify the parent.
    body_ids = {id(node) for node in bodies}
    labels = " ".join(str(item) for item in article.find_all(string=True)
                      if not any(id(parent) in body_ids for parent in item.parents))
    post_type = "original"
    if author.casefold() != handle.removeprefix("@").casefold():
        post_type = "repost"
    elif quote:
        post_type = "quote"
    elif "Replying to" in labels or "正在回复" in labels or "回覆給" in labels:
        post_type = "reply"
    metrics = {}
    for key, test_id, label in (
        ("replies", "reply", "Reply"), ("reposts", "retweet", "Repost"),
        ("likes", "like", "Like"), ("views", "app-text-transition-container", "View count"),
    ):
        node = article.select_one(f'[data-testid="{test_id}"], [aria-label="{label}"]')
        metrics[key] = node.get_text(strip=True) if node else ""
    return {
        "url": f"https://x.com/{author}/status/{post_id}",
        "post_type": post_type,
        "text": text,
        "published_at": published_at.isoformat(),
        "quoted_post_id": quote[1] if quote else None,
        "quoted_author_handle": quote[0] if quote else None,
        "quoted_text": _text(quote_body) if quote else "",
        "external_links": external_links,
        "media": media,
        "public_metrics": metrics,
        "collection_method": "public_profile",
        "is_truncated": _truncated(article, body) or _truncated(
            nested_quote if nested_quote is not None else article, quote_body
        ),
    }


def parse_public_profile(html: str, handle: str) -> list[dict[str, Any]]:
    """Return top-level visible posts; missing/invalid post content is a parse failure."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script, style"):
        node.decompose()
    now = datetime.now(UTC)
    posts: dict[str, dict[str, Any]] = {}
    for article in soup.select("article"):
        if article.find_parent("article") is not None:
            continue
        if payload := _parse_article(article, handle, now):
            posts[payload["url"]] = payload
    if not posts:
        check_profile_availability(html)
        raise ValueError("X 公开页面没有可解析的帖子；可能需要登录或页面结构已变化")
    return list(posts.values())
