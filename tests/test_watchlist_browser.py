from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from . import test_decisions_api as api_fixtures

decisions_client = api_fixtures.decisions_client


@pytest.fixture
def watchlist_page(decisions_client: TestClient) -> Iterator[tuple[Any, dict[str, Any]]]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    state: dict[str, Any] = {"mode": "pending", "pending": [], "saves": 0}

    def route_request(route: Any) -> None:
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != "watchlist.test":
            route.abort()
            return
        if url.path == "/api/v1/holdings/ibkr/accounts":
            route.fulfill(json={"accounts": [{"account_key": "test-key", "label": "Test account"}]})
            return
        if request.method == "PUT" and url.path == "/api/v1/watchlist":
            state["saves"] += 1
            if state["mode"] == "pending":
                state["pending"].append(route)
                return
            if state["mode"] == "network":
                route.abort()
                return
        headers = {"Content-Type": request.headers["content-type"]} \
            if "content-type" in request.headers else {}
        response = decisions_client.request(
            request.method, url.path, content=request.post_data, headers=headers,
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers),
                      body=response.content)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error as exc:
            if "executable" in str(exc).lower() or "not found" in str(exc).lower():
                pytest.skip("Local Chrome is required for watchlist interaction tests")
            raise
        try:
            page = browser.new_page()
            page.route("**/*", route_request)
            page.goto("http://watchlist.test/watchlist")
            yield page, state
        finally:
            browser.close()


def test_pending_save_blocks_edits_and_recovers_after_http_and_network_errors(
    watchlist_page: tuple[Any, dict[str, Any]], decisions_client: TestClient,
) -> None:
    from playwright.sync_api import expect

    page, state = watchlist_page
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.locator("#ibkr-connect").click()
    expect(page.locator("#ibkr-sync")).to_be_enabled()
    checkbox = page.locator('[data-field="active"]').first
    checkbox.uncheck()
    page.locator("#save").click()
    expect(page.locator("#message")).to_have_text("正在保存…")
    expect(page.locator("#save")).to_be_disabled()
    expect(page.locator("#add")).to_be_disabled()
    expect(page.locator("#ibkr-connect")).to_be_disabled()
    expect(page.locator("#ibkr-sync")).to_be_disabled()
    expect(page.locator("#watchlist-table")).to_have_attribute("inert", "")
    query = page.locator('[data-field="query"]').first
    original = query.input_value()
    query.click(force=True)
    page.keyboard.insert_text("UNSAVED")
    expect(query).to_have_value(original)
    page.evaluate("document.getElementById('save').onclick()")
    assert state["saves"] == 1

    state["pending"].pop().fulfill(status=500, content_type="text/html", body="Unavailable")
    expect(page.locator("#message")).to_contain_text("HTTP 500")
    for selector in ("#save", "#add", "#ibkr-connect", "#ibkr-sync"):
        expect(page.locator(selector)).to_be_enabled()
    expect(page.locator("#watchlist-table")).not_to_have_attribute("inert", "")
    expect(checkbox).not_to_be_checked()

    state["mode"] = "network"
    page.locator("#save").click()
    expect(page.locator("#message")).to_contain_text("保存失败")
    expect(page.locator("#save")).to_be_enabled()
    expect(page.locator("#ibkr-sync")).to_be_enabled()
    expect(checkbox).not_to_be_checked()
    page.locator("#ibkr-sync").click()
    expect(page.locator("#ibkr-message")).to_contain_text("自选列表有未保存的修改")

    state["mode"] = "success"
    page.locator("#save").click()
    expect(page.locator("#message")).not_to_be_visible()
    expect(checkbox).not_to_be_checked()
    assert decisions_client.get("/api/v1/watchlist").json()[0]["active"] is False
    assert state["saves"] == 3
    assert errors == []
