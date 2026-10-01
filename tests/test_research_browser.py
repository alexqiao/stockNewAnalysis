from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from trade_news_analysis.db import SessionFactory
from trade_news_analysis.services.research_workflow import list_claims, sync_x_claims

from . import test_research_api as api_fixtures
from .test_research_workflow import saved_post

decisions_client = api_fixtures.decisions_client
decision_security = api_fixtures.decision_security


@pytest.fixture
def research_browser(
    decisions_client: TestClient, decision_security: tuple[int, int],
    session_factory: SessionFactory,
) -> Iterator[tuple[Any, dict[str, Any]]]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    now = datetime.now(UTC)
    with session_factory() as session:
        saved_post(session, published_at=now - timedelta(hours=1))
        sync_x_claims(session, now)
        claim = list_claims(session, now=now)[0]
        session.commit()
    state: dict[str, Any] = {
        "mode": "pending", "pending": [], "saves": 0, "errors": [], "claim": claim,
        "security_id": decision_security[0], "refresh_mode": "success", "refreshes": [],
        "run_pending": [], "run_polls": 0, "run_mode": "pending", "history_reads": 0,
    }

    def route_request(route: Any) -> None:
        request = route.request
        url = urlsplit(request.url)
        if url.hostname != "research.test":
            route.abort()
            return
        if url.path.endswith("/daily-bars"):
            route.fulfill(json={"bars": [], "available_adjustments": [], "sync_status": "failed",
                                "next_retry_at": (now + timedelta(days=1)).isoformat()})
            return
        if url.path == "/api/v1/runs/777":
            state["run_polls"] += 1
            if state["run_mode"] == "pending":
                state["run_pending"].append(route)
            else:
                route.fulfill(json={"id": 777, "status": "completed", "is_terminal": True})
            return
        if url.path.endswith("/pe-analysis/refresh"):
            if state["refresh_mode"] == "pending":
                state["refreshes"].append(route)
            else:
                route.fulfill(json={})
            return
        if request.method in {"PUT", "PATCH"}:
            state["saves"] += 1
            if state["mode"] == "pending":
                state["pending"].append(route)
                return
            if state["mode"] == "network":
                route.abort()
                return
        if url.path == f"/api/v1/research/claims/{claim['id']}" and request.method == "GET":
            state["history_reads"] += 1
        headers = {"Content-Type": request.headers["content-type"]} \
            if "content-type" in request.headers else {}
        response = decisions_client.request(
            request.method, url.path + ("?" + url.query if url.query else ""),
            content=request.post_data, headers=headers,
        )
        route.fulfill(status=response.status_code, headers=dict(response.headers),
                      body=response.content)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
        except playwright_api.Error as exc:
            missing = "executable" in str(exc).lower() or "not found" in str(exc).lower()
            if missing and not os.getenv("CI"):
                pytest.skip("Local Chrome is required for research interaction tests")
            raise
        try:
            page = browser.new_page()
            page.route("**/*", route_request)
            page.on("pageerror", lambda error: state["errors"].append(str(error)))
            yield page, state
            assert state["errors"] == []
        finally:
            browser.close()


def test_research_form_freezes_recovers_and_keeps_other_drafts(
    research_browser: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = research_browser
    page.goto("http://research.test/research")
    form = page.locator('.research-form[data-endpoint="/api/v1/research/portfolio"]')
    form.locator('[name="total_value"]').fill("123456")
    other = page.locator('[name="risk_budget_pct"]')
    other.fill("0.031")
    form.locator("button").click()
    expect(form).to_have_attribute("inert", "")
    page.evaluate("""document.querySelector('[data-endpoint="/api/v1/research/portfolio"]')
      .dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))""")
    assert state["saves"] == 1
    state["pending"].pop().fulfill(status=500, content_type="text/html", body="Unavailable")
    expect(form.locator('[role="status"]')).to_contain_text("HTTP 500")
    expect(form).not_to_have_attribute("inert", "")
    expect(form.locator('[name="total_value"]')).to_have_value("123456")
    state["mode"] = "network"
    form.locator("button").click()
    expect(form.locator('[role="status"]')).to_contain_text("Failed to fetch")
    state["mode"] = "success"
    form.locator('[name="currency"]').fill("USD")
    form.locator("button").click()
    expect(form.locator('[role="status"]')).to_contain_text("已保存并更新")
    expect(other).to_have_value("0.031")
    assert state["saves"] == 3


def test_claim_conflict_keeps_draft_and_requires_explicit_revision_acknowledgement(
    research_browser: tuple[Any, dict[str, Any]], decisions_client: TestClient,
) -> None:
    from playwright.sync_api import expect

    page, state = research_browser
    page.goto("http://research.test/research")
    item = page.locator(f'#claim-{state["claim"]["id"]}')
    item.locator('[data-claim-editor] summary').click()
    form = item.locator('form[data-workflow-form="claim"]')
    form.locator('[name="note"]').fill("本页面的未提交核验草稿")
    external = decisions_client.patch(f'/api/v1/research/claims/{state["claim"]["id"]}', json={
        "expected_revision": state["claim"]["revision"], "status": "pending",
        "note": "另一页面的最新核验",
    })
    assert external.status_code == 200
    state["mode"] = "success"
    form.locator('button[type="submit"]').click()
    expect(form.locator('[data-conflict]')).to_contain_text("另一页面的最新核验")
    expect(form.locator('[name="note"]')).to_have_value("本页面的未提交核验草稿")
    expect(form).not_to_have_attribute("inert", "")
    form.locator('[data-conflict] button').click()
    form.locator('button[type="submit"]').click()
    expect(form.locator('[data-workflow-message]')).to_contain_text("已保存核验记录")
    latest = decisions_client.get(f'/api/v1/research/claims/{state["claim"]["id"]}').json()
    assert latest["note"] == "本页面的未提交核验草稿"
    assert state["history_reads"] == 0
    item.locator('.workflow-history summary').click()
    expect(item.locator('.workflow-history')).to_have_attribute('data-loaded', 'true')
    assert state["history_reads"] == 1


def test_pe_automatic_refresh_keeps_draft_and_save_recovers_after_errors(
    research_browser: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = research_browser
    state["refresh_mode"] = "pending"
    page.goto(f'http://research.test/securities/{state["security_id"]}')
    root = page.locator('#pe-analysis')
    # The initial provider refresh intentionally remains pending while the user edits.
    root.locator('details summary').click()
    root.locator('[data-override="price"]').fill("123.45")
    assert len(state["refreshes"]) == 1
    state["refreshes"].pop().fulfill(json={})
    expect(root.locator('#pe-message')).to_contain_text("草稿已保留")
    expect(root.locator('[data-override="price"]')).to_have_value("123.45")
    root.locator('#save-pe').click()
    expect(root).to_have_attribute('inert', '')
    page.evaluate("document.getElementById('save-pe').click()")
    assert state["saves"] == 1
    state["pending"].pop().fulfill(status=500, content_type="text/html", body="Unavailable")
    expect(root.locator('#pe-message')).to_contain_text('HTTP 500')
    expect(root.locator('[data-override="price"]')).to_have_value('123.45')
    state["mode"] = 'network'
    root.locator('#save-pe').click()
    expect(root.locator('#pe-message')).to_contain_text('操作失败')
    state["mode"] = 'success'
    root.locator('#save-pe').click()
    expect(root.locator('#pe-message')).to_contain_text('已保存并重算')
    assert state["saves"] == 3


def test_run_poller_is_serial_and_restores_after_reload(
    research_browser: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = research_browser
    page.goto('http://research.test/research')
    page.evaluate("RunPoller.start({runId:777, storageKey:'research-active-run', intervalMs:10})")
    page.wait_for_function("sessionStorage.getItem('research-active-run') === '777'")
    page.wait_for_timeout(80)
    assert state['run_polls'] == 1
    state['run_mode'] = 'complete'
    page.reload()
    expect(page.locator('#research-message')).to_contain_text('刷新完成')
    assert state['run_polls'] == 2
    assert page.evaluate("sessionStorage.getItem('research-active-run')") is None
    expect(page.locator('#refresh-research')).to_be_enabled()


def test_claim_pending_save_blocks_duplicate_and_recovers_with_draft(
    research_browser: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = research_browser
    page.goto("http://research.test/research")
    item = page.locator(f'#claim-{state["claim"]["id"]}')
    item.locator('[data-claim-editor] summary').click()
    form = item.locator('form[data-workflow-form="claim"]')
    form.locator('[name="note"]').fill("延迟期间保留草稿")
    form.locator('button[type="submit"]').click()
    expect(form).to_have_attribute('inert', '')
    form.evaluate(
        "form => form.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))",
    )
    assert state['saves'] == 1
    state['pending'].pop().fulfill(status=500, content_type='text/html', body='Unavailable')
    expect(form.locator('[data-workflow-message]')).to_contain_text('HTTP 500')
    expect(form.locator('[name="note"]')).to_have_value('延迟期间保留草稿')
    expect(form).not_to_have_attribute('inert', '')
    state['mode'] = 'network'
    form.locator('button[type="submit"]').click()
    expect(form.locator('[data-workflow-message]')).to_contain_text('Failed to fetch')
    state['mode'] = 'success'
    form.locator('button[type="submit"]').click()
    expect(form.locator('[data-workflow-message]')).to_contain_text('已保存核验记录')


def test_request_timeout_is_visible_and_does_not_erase_recovery_id(
    research_browser: tuple[Any, dict[str, Any]],
) -> None:
    page, _state = research_browser
    page.goto('http://research.test/research')
    result = page.evaluate("""async () => {
      sessionStorage.setItem('pending-run', '777');
      try { await RunPoller.request('/api/v1/runs/777', {}, 30); }
      catch (error) { return error.message; }
    }""")
    assert '请求超时' in result
    assert page.evaluate("RunPoller.restore('pending-run')") == 777
