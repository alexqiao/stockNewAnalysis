from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from trade_news_analysis.services.x_posts import _SCROLL_AND_COLLECT_SCRIPT

playwright_api = pytest.importorskip("playwright.sync_api")

NOW = datetime(2026, 9, 15, 8, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def post_id(*, age_hours: int = 1, sequence: int = 1) -> str:
    timestamp = int((NOW - timedelta(hours=age_hours)).timestamp() * 1000)
    return str(((timestamp - 1288834974657) << 22) + sequence)


def modern_article(
    identifier: str, *, author: str = "example", text: str = "Public announcement"
) -> str:
    return (
        f'<article><a href="/{author}/status/{identifier}">1h</a>'
        f'<div class="whitespace-pre-wrap text-text">{text}</div></article>'
    )


@pytest.fixture(scope="module")
def local_browser() -> Iterator[Any]:
    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error as exc:
            if "executable" in str(exc).lower() or "not found" in str(exc).lower():
                pytest.skip("Local Google Chrome is required for DOM collector tests")
            raise
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def local_page(local_browser: Any) -> Iterator[Any]:
    context = local_browser.new_context()
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


def collect(local_page: Any, html: str, *, max_scrolls: int = 0) -> list[dict[str, Any]]:
    local_page.set_content(f'<base href="https://x.com">{html}')
    local_page.evaluate("now => { Date.now = () => now; }", NOW_MS)
    result: list[dict[str, Any]] = local_page.evaluate(
        _SCROLL_AND_COLLECT_SCRIPT,
        {"handle": "example", "lookbackMs": 24 * 60 * 60 * 1000, "maxScrolls": max_scrolls},
    )
    return result


def test_modern_dom_uses_snowflake_time_and_skips_empty_author_layout(local_page: Any) -> None:
    identifier = post_id(sequence=(1 << 22) - 1)
    html = f"""
    <article>
      <div class="whitespace-pre-wrap"> </div>
      <div class="whitespace-pre-wrap text-gray-700">Pinned</div>
      <a href="/example/status/{identifier}">1h</a>
      <div class="whitespace-pre-wrap text-text">正文<br>第二行</div>
    </article>
    """

    rows = collect(local_page, html)

    assert len(rows) == 1
    assert rows[0]["url"] == f"https://x.com/example/status/{identifier}"
    assert rows[0]["published_at"] == "2026-09-15T07:00:00.000Z"
    assert rows[0]["text"] == "正文\n第二行"
    assert rows[0]["post_type"] == "original"
    assert rows[0]["collection_method"] == "public_profile"


def test_legacy_dom_prefers_explicit_time_and_tweet_text(local_page: Any) -> None:
    html = f"""
    <article data-testid="tweet">
      <a href="/example/status/{post_id(age_hours=40)}">
        <time datetime="2026-09-15T06:00:00Z">2h</time>
      </a>
      <div class="whitespace-pre-wrap">An unrelated layout string</div>
      <div data-testid="tweetText">Legacy announcement</div>
      <button data-testid="like" aria-label="42 Likes">42</button>
    </article>
    """

    rows = collect(local_page, html)

    assert len(rows) == 1
    assert rows[0]["published_at"] == "2026-09-15T06:00:00.000Z"
    assert rows[0]["text"] == "Legacy announcement"
    assert rows[0]["public_metrics"]["likes"] == "42 Likes"
    assert rows[0]["collection_method"] == "browser"


def test_modern_nested_quote_is_attached_without_becoming_an_independent_post(
    local_page: Any,
) -> None:
    original_id = post_id()
    quote_id = post_id(age_hours=2)
    html = f"""
    <article>
      <a href="/example/status/{original_id}">1h</a>
      <div class="whitespace-pre-wrap text-text">My interpretation</div>
      <div role="link">
        <article>
          <a href="/source/status/{quote_id}">2h</a>
          <div class="whitespace-pre-wrap text-text line-clamp-5">Original source</div>
        </article>
      </div>
    </article>
    """

    rows = collect(local_page, html)

    assert len(rows) == 1
    assert rows[0]["post_type"] == "quote"
    assert rows[0]["text"] == "My interpretation"
    assert rows[0]["quoted_post_id"] == quote_id
    assert rows[0]["quoted_author_handle"] == "source"
    assert rows[0]["quoted_text"] == "Original source"
    assert rows[0]["is_truncated"] is False


def test_legacy_quote_uses_second_text_node(local_page: Any) -> None:
    original_id = post_id()
    quote_id = post_id(age_hours=2)
    html = f"""
    <article data-testid="tweet">
      <a href="/example/status/{original_id}"><time datetime="2026-09-15T07:00:00Z"></time></a>
      <div data-testid="tweetText">My interpretation</div>
      <a href="/source/status/{quote_id}">Source</a>
      <div data-testid="tweetText">The source statement</div>
    </article>
    """

    rows = collect(local_page, html)

    assert rows[0]["post_type"] == "quote"
    assert rows[0]["quoted_post_id"] == quote_id
    assert rows[0]["quoted_text"] == "The source statement"


@pytest.mark.parametrize(
    ("author", "text", "expected_type"),
    [
        ("source", "A source post", "repost"),
        ("EXAMPLE", "A case insensitive author match", "original"),
        ("example", "Replying to @source A reply", "reply"),
        ("example", "正在回复 @source 回复正文", "reply"),
    ],
)
def test_collector_preserves_repost_and_reply_classification(
    local_page: Any, author: str, text: str, expected_type: str
) -> None:
    rows = collect(local_page, modern_article(post_id(), author=author, text=text))

    assert rows[0]["post_type"] == expected_type


def test_collector_requires_exact_public_status_paths(local_page: Any) -> None:
    identifier = post_id()
    html = f"""
    <article>
      <a href="/i/status/{identifier}">Internal link</a>
      <a href="/wrong/status/{identifier}/photo/1">Photo</a>
      <a href="https://example.org/wrong/status/{identifier}">External link</a>
      <a href="/example/status/{identifier}">1h</a>
      <div class="whitespace-pre-wrap">The main body</div>
    </article>
    """

    rows = collect(local_page, html)

    assert len(rows) == 1
    assert rows[0]["url"] == f"https://x.com/example/status/{identifier}"
    assert rows[0]["post_type"] == "original"
    assert rows[0]["quoted_post_id"] is None
    assert rows[0]["external_links"] == [f"https://example.org/wrong/status/{identifier}"]


@pytest.mark.parametrize(
    "identifier", [post_id(age_hours=25), post_id(age_hours=-1), "0", str(2**63)]
)
def test_collector_ignores_old_future_or_invalid_snowflakes(
    local_page: Any, identifier: str
) -> None:
    assert collect(local_page, modern_article(identifier)) == []


def test_collector_rejects_future_explicit_timestamp(local_page: Any) -> None:
    html = f"""
    <article><a href="/example/status/{post_id()}">
      <time datetime="2026-09-16T07:00:00Z"></time>
    </a><div data-testid="tweetText">Future dated item</div></article>
    """

    assert collect(local_page, html) == []


def test_collector_removes_show_more_control_and_marks_truncated_text(local_page: Any) -> None:
    html = modern_article(post_id(), text='An excerpt <button>Show more</button>')

    rows = collect(local_page, html)

    assert rows[0]["text"] == "An excerpt"
    assert rows[0]["is_truncated"] is True


@pytest.mark.parametrize(
    "truncated_content",
    [
        '<div class="whitespace-pre-wrap text-text mask-[linear-gradient(...)]">Excerpt</div>',
        '<div class="whitespace-pre-wrap text-text">Excerpt<button>Show more</button></div>',
        '<div class="whitespace-pre-wrap text-text">Excerpt</div>'
        '<a href="/source/superfollows/subscribe">Subscribe</a>',
    ],
)
def test_collector_marks_quote_excerpts_as_truncated(
    local_page: Any, truncated_content: str
) -> None:
    html = f"""
    <article><a href="/example/status/{post_id()}">1h</a>
      <div class="whitespace-pre-wrap text-text">My interpretation</div>
      <article><a href="/source/status/{post_id(age_hours=2)}">2h</a>
        {truncated_content}
      </article>
    </article>
    """

    rows = collect(local_page, html)

    assert rows[0]["is_truncated"] is True
    assert rows[0]["quoted_text"] == "Excerpt"


def test_collector_deduplicates_repeated_posts(local_page: Any) -> None:
    html = modern_article(post_id()) * 2

    assert len(collect(local_page, html)) == 1


def test_collector_collects_posts_added_during_scroll(local_page: Any) -> None:
    initial_id = post_id(sequence=1)
    new_id = post_id(sequence=2)
    local_page.evaluate(
        """html => {
          window.scrollBy = () => document.body.insertAdjacentHTML('beforeend', html);
          window.setTimeout = callback => { callback(); return 0; };
        }""",
        modern_article(new_id, text="Loaded after scrolling"),
    )

    rows = collect(local_page, modern_article(initial_id), max_scrolls=2)

    assert {row["url"] for row in rows} == {
        f"https://x.com/example/status/{initial_id}",
        f"https://x.com/example/status/{new_id}",
    }
