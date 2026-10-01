from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security

from . import test_decisions_api as api_fixtures

decisions_client = api_fixtures.decisions_client


def sample_payload(security_id: int, market: str = "US") -> dict[str, Any]:
    bars: list[dict[str, Any]] = []
    current = date(2021, 9, 29)
    while current <= date(2026, 9, 28):
        if current.weekday() < 5:
            close = 100 + len(bars) / 10
            opening = close - (1 if len(bars) % 2 else -1)
            bar: dict[str, Any] = {
                "date": current.isoformat(), "open": opening, "high": close + 2,
                "low": close - 2, "close": close,
                "volume": None if len(bars) % 7 == 0 else 100000 + len(bars), "amount": None,
            }
            bar.update({f"adj_{key}": bar[key] / 2 for key in ("open", "high", "low", "close")})
            bars.append(bar)
        current += timedelta(days=1)
    return {
        "security_id": security_id, "market": market,
        "symbol": "AAPL" if market == "US" else "00700", "source": "yahoo",
        "currency": "USD" if market == "US" else "HKD",
        "timezone": "America/New_York" if market == "US" else "Asia/Hong_Kong",
        "volume_unit": "shares",
        "available_adjustments": ["split_adjusted", "total_return_adjusted"],
        "bars": bars, "coverage": {"start": bars[0]["date"], "end": bars[-1]["date"]},
        "latest_trade_date": bars[-1]["date"], "last_success_at": "2026-09-29T00:00:00Z",
        "last_attempt_at": "2026-09-29T00:00:00Z", "next_retry_at": None,
        "sync_status": "success", "error": None, "needs_refresh": False, "stale": False,
    }


@pytest.fixture
def daily_page(
    decisions_client: TestClient, session_factory: SessionFactory, request: pytest.FixtureRequest,
) -> Iterator[tuple[Any, dict[str, Any]]]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    market = getattr(request, "param", "US")
    with session_factory() as session:
        if market == "US":
            security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
            assert security is not None
        else:
            security = Security(
                market="HK", exchange="HKEX", symbol="00700", name="腾讯控股",
                currency="HKD", timezone="Asia/Hong_Kong", calendar="HK",
            )
            session.add(security)
            session.commit()
        security_id = security.id
    if market == "US":
        response = decisions_client.post(f"/api/v1/securities/{security_id}/pe-analysis/refresh")
        assert response.status_code == 200
    state: dict[str, Any] = {
        "payload": sample_payload(security_id, market), "refreshes": [], "reads": 0,
        "polls": 0, "run": {"id": 900, "status": "completed", "errors": []},
        "pending_refresh": [], "mode": "success", "errors": [],
        "stylesheets": [], "legacy_stylesheets": 0,
        "url": f"http://daily.test/securities/{security_id}",
    }

    def route_request(route: Any) -> None:
        req = route.request
        url = urlsplit(req.url)
        if url.hostname != "daily.test":
            route.abort()
            return
        if url.path.endswith("/daily-bars/refresh"):
            state["refreshes"].append(url.query)
            if state["mode"] == "pending":
                state["pending_refresh"].append(route)
            elif state["mode"] == "network":
                route.abort()
            elif state["mode"] == "http_error":
                route.fulfill(status=503, content_type="text/html", body="Unavailable")
            else:
                route.fulfill(json={"run_id": 900, "status": "queued"})
            return
        if url.path.endswith("/daily-bars"):
            state["reads"] += 1
            assert not url.query, "The browser must load all cached bars for local range switches"
            route.fulfill(json=state["payload"])
            return
        if url.path == "/api/v1/runs/900":
            state["polls"] += 1
            route.fulfill(json=state["run_sequence"].pop(0)
                          if state.get("run_sequence") else state["run"])
            return
        response = decisions_client.request(
            req.method, url.path, content=req.post_data, headers={"host": "daily.test"},
        )
        body = response.content
        headers = dict(response.headers)
        if url.path == "/static/style.css":
            state["stylesheets"].append(url.query)
            legacy = state.get("css_mode") == "missing_height" or (
                state.get("css_mode") == "unversioned_stale" and not parse_qs(url.query).get("v")
            )
            if legacy:
                state["legacy_stylesheets"] += 1
                body = re.sub(rb"\.daily-bars-chart\s*\{[^}]*\}", b"", body)
                headers.pop("content-length", None)
        if url.path.endswith("lightweight-charts-5.0.9.standalone.production.js"):
            # Capture actual library objects while preserving their real rendering behavior.
            body += b"""
;(() => {
  const originalCreate = window.LightweightCharts.createChart;
  window.LightweightCharts = {...window.LightweightCharts, createChart(...args) {
    const chart = originalCreate(...args);
    window.dailyTestChart = chart;
    window.dailyTestSeries = [];
    const originalAdd = chart.addSeries.bind(chart);
    chart.addSeries = (...seriesArgs) => {
      const series = originalAdd(...seriesArgs);
      window.dailyTestSeries.push(series);
      return series;
    };
    return chart;
  }};
})();
"""
            headers.pop("content-length", None)
        route.fulfill(status=response.status_code, headers=headers, body=body)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error as exc:
            if "executable" in str(exc).lower() or "not found" in str(exc).lower():
                pytest.skip("Local Chrome is required for daily chart interaction tests")
            raise
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 1200})
            page.route("**/*", route_request)
            page.on("pageerror", lambda error: state["errors"].append(str(error)))
            yield page, state
            assert state["errors"] == []
        finally:
            browser.close()


def test_chart_uses_real_local_library_and_switches_ranges_adjustments_and_ma(
    daily_page: tuple[Any, dict[str, Any]], tmp_path: Path,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    assert page.evaluate("LightweightCharts.version()") == "5.0.9"
    assert page.evaluate("dailyTestChart.panes().length") == 2
    assert page.locator("#daily-bars-chart").evaluate("el => el.clientHeight") == 430
    assert page.locator("#daily-bars-chart").evaluate("el => el.style.minHeight") == ""
    assert page.locator("#daily-bars-chart canvas").count() >= 4
    expect(page.locator('[data-months="12"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#daily-bars-adjustment")).to_have_value("total_return_adjusted")
    assert page.evaluate("dailyTestSeries[0].data().at(-1).close") == \
        state["payload"]["bars"][-1]["adj_close"]
    assert page.evaluate("dailyTestSeries[1].data().some(bar => bar.time === '2021-09-29')") \
        is False
    assert page.evaluate("dailyTestSeries[1].data().some(bar => bar.value === 0)") is False
    assert page.evaluate("dailyTestSeries[1].data()[0].value") == \
        state["payload"]["bars"][1]["volume"]
    default_range = page.evaluate("dailyTestChart.timeScale().getVisibleLogicalRange()")
    page.locator('[data-months="3"]').click()
    page.wait_for_function(
        "previous => dailyTestChart.timeScale().getVisibleLogicalRange().from > previous",
        arg=default_range["from"],
    )
    short_range = page.evaluate("dailyTestChart.timeScale().getVisibleLogicalRange()")
    assert short_range["from"] > default_range["from"]
    for months in (6, 36, 60):
        page.locator(f'[data-months="{months}"]').click()
        expect(page.locator(f'[data-months="{months}"]')).to_have_attribute("aria-pressed", "true")
    page.wait_for_function("dailyTestChart.timeScale().getVisibleLogicalRange().from < 1")
    for index, period in enumerate((5, 10, 20, 60), start=2):
        expected = sum(bar["adj_close"] for bar in state["payload"]["bars"][-period:]) / period
        assert page.evaluate(f"dailyTestSeries[{index}].data().at(-1).value") == \
            pytest.approx(expected)
        page.locator(f'[data-ma="{period}"]').uncheck()
        assert page.evaluate(f"dailyTestSeries[{index}].options().visible") is False
        page.locator(f'[data-ma="{period}"]').check()
    page.locator("#daily-bars-adjustment").select_option("split_adjusted")
    assert page.evaluate("dailyTestSeries[0].data().at(-1).close") == \
        state["payload"]["bars"][-1]["close"]
    expected_ma = sum(bar["close"] for bar in state["payload"]["bars"][-60:]) / 60
    assert page.evaluate("dailyTestSeries[5].data().at(-1).value") == pytest.approx(expected_ma)
    assert state["reads"] == 1 and state["refreshes"] == []
    expect(page.locator("#daily-bars-meta")).to_contain_text("USD")
    expect(page.locator("#daily-bars-meta")).to_contain_text("2026-09-28")

    page.locator('[data-months="3"]').click()
    page.wait_for_function(
        "previous => dailyTestChart.timeScale().getVisibleLogicalRange().from > previous",
        arg=default_range["from"],
    )
    page.locator("#daily-bars").scroll_into_view_if_needed()
    position = page.evaluate("""() => {
      const bar = dailyTestSeries[0].data().at(-5);
      const rect = document.getElementById('daily-bars-chart').getBoundingClientRect();
      return {x: rect.x + dailyTestChart.timeScale().timeToCoordinate(bar.time),
              y: rect.y + dailyTestSeries[0].priceToCoordinate(bar.close)};
    }""")
    page.mouse.move(position["x"], position["y"])
    expect(page.locator("#daily-bars-quote")).to_contain_text(
        state["payload"]["bars"][-5]["date"]
    )
    page.locator("#daily-bars").screenshot(path=str(tmp_path / "daily-bars-desktop.png"))


@pytest.mark.parametrize("daily_page", ["HK"], indirect=True)
def test_non_watchlisted_hk_chart_on_narrow_screen_and_missing_adjustment(
    daily_page: tuple[Any, dict[str, Any]], tmp_path: Path,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["payload"]["available_adjustments"] = ["split_adjusted"]
    for bar in state["payload"]["bars"]:
        for name in ("adj_open", "adj_high", "adj_low", "adj_close"):
            bar[name] = None
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    expect(page.locator("#pe-analysis")).to_contain_text("请先在")
    expect(page.locator("#daily-bars-adjustment")).to_have_value("split_adjusted")
    expect(page.locator('[value="total_return_adjusted"]')).to_have_attribute("disabled", "")
    expect(page.locator("#daily-bars-adjustment-note")).to_be_visible()
    expect(page.locator("#daily-bars-meta")).to_contain_text("HKD")
    expect(page.locator("#daily-bars-meta")).to_contain_text("Asia/Hong_Kong")
    assert page.evaluate("dailyTestChart.panes().length") == 2
    assert page.locator("#daily-bars-chart").evaluate("el => el.clientHeight") == 360
    assert page.locator("#daily-bars-chart").evaluate("el => el.style.minHeight") == ""
    panel = page.locator("#daily-bars").bounding_box()
    assert panel and panel["x"] >= 0 and panel["x"] + panel["width"] <= 390
    assert page.locator("#daily-bars").evaluate("el => el.scrollWidth <= el.clientWidth")
    page.locator('[data-months="6"]').click()
    expect(page.locator('[data-months="6"]')).to_have_attribute("aria-pressed", "true")
    page.set_viewport_size({"width": 390, "height": 1500})
    page.evaluate("window.scrollTo(0, 0)")
    page.locator("#daily-bars").screenshot(path=str(tmp_path / "daily-bars-mobile.png"))


def test_empty_cache_refreshes_once_then_renders_default_adjustment(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    complete = state["payload"]
    state["payload"] = {
        **complete, "bars": [], "needs_refresh": True, "sync_status": "pending",
        "coverage": {}, "latest_trade_date": None, "last_attempt_at": None,
    }
    state["mode"] = "pending"
    state["run_sequence"] = [
        {"status": "queued"}, {"status": "running"}, {"status": "completed"},
    ]
    page.goto(state["url"])
    expect(page.locator("#daily-bars-empty")).to_be_visible()
    expect(page.locator("#daily-bars-refresh")).to_be_disabled()
    assert state["refreshes"] == [""]
    page.evaluate("document.getElementById('daily-bars-refresh').click()")
    assert len(state["refreshes"]) == 1
    state["payload"] = complete
    state["pending_refresh"].pop().fulfill(json={"run_id": 900, "status": "queued"})
    expect(page.locator("#daily-bars-status")).to_have_attribute(
        "data-state", "ready", timeout=10000,
    )
    expect(page.locator("#daily-bars-empty")).not_to_be_visible()
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    expect(page.locator("#daily-bars-adjustment")).to_have_value("total_return_adjusted")
    assert state["reads"] == 2 and state["refreshes"] == [""] and state["polls"] == 1


def test_failed_run_and_http_network_failures_keep_old_chart_without_auto_retry(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["payload"].update(
        needs_refresh=True, stale=True, sync_status="failed", error="Yahoo rate limit",
    )
    state["run"] = {"id": 900, "status": "failed", "errors": ["Yahoo rate limit"]}
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_contain_text("Yahoo rate limit")
    expect(page.locator("#daily-bars-status")).to_contain_text("仍显示已保存行情")
    original = page.evaluate("dailyTestSeries[0].data()")
    assert state["refreshes"] == [""] and state["polls"] == 1
    for mode, message in (("http_error", "HTTP 503"), ("network", "刷新失败")):
        state["mode"] = mode
        page.locator("#daily-bars-refresh").click()
        expect(page.locator("#daily-bars-status")).to_contain_text(message)
        expect(page.locator("#daily-bars-refresh")).to_be_enabled()
        assert page.evaluate("dailyTestSeries[0].data()") == original
        expect(page.locator("#daily-bars-meta")).to_contain_text("2026-09-28")
    assert state["refreshes"] == ["", "force=true", "force=true"]
    assert state["reads"] == 4


def test_empty_completed_response_stays_empty_without_refresh_loop(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["payload"].update(bars=[], coverage={}, latest_trade_date=None)
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "empty")
    expect(page.locator("#daily-bars-empty")).to_be_visible()
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    assert state["reads"] == 2 and state["refreshes"] == [""]
    assert page.locator("#daily-bars-chart canvas").count() == 0


@pytest.mark.parametrize("sync_status,label", [("failed", "采集失败"), ("partial", "行情待补")])
def test_retry_cooldown_keeps_cached_chart_and_watchlist_has_entry(
    daily_page: tuple[Any, dict[str, Any]], decisions_client: TestClient,
    sync_status: str, label: str,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["payload"].update(
        needs_refresh=True, stale=True, sync_status=sync_status, error="Yahoo rate limit",
        next_retry_at="2099-01-01T00:00:00Z",
    )
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_contain_text("下次重试")
    expect(page.locator("#daily-bars-status")).to_contain_text(label)
    expect(page.locator("#daily-bars-chart")).to_be_visible()
    assert state["refreshes"] == []
    watchlist = decisions_client.get("/watchlist")
    security_id = state["payload"]["security_id"]
    assert f'href="/securities/{security_id}#daily-bars">行情/K线</a>' in watchlist.text


def test_empty_failed_refresh_preserves_existing_adjusted_prices_and_metadata(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    original = page.evaluate("dailyTestSeries[0].data()")
    state["payload"] = {
        **state["payload"], "bars": [], "available_adjustments": [], "coverage": {},
        "latest_trade_date": None, "last_success_at": None, "sync_status": "failed",
        "error": "Yahoo unavailable",
    }
    state["run"] = {"id": 900, "status": "failed", "errors": ["Yahoo unavailable"]}
    page.locator("#daily-bars-refresh").click()
    expect(page.locator("#daily-bars-status")).to_contain_text("Yahoo unavailable")
    expect(page.locator("#daily-bars-adjustment")).to_have_value("total_return_adjusted")
    expect(page.locator("#daily-bars-adjustment-note")).not_to_be_visible()
    expect(page.locator("#daily-bars-meta")).to_contain_text("2026-09-28")
    expect(page.locator("#daily-bars-meta")).to_contain_text("2026/9/29")
    assert page.evaluate("dailyTestSeries[0].data()") == original
    assert state["refreshes"] == ["force=true"]


@pytest.mark.parametrize("batch_status", ["running", "failed"])
def test_stock_completion_renders_before_batch_end_and_ignores_other_stock_failure(
    daily_page: tuple[Any, dict[str, Any]], batch_status: str,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    complete = state["payload"]
    state["payload"] = {
        **complete, "bars": [], "sync_status": "pending", "last_attempt_at": None,
        "latest_trade_date": None, "needs_refresh": True,
    }
    state["mode"] = "pending"
    state["run"] = {"status": batch_status, "errors": ["Other stock unavailable"]}
    page.clock.install()
    page.goto(state["url"])
    expect(page.locator("#daily-bars-refresh")).to_be_disabled()
    expect(page.locator("#daily-bars-empty")).to_be_visible()
    state["payload"] = complete
    state["pending_refresh"].pop().fulfill(json={"run_id": 900, "status": "queued"})
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    expect(page.locator("#daily-bars-chart")).to_be_visible()
    expect(page.locator("#daily-bars-status")).not_to_contain_text("Other stock")
    assert page.evaluate("dailyTestSeries[0].data().at(-1).close") == \
        complete["bars"][-1]["adj_close"]
    page.clock.fast_forward(10000)
    assert state["polls"] == 1 and state["refreshes"] == [""]


def test_force_refresh_waits_for_new_attempt_instead_of_reusing_old_success(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    page.clock.install()
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    # Another update happened after the page loaded, before the user's force refresh.
    state["payload"] = {
        **state["payload"], "last_attempt_at": "2026-09-29T00:01:00Z",
        "last_success_at": "2026-09-29T00:01:00Z",
    }
    state["run"] = {"status": "queued", "errors": []}
    page.locator("#daily-bars-refresh").click()
    expect(page.locator("#daily-bars-status")).to_contain_text("行情任务排队中")
    expect(page.locator("#daily-bars-refresh")).to_be_disabled()
    assert state["polls"] == 1 and state["reads"] == 3
    page.evaluate("dailyTestChart.timeScale().setVisibleLogicalRange({from: 1200, to: 1250})")
    page.wait_for_function("dailyTestChart.timeScale().getVisibleLogicalRange().from === 1200")
    state["run"] = {"status": "running", "errors": []}
    page.clock.fast_forward(2000)
    expect(page.locator("#daily-bars-status")).to_contain_text("等待本股票行情更新")
    expect(page.locator("#daily-bars-refresh")).to_be_disabled()
    assert page.evaluate("dailyTestChart.timeScale().getVisibleLogicalRange().from") == 1200
    state["payload"] = {
        **state["payload"], "last_attempt_at": "2026-09-29T00:02:00Z",
        "last_success_at": "2026-09-29T00:02:00Z",
    }
    page.clock.fast_forward(2000)
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    expect(page.locator("#daily-bars-meta")).to_contain_text("08:02:00")
    assert state["polls"] == 3 and state["refreshes"] == ["force=true"]


def test_existing_running_stock_completion_does_not_need_new_attempt_timestamp(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    complete = state["payload"]
    state["payload"] = {**complete, "sync_status": "running", "needs_refresh": True}
    state["mode"] = "pending"
    state["run"] = {"status": "running", "errors": []}
    page.goto(state["url"])
    expect(page.locator("#daily-bars-refresh")).to_be_disabled()
    state["payload"] = complete
    state["pending_refresh"].pop().fulfill(json={"run_id": 900, "status": "queued"})
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    assert state["polls"] == 1


def test_failed_forced_run_cannot_relabel_an_old_success_as_this_attempt_completed(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    original = page.evaluate("dailyTestSeries[0].data()")
    state["run"] = {"status": "failed", "errors": ["Task worker stopped"]}
    page.locator("#daily-bars-refresh").click()
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "error")
    expect(page.locator("#daily-bars-status")).to_contain_text("本次刷新未确认完成")
    expect(page.locator("#daily-bars-status")).to_contain_text("仍显示已保存行情")
    assert page.evaluate("dailyTestSeries[0].data()") == original
    assert state["refreshes"] == ["force=true"] and state["polls"] == 1


@pytest.mark.parametrize("finished", [False, True])
def test_wait_deadline_reads_cache_once_more_and_stops_showing_active_polling(
    daily_page: tuple[Any, dict[str, Any]], finished: bool,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    complete = state["payload"]
    state["payload"] = {
        **complete, "bars": [], "sync_status": "pending", "last_attempt_at": None,
        "latest_trade_date": None, "needs_refresh": True,
    }
    state["run"] = {"status": "running", "errors": []}
    page.clock.install()
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_contain_text("等待本股票行情更新")
    assert state["reads"] == 2 and state["polls"] == 1
    if finished:
        state["payload"] = complete
    page.clock.fast_forward(6 * 60 * 1000)
    expect(page.locator("#daily-bars-refresh")).to_be_enabled()
    if finished:
        expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
        expect(page.locator("#daily-bars-chart")).to_be_visible()
    else:
        expect(page.locator("#daily-bars-status")).to_contain_text("已停止等待")
        expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "stale")
    page.clock.fast_forward(60000)
    assert state["reads"] == 3 and state["polls"] == 1


def test_versioned_assets_bypass_old_unversioned_stylesheet(
    daily_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["css_mode"] = "unversioned_stale"
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    assert len(state["stylesheets"]) == 1
    assert parse_qs(state["stylesheets"][0]).get("v")
    assert state["legacy_stylesheets"] == 0
    script = page.locator('script[src*="/static/daily-bars.js"]').get_attribute("src")
    assert script is not None and parse_qs(urlsplit(script).query).get("v")
    host = page.locator("#daily-bars-chart")
    assert host.evaluate("el => el.clientHeight") == 430
    assert host.evaluate("el => el.style.minHeight") == ""
    old_css = page.evaluate("fetch('/static/style.css').then(response => response.text())")
    assert ".daily-bars-chart {" not in old_css
    assert state["legacy_stylesheets"] == 1
    expect(host.locator("canvas").first).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390])
def test_missing_chart_height_rules_still_render_visible_real_canvas(
    daily_page: tuple[Any, dict[str, Any]], width: int,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    state["css_mode"] = "missing_height"
    page.set_viewport_size({"width": width, "height": 1200})
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    host = page.locator("#daily-bars-chart")
    expect(host).to_be_visible()
    assert host.evaluate("el => el.clientHeight") == 360
    assert host.evaluate("el => el.style.minHeight") == "360px"
    expect(host.locator("canvas").first).to_be_visible()
    assert host.locator("canvas").first.evaluate("el => el.width > 0 && el.height > 0")
    assert page.evaluate("dailyTestChart.panes().length") == 2
    assert page.evaluate("dailyTestSeries[0].data().length") == len(state["payload"]["bars"])
    assert state["legacy_stylesheets"] == 1
