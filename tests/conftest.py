from __future__ import annotations

import os
import re
import socket
import sys
from collections.abc import Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from pydantic_settings import SettingsConfigDict
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import (
    SessionFactory,
    build_engine,
    build_session_factory,
    initialize_database,
)


class IsolatedSettings(Settings):
    model_config = SettingsConfigDict(env_file=None)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[Any]:
    outcome = yield
    report = outcome.get_result()
    browser_skip = any(word in f"{item.path.name} {report.longrepr}".lower()
                       for word in ("browser", "playwright", "chrome"))
    if report.skipped and os.environ.get("CI_REQUIRE_BROWSER") == "1" and browser_skip:
        report.outcome = "failed"
        report.longrepr = f"CI 必须实际执行浏览器测试，不能跳过：{item.nodeid}\n{report.longrepr}"
    setattr(item, f"report_{report.when}", report)


@pytest.fixture(autouse=True)
def browser_failure_artifacts(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        from playwright.sync_api import Browser
    except ImportError:
        return
    contexts: dict[int, Any] = {}
    messages: list[str] = []
    original_page, original_context, original_close = (
        Browser.new_page, Browser.new_context, Browser.close,
    )

    def observe_page(page: Any) -> None:
        page.on("pageerror", lambda error: messages.append(f"pageerror: {error}"))
        page.on("console", lambda message: messages.append(
            f"console[{message.type}]: {message.text}"
        ))

    def observe_context(context: Any) -> None:
        if id(context) not in contexts:
            contexts[id(context)] = context
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            context.on("page", observe_page)

    def new_page(browser: Any, *args: Any, **kwargs: Any) -> Any:
        page = original_page(browser, *args, **kwargs)
        observe_context(page.context)
        observe_page(page)
        return page

    def new_context(browser: Any, *args: Any, **kwargs: Any) -> Any:
        context = original_context(browser, *args, **kwargs)
        observe_context(context)
        return context

    def close(browser: Any, *args: Any, **kwargs: Any) -> None:
        failed = sys.exc_info()[0] is not None or any(
                     getattr(request.node, f"report_{phase}", None) and
                     getattr(request.node, f"report_{phase}").failed
                     for phase in ("setup", "call"))
        if failed:
            directory = Path("test-results/browser") / re.sub(
                r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid
            )
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "console.txt").write_text("\n".join(messages), encoding="utf-8")
            for index, context in enumerate(contexts.values()):
                try:
                    for page_index, page in enumerate(context.pages):
                        page.screenshot(path=str(directory / f"{index}-{page_index}.png"))
                    context.tracing.stop(path=str(directory / f"trace-{index}.zip"))
                except Exception as exc:
                    (directory / "capture-error.txt").write_text(type(exc).__name__)
        original_close(browser, *args, **kwargs)

    monkeypatch.setattr(Browser, "new_page", new_page)
    monkeypatch.setattr(Browser, "new_context", new_context)
    monkeypatch.setattr(Browser, "close", close)


def pytest_sessionstart(session: pytest.Session) -> None:
    if os.environ.get("CI_REQUIRE_BROWSER") == "1":
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError as exc:
            raise pytest.UsageError("CI 必须安装 Playwright，不能跳过浏览器测试") from exc


def pytest_configure(config: pytest.Config) -> None:
    """Isolate even Settings() calls and collection-time imports from local configuration."""
    patch = pytest.MonkeyPatch()
    patch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        patch.delenv(name.upper(), raising=False)
    directory = TemporaryDirectory(prefix="trade-news-tests-")
    patch.setenv("DATABASE_URL", f"sqlite:///{Path(directory.name) / 'default.db'}")
    patch.setenv("SCHEDULER_ENABLED", "false")
    patch.setenv("DAILY_BARS_ENABLED", "false")
    patch.setenv("AKSHARE_ENABLED", "false")
    config.add_cleanup(directory.cleanup)
    config.add_cleanup(patch.undo)


@pytest.fixture(autouse=True)
def block_unmarked_external_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if request.node.get_closest_marker("network"):
        return
    original = socket.socket.connect

    def connect(sock: socket.socket, address: Any) -> None:
        if sock.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1", "::1", "localhost",
        }:
            raise RuntimeError("外部网络已禁用；真实来源测试须显式标记 network")
        original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return IsolatedSettings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        scheduler_enabled=False,
        daily_bars_enabled=False,
        seed_watchlist="AAPL,MSFT",
        auto_analyze=True,
        llm_api_key=None,
        tushare_token=None,
        tushare_news_enabled=False,
        akshare_enabled=False,
    )


@pytest.fixture
def session_factory(settings: Settings) -> Iterator[SessionFactory]:
    engine = build_engine(settings.database_url)
    initialize_database(engine, settings)
    factory = build_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def session(session_factory: SessionFactory) -> Iterator[Session]:
    with session_factory() as db_session:
        yield db_session
