from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest
from pydantic import SecretStr

from trade_news_analysis.config import Settings
from trade_news_analysis.services import x_posts
from trade_news_analysis.services.x_posts import (
    BrowserPost,
    PublicProfileXFetcher,
    ResilientXFetcher,
    XBrowserError,
)


def sample_post() -> BrowserPost:
    return BrowserPost(
        post_id="123", url="https://x.com/example/status/123", post_type="original",
        text="公司更新", published_at=datetime.now(UTC),
    )


def test_auto_falls_back_and_retries_browser_on_next_run(settings: Settings) -> None:
    fetcher = ResilientXFetcher(settings.model_copy(update={"x_fetch_mode": "auto"}))
    browser = Mock(side_effect=XBrowserError("network", "浏览器网络失败"))
    public = Mock(return_value=[sample_post()])
    fetcher.browser.fetch = browser  # type: ignore[method-assign]
    fetcher.public.fetch = public  # type: ignore[method-assign]

    assert fetcher.fetch("example")
    assert fetcher.coverage == "public_page"
    assert fetcher.fetch("second")
    assert browser.call_count == 1
    assert public.call_count == 2
    fetcher.begin_run()
    fetcher.fetch("example")
    assert browser.call_count == 2


def test_account_failure_does_not_disable_browser_for_other_accounts(settings: Settings) -> None:
    fetcher = ResilientXFetcher(settings.model_copy(update={"x_fetch_mode": "auto"}))
    browser = Mock(side_effect=[XBrowserError("http", "HTTP 404"), [sample_post()]])
    fetcher.browser.fetch = browser  # type: ignore[method-assign]
    fetcher.public.fetch = Mock(return_value=[sample_post()])  # type: ignore[method-assign]

    fetcher.fetch("example")
    fetcher.fetch("second")
    assert browser.call_count == 2
    assert fetcher.coverage == "curated"


@pytest.mark.parametrize("mode", ["browser", "public"])
def test_explicit_mode_does_not_use_other_transport(settings: Settings, mode: str) -> None:
    fetcher = ResilientXFetcher(settings.model_copy(update={"x_fetch_mode": mode}))
    selected = Mock(side_effect=XBrowserError("network", "请求失败"))
    other = Mock(side_effect=AssertionError("unexpected transport"))
    fetcher.browser.fetch = selected if mode == "browser" else other  # type: ignore[method-assign]
    fetcher.public.fetch = selected if mode == "public" else other  # type: ignore[method-assign]

    with pytest.raises(XBrowserError, match="请求失败"):
        fetcher.fetch("example")
    selected.assert_called_once()
    other.assert_not_called()


def test_both_failures_are_reported(settings: Settings) -> None:
    fetcher = ResilientXFetcher(settings.model_copy(update={"x_fetch_mode": "auto"}))
    fetcher.browser.fetch = Mock(  # type: ignore[method-assign]
        side_effect=XBrowserError("network", "浏览器网络失败")
    )
    fetcher.public.fetch = Mock(  # type: ignore[method-assign]
        side_effect=XBrowserError("parse", "公开页面无帖子")
    )

    with pytest.raises(XBrowserError, match="浏览器网络失败.*公开页面无帖子"):
        fetcher.fetch("example")


class HtmlResponse(BytesIO):
    def __init__(self, body: bytes, content_type: str = "text/html") -> None:
        super().__init__(body)
        self.headers = Message()
        self.headers["Content-Type"] = content_type


def mock_opener(monkeypatch: pytest.MonkeyPatch, result: object) -> Mock:
    opener = Mock()
    if isinstance(result, Exception):
        opener.open.side_effect = result
    else:
        opener.open.return_value = result
    monkeypatch.setattr(x_posts, "getproxies", lambda: {})
    build = Mock(return_value=opener)
    monkeypatch.setattr(x_posts, "build_opener", build)
    return build


def test_public_fetch_parses_html_and_uses_proxy(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    body = b'''<article data-testid="tweet"><a href="/example/status/1855961006029328823">
    <time datetime="2024-11-11T12:00:00Z"></time></a>
    <div data-testid="tweetText">Company update</div></article>'''
    response = HtmlResponse(body)
    build = mock_opener(monkeypatch, response)
    configured = settings.model_copy(update={
        "x_browser_proxy_url": SecretStr("http://name:p%40ss@proxy:7890")
    })

    posts = PublicProfileXFetcher(configured).fetch("example")

    assert posts[0].text == "Company update"
    assert response.closed
    assert build.call_args.args[0].proxies == {"https": "http://name:p%40ss@proxy:7890"}
    request = build.return_value.open.call_args.args[0]
    assert request.full_url == "https://x.com/example"
    assert not request.has_header("Cookie")


@pytest.mark.parametrize("body,content_type", [
    (b"<html>Log in</html>", "text/html"),
    (b"{}", "application/json"),
    (b"x" * 5_000_001, "text/html"),
])
def test_invalid_public_pages_never_succeed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, body: bytes, content_type: str,
) -> None:
    response = HtmlResponse(body, content_type)
    mock_opener(monkeypatch, response)
    with pytest.raises(XBrowserError) as caught:
        PublicProfileXFetcher(settings).fetch("example")
    assert caught.value.code == "parse"
    assert response.closed


@pytest.mark.parametrize("failure", [
    HTTPError("https://x.com/example", 403, "private-detail", {}, None),  # type: ignore[arg-type]
    URLError("proxy-secret"),
    TimeoutError("private-detail"),
])
def test_public_network_errors_hide_connection_details(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, failure: Exception,
) -> None:
    mock_opener(monkeypatch, failure)
    with pytest.raises(XBrowserError) as caught:
        PublicProfileXFetcher(settings).fetch("example")
    assert "private-detail" not in str(caught.value)
    assert "proxy-secret" not in str(caught.value)


@pytest.mark.parametrize("handle", ["../home", "example?key=value", "", "a" * 16])
def test_invalid_handle_is_rejected_before_request(settings: Settings, handle: str) -> None:
    with pytest.raises(XBrowserError, match="账号格式无效"):
        PublicProfileXFetcher(settings).fetch(handle)


def test_public_redirect_does_not_follow_external_host() -> None:
    handler = x_posts._XRedirectHandler()
    with pytest.raises(XBrowserError, match="非预期跳转"):
        handler.redirect_request(None, None, 302, "", {}, "https://example.org/")
