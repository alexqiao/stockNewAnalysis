from datetime import UTC, datetime, timedelta

import pytest

from trade_news_analysis.services.x_html import parse_public_profile

POST_ID = "1855961006029328823"
QUOTE_ID = "1855900006029328823"
BODY_CLASS = "font-chirp whitespace-pre-wrap text-text text-body font-normal"


def article(body: str = "原帖正文", handle: str = "alice", post_id: str = POST_ID) -> str:
    return (
        f'<article><a href="/{handle}/status/{post_id}">1h</a>'
        f'<div class="{BODY_CLASS}">{body}</div></article>'
    )


def test_new_dom_extracts_body_and_exact_snowflake_time() -> None:
    html = f"""<article>
      <div class="whitespace-pre-wrap text-gray-700">Pinned</div>
      <a href="/alice">Author</a>
      <div class="{BODY_CLASS}"><span aria-label="Verified account"></span></div>
      <a href="/alice/status/{POST_ID}">Nov 11, 2024</a>
      <div class="{BODY_CLASS}">今天收到 <a href="/search?q=GOOG">$GOOG</a> 的分红<br>第二行</div>
      <a href="/alice/status/{POST_ID}/photo/1"><img src="https://pbs.twimg.com/media/test.jpg"></a>
      <img src="https://pbs.twimg.com/profile_images/avatar.jpg">
      <a href="/i/status/{POST_ID}" aria-label="Reply">88</a>
      <button aria-label="Like"><span>720</span></button>
    </article>"""
    posts = parse_public_profile(html, "alice")
    assert len(posts) == 1
    post = posts[0]
    assert post["text"] == "今天收到 $GOOG 的分红\n第二行"
    assert post["post_type"] == "original"
    assert post["published_at"] == "2024-11-11T13:09:20.582000+00:00"
    assert post["public_metrics"]["likes"] == "720"
    assert post["public_metrics"]["replies"] == "88"
    assert len(post["media"]) == 1
    assert post["collection_method"] == "public_profile"
    assert post["is_truncated"] is False


def test_old_dom_uses_explicit_time_and_tweet_text() -> None:
    html = f"""<article data-testid="tweet">
      <a href="https://twitter.com/Alice/status/{POST_ID}">
        <time datetime="2024-11-11T12:29:20Z"></time></a>
      <div data-testid="tweetText">旧版正文</div>
    </article>"""
    post = parse_public_profile(html, "@alice")[0]
    assert post["published_at"] == "2024-11-11T12:29:20+00:00"
    assert post["text"] == "旧版正文"
    assert post["post_type"] == "original"


def test_nested_quote_remains_part_of_parent_only() -> None:
    html = article("作者评论").replace(
        "</article>", f'<div role="link">{article("引用原文", "bob", QUOTE_ID)}</div></article>'
    )
    posts = parse_public_profile(html, "alice")
    assert len(posts) == 1
    assert posts[0]["text"] == "作者评论"
    assert posts[0]["post_type"] == "quote"
    assert posts[0]["quoted_text"] == "引用原文"
    assert posts[0]["quoted_post_id"] == QUOTE_ID
    assert posts[0]["quoted_author_handle"] == "bob"


def test_old_quote_and_body_status_link_are_distinguished() -> None:
    html = f"""<article data-testid="tweet">
      <a href="/alice/status/{POST_ID}"><time datetime="2024-11-11T12:29:20Z"></time></a>
      <div data-testid="tweetText">评论 <a href="/carol/status/1855961006029328000">链接</a></div>
      <div role="link"><a href="/bob/status/{QUOTE_ID}">
        <time datetime="2024-11-10T00:00:00Z"></time></a>
        <div data-testid="tweetText">引用内容</div></div>
    </article>"""
    post = parse_public_profile(html, "alice")[0]
    assert post["post_type"] == "quote"
    assert post["quoted_author_handle"] == "bob"
    assert post["quoted_text"] == "引用内容"


def test_repost_is_not_attributed_to_the_profile_owner() -> None:
    post = parse_public_profile(article(handle="bob"), "alice")[0]
    assert post["post_type"] == "repost"
    assert post["url"] == f"https://x.com/bob/status/{POST_ID}"


def test_reply_marker_outside_body_classifies_reply() -> None:
    html = article().replace(f'<div class="{BODY_CLASS}">',
                             f'<div>Replying to @bob</div><div class="{BODY_CLASS}">')
    assert parse_public_profile(html, "alice")[0]["post_type"] == "reply"
    post = parse_public_profile(article("Replying to 投资问题"), "alice")[0]
    assert post["post_type"] == "original"


@pytest.mark.parametrize("html", [
    "", "<html><title>X</title><main>登录</main></html>",
    "<article>Loading...</article>", article(post_id="0"), article(post_id="-1"),
    article(post_id="not-a-number"), article(post_id=str(2**63)),
    article(post_id="9" * 300),
    f'<article><a href="https://evil.test/alice/status/{POST_ID}">date</a></article>',
])
def test_empty_or_invalid_posts_fail_without_echoing_html(html: str) -> None:
    with pytest.raises(ValueError, match="X 公开页面没有可解析的帖子") as error:
        parse_public_profile(html, "alice")
    assert "Loading" not in str(error.value)
    assert "evil.test" not in str(error.value)


def test_future_snowflake_is_rejected() -> None:
    future_ms = int((datetime.now(UTC) + timedelta(days=1)).timestamp() * 1000)
    post_id = str((future_ms - 1288834974657) << 22)
    with pytest.raises(ValueError):
        parse_public_profile(article(post_id=post_id), "alice")


@pytest.mark.parametrize("timestamp", ["invalid", "2099-01-01T00:00:00Z", "2024-01-01"])
def test_invalid_or_future_explicit_time_does_not_fall_back(timestamp: str) -> None:
    html = article().replace("1h</a>", f'<time datetime="{timestamp}"></time></a>')
    with pytest.raises(ValueError):
        parse_public_profile(html, "alice")


def test_posts_are_deduplicated_without_dropping_other_valid_posts() -> None:
    posts = parse_public_profile(article() + article() + article(post_id="0"), "alice")
    assert len(posts) == 1


def test_show_more_is_removed_and_marked_truncated() -> None:
    post = parse_public_profile(article("摘要<button>Show more</button>"), "alice")[0]
    assert post["text"] == "摘要"
    assert post["is_truncated"] is True


def test_subscriber_preview_is_marked_truncated() -> None:
    html = article("订阅摘要…").replace("</article>",
        '<a href="/alice/superfollows/subscribe">Subscribe to unlock</a></article>')
    assert parse_public_profile(html, "alice")[0]["is_truncated"] is True


def test_truncated_quote_marks_parent_for_review() -> None:
    quote = article("引用摘要<button>Show more</button>", "bob", QUOTE_ID)
    html = article("作者评论").replace("</article>", f"{quote}</article>")
    post = parse_public_profile(html, "alice")[0]
    assert post["is_truncated"] is True
    assert post["quoted_text"] == "引用摘要"


def test_external_links_are_deduplicated_and_safe_schemes_only() -> None:
    html = article().replace("</article>", """<a href="https://example.com/report">来源</a>
      <a href="https://example.com/report">来源</a><a href="javascript:alert(1)">坏链接</a>
      <a href="https://help.x.com">X帮助</a></article>""")
    assert parse_public_profile(html, "alice")[0]["external_links"] == ["https://example.com/report"]


def test_media_only_post_is_valid_but_empty_post_is_not() -> None:
    html = article("").replace("</article>",
                               '<img src="https://pbs.twimg.com/media/test.jpg"></article>')
    assert parse_public_profile(html, "alice")[0]["text"] == ""
    with pytest.raises(ValueError):
        parse_public_profile(article(""), "alice")
