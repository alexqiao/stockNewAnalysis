from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch

from trade_news_analysis.config import Settings
from trade_news_analysis.services import x_posts
from trade_news_analysis.services.x_posts import PlaywrightXFetcher, XBrowserError


class FakePage:
    def __init__(
        self,
        payloads: object = None,
        *,
        navigation_error: Exception | None = None,
        redirect_url: str | None = None,
        response_status: int | None = 200,
        selector_error: Exception | None = None,
        html: str = "",
    ) -> None:
        self.payloads = payloads
        self.navigation_error = navigation_error
        self.redirect_url = redirect_url
        self.response_status = response_status
        self.selector_error = selector_error
        self.html = html
        self.url = "about:blank"
        self.goto_count = 0
        self.selector_count = 0
        self.evaluate_count = 0

    def goto(self, url: str, **_kwargs: object) -> SimpleNamespace | None:
        self.goto_count += 1
        if self.navigation_error is not None:
            raise self.navigation_error
        self.url = self.redirect_url or url
        if self.response_status is None:
            return None
        return SimpleNamespace(status=self.response_status)

    def wait_for_timeout(self, _milliseconds: int) -> None:
        pass

    def wait_for_selector(self, _selector: str, **_kwargs: object) -> None:
        self.selector_count += 1
        if self.selector_error:
            raise self.selector_error

    def content(self) -> str:
        return self.html

    def evaluate(self, _script: str, _arguments: object) -> object:
        self.evaluate_count += 1
        return self.payloads


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.pages = [page]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.kwargs: dict[str, object] = {}

    def launch_persistent_context(self, **kwargs: object) -> FakeContext:
        self.kwargs = kwargs
        return self.context


def fake_browser(
    monkeypatch: MonkeyPatch, settings: Settings, page: FakePage
) -> tuple[PlaywrightXFetcher, FakeContext, FakeChromium]:
    context = FakeContext(page)
    chromium = FakeChromium(context)
    playwright = SimpleNamespace(chromium=chromium)
    fetcher = PlaywrightXFetcher(settings)
    monkeypatch.setattr(x_posts, "getproxies", lambda: {})
    monkeypatch.setattr(fetcher, "_playwright", lambda: lambda: nullcontext(playwright))
    return fetcher, context, chromium


@pytest.mark.parametrize(
    ("proxies", "expected_server"),
    [
        (
            {"https": "http://secure-proxy:8443", "http": "http://fallback:8080"},
            "http://secure-proxy:8443",
        ),
        (
            {"http": "http://web-proxy:8080", "all": "socks5://fallback:1080"},
            "http://web-proxy:8080",
        ),
        ({"all": "socks5://all-proxy:1080"}, "socks5://all-proxy:1080"),
        ({"https": "localhost:7890"}, "http://localhost:7890"),
    ],
)
def test_browser_proxy_uses_system_proxy_priority(
    monkeypatch: MonkeyPatch,
    settings: Settings,
    proxies: dict[str, str],
    expected_server: str,
) -> None:
    monkeypatch.setattr(x_posts, "getproxies", lambda: proxies)
    configured = settings.model_copy(update={"x_browser_proxy_url": None})

    assert PlaywrightXFetcher(configured)._proxy() == {"server": expected_server}


def test_browser_proxy_explicit_secret_overrides_system(
    monkeypatch: MonkeyPatch, settings: Settings
) -> None:
    def unexpected_system_proxy_read() -> dict[str, str]:
        pytest.fail("An explicit proxy must not require reading the system proxy")

    monkeypatch.setattr(x_posts, "getproxies", unexpected_system_proxy_read)
    configured = settings.model_copy(
        update={"x_browser_proxy_url": SecretStr("http://explicit-proxy:7890")}
    )

    assert PlaywrightXFetcher(configured)._proxy() == {"server": "http://explicit-proxy:7890"}


def test_browser_proxy_without_configuration_returns_none(
    monkeypatch: MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(x_posts, "getproxies", lambda: {})
    configured = settings.model_copy(update={"x_browser_proxy_url": None})

    assert PlaywrightXFetcher(configured)._proxy() is None


def test_browser_proxy_separates_credentials_from_server(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "x_browser_proxy_url": SecretStr("http://test%40user:test%3Apassword@[::1]:7890/")
        }
    )

    proxy = PlaywrightXFetcher(configured)._proxy()

    assert proxy == {
        "server": "http://[::1]:7890",
        "username": "test@user",
        "password": "test:password",
    }
    assert "user" not in proxy["server"]
    assert "password" not in proxy["server"]


@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://test-user:test-secret@proxy:bad-port",
        "ftp://test-user:test-secret@proxy:8080",
        "http://test-user:test-secret@proxy:8080/private-path",
        "http://test-user:test-secret@proxy:8080?token=test-token",
        "http://test-user:test-secret@[invalid-ipv6:8080",
    ],
)
def test_browser_proxy_invalid_url_does_not_expose_credentials(
    settings: Settings, proxy_url: str
) -> None:
    configured = settings.model_copy(update={"x_browser_proxy_url": SecretStr(proxy_url)})

    with pytest.raises(XBrowserError) as caught:
        PlaywrightXFetcher(configured)._proxy()

    assert caught.value.code == "proxy"
    assert "X_BROWSER_PROXY_URL" in str(caught.value)
    assert proxy_url not in str(caught.value)
    assert "test-secret" not in str(caught.value)
    assert "test-user" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


@pytest.mark.parametrize("headless", [True, False])
@pytest.mark.parametrize("proxy_url", [None, "http://configured-proxy:7890"])
def test_browser_context_passes_proxy_and_headless_settings(
    monkeypatch: MonkeyPatch,
    settings: Settings,
    headless: bool,
    proxy_url: str | None,
) -> None:
    configured = settings.model_copy(
        update={
            "x_browser_headless": headless,
            "x_browser_proxy_url": SecretStr(proxy_url) if proxy_url else None,
        }
    )
    fetcher, context, chromium = fake_browser(monkeypatch, configured, FakePage([]))

    assert fetcher.fetch("example") == []

    assert chromium.kwargs["headless"] is headless
    assert chromium.kwargs["channel"] == "chrome"
    assert chromium.kwargs["user_data_dir"] == str(configured.x_browser_profile_path)
    if proxy_url:
        assert chromium.kwargs["proxy"] == {"server": proxy_url}
    else:
        assert "proxy" not in chromium.kwargs
    assert context.closed


@pytest.mark.parametrize(
    ("error_message", "expected_code"),
    [
        ("page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://x.com/example", "network"),
        ("page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x.com/example", "network"),
        ("page.goto: net::ERR_CONNECTION_RESET at https://x.com/example", "network"),
        ("page.goto: Timeout 60000ms exceeded for private-navigation-details", "page"),
    ],
)
def test_browser_fetch_classifies_navigation_errors_and_closes_context(
    monkeypatch: MonkeyPatch,
    settings: Settings,
    error_message: str,
    expected_code: str,
) -> None:
    page = FakePage(navigation_error=RuntimeError(error_message))
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, page)

    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")

    assert caught.value.code == expected_code
    assert error_message not in str(caught.value)
    assert page.goto_count == 2
    assert page.evaluate_count == 0
    assert context.closed


@pytest.mark.parametrize("redirect_url", ["https://x.com/i/flow/login", "https://x.com/login"])
def test_browser_fetch_login_redirect_fails_without_retrying(
    monkeypatch: MonkeyPatch, settings: Settings, redirect_url: str
) -> None:
    page = FakePage(redirect_url=redirect_url)
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, page)

    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")

    assert caught.value.code == "auth"
    assert "x-login" in str(caught.value)
    assert page.goto_count == 1
    assert page.selector_count == 0
    assert page.evaluate_count == 0
    assert context.closed


def test_browser_fetch_http_forbidden_reports_http_error_and_closes_context(
    monkeypatch: MonkeyPatch, settings: Settings
) -> None:
    page = FakePage(response_status=403)
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, page)

    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")

    assert caught.value.code == "http"
    assert "403" in str(caught.value)
    assert page.evaluate_count == 0
    assert context.closed


def test_browser_suspended_account_is_reported_without_retry(
    monkeypatch: MonkeyPatch, settings: Settings,
) -> None:
    page = FakePage(
        selector_error=TimeoutError("no articles"),
        html="<main><h2>Account suspended</h2></main>",
    )
    fetcher, context, _ = fake_browser(monkeypatch, settings, page)

    with pytest.raises(XBrowserError, match="账号已停用") as caught:
        fetcher.fetch("example")

    assert caught.value.code == "account"
    assert page.goto_count == 1
    assert page.evaluate_count == 0
    assert context.closed


def test_browser_missing_articles_without_suspension_still_retries(
    monkeypatch: MonkeyPatch, settings: Settings,
) -> None:
    page = FakePage(selector_error=TimeoutError("no articles"), html="<main>Loading</main>")
    fetcher, context, _ = fake_browser(monkeypatch, settings, page)

    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")

    assert caught.value.code == "page"
    assert page.goto_count == 2
    assert context.closed


def test_browser_script_exception_is_sanitized_for_automatic_fallback(
    monkeypatch: MonkeyPatch, settings: Settings,
) -> None:
    page = FakePage([])
    page.evaluate = Mock(side_effect=Exception("private browser details"))  # type: ignore[method-assign]
    fetcher, context, _ = fake_browser(monkeypatch, settings, page)
    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")
    assert caught.value.code == "parse"
    assert "private browser details" not in str(caught.value)
    assert context.closed


@pytest.mark.parametrize("payloads", [None, {}, "unexpected", [{"url": "not-a-post"}], [None]])
def test_browser_fetch_rejects_invalid_extraction_results(
    monkeypatch: MonkeyPatch, settings: Settings, payloads: object
) -> None:
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, FakePage(payloads))

    with pytest.raises(XBrowserError) as caught:
        fetcher.fetch("example")

    assert caught.value.code == "parse"
    assert context.closed


def test_browser_fetch_valid_empty_list_is_successful(
    monkeypatch: MonkeyPatch, settings: Settings
) -> None:
    page = FakePage([], response_status=None)
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, page)

    assert fetcher.fetch("example") == []
    assert page.goto_count == 1
    assert page.evaluate_count == 1
    assert context.closed


def test_browser_fetch_returns_valid_post_with_unparseable_entries_ignored(
    monkeypatch: MonkeyPatch, settings: Settings
) -> None:
    page = FakePage(
        [
            None,
            {"url": "https://x.com/example"},
            {
                "url": "https://x.com/example/status/12345",
                "text": "A new public announcement",
                "published_at": "2026-09-15T01:00:00Z",
            },
        ]
    )
    fetcher, context, _chromium = fake_browser(monkeypatch, settings, page)

    posts = fetcher.fetch("example")

    assert [post.post_id for post in posts] == ["12345"]
    assert posts[0].text == "A new public announcement"
    assert context.closed
