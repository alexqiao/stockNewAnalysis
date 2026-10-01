from __future__ import annotations

from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from trade_news_analysis.services.bollinger_reference import BollingerParameters

from . import test_daily_bars_browser as chart_fixtures

decisions_client = chart_fixtures.decisions_client
daily_page = chart_fixtures.daily_page


def reference_payload() -> dict[str, Any]:
    return {
        "status": "ready",
        "price_basis": "total_return_adjusted",
        "as_of": "2026-09-28",
        "action": {
            "code": "entry_reference",
            "label": "首次回踩已确认",
            "reason": "已满足样本条件",
        },
        "setup": {
            "state": "pullback_confirmed",
            "label": "首次回踩已确认",
            "breakout_date": "2026-09-20",
            "first_pullback_date": "2026-09-28",
        },
        "checks": [
            {"key": "squeeze", "label": "缩口", "passed": True, "detail": "达到收口分位"},
            {"key": "volume", "label": "缩量", "passed": False, "detail": "等待成交量条件"},
            {"key": "holding", "label": "持仓核对", "passed": None, "detail": "待核对"},
        ],
        "levels": {
            "upper": 110,
            "middle": 100,
            "lower": 90,
            "entry_reference": 100,
            "initial_stop": 96,
            "initial_risk_pct": 4,
            "trailing_stop": 98,
            "current_stop": 97,
            "supports": [{"price": 95, "date": "2026-09-21", "confirmed_at": "2026-09-23"}],
        },
        "position": {"status": "long", "reliable": True, "profit_pct": 12.4},
        "add_on": {
            "enabled": False,
            "conditions": ["回踩与风险条件重新核实"],
            "tranches": [20, 20, 20, 40],
        },
        "parameters": BollingerParameters().model_dump(),
        "definitions": ["布林20日收盘均线与2倍总体标准差", "ATR14与左右各2日支撑确认"],
        "warnings": [],
        "events": [],
        "holding_source": "固定持仓样本",
        "holdings_source": {
            "using_cached": True,
            "note": "持仓沿用 2026-09-20 08:00 UTC 成功快照；仓位、资金按该快照估算。",
        },
        "data": {
            "currency": "USD",
            "source": "yfinance",
            "latest_trade_date": "2026-09-28",
            "stale": False,
            "sync_status": "success",
            "last_success_at": "2026-09-29T00:00:00Z",
        },
        "guardrails": {"blocked": False, "reasons": []},
    }


@pytest.fixture
def reference_page(daily_page: tuple[Any, dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    page, state = daily_page
    state.update(reference=reference_payload(), reference_queries=[], reference_mode="ready")

    def reference_route(route: Any) -> None:
        assert route.request.method == "GET"
        query = parse_qs(urlsplit(route.request.url).query)
        state["reference_queries"].append(query)
        if state["reference_mode"] == "error":
            route.fulfill(status=503, content_type="text/html", body="Unavailable")
            return
        data = deepcopy(state["reference"])
        data["price_basis"] = query["adjustment"][0]
        route.fulfill(json=data)

    page.route("**/api/v1/securities/*/bollinger-reference?*", reference_route)
    return page, state


def test_default_parameters_pass_native_validation_and_submit_expected_values(
    reference_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = reference_page
    page.goto(state["url"], wait_until="networkidle")
    expect(page.locator("#bollinger-reference-status")).to_have_attribute("data-state", "ready")
    inputs = page.locator('#bollinger-reference-form input[type="number"]').evaluate_all(
        "elements => Object.fromEntries(elements.map(el => [el.name, el.valueAsNumber]))"
    )
    fixed = {
        "bb_period",
        "bb_std",
        "atr_period",
        "pivot_left",
        "pivot_right",
        "volume_lookback",
        "contraction_bars",
    }
    defaults = {
        key: value for key, value in BollingerParameters().model_dump().items() if key not in fixed
    }
    assert inputs == defaults
    schema = BollingerParameters.model_json_schema()["properties"]
    for key in ("squeeze_lookback", "squeeze_percentile", "touch_tolerance_pct"):
        field = page.locator(f'#bollinger-reference-form input[name="{key}"]')
        assert float(field.get_attribute("min")) == schema[key]["minimum"]
        assert float(field.get_attribute("max")) == schema[key]["maximum"]
        assert field.get_attribute("step") == ("1" if schema[key]["type"] == "integer" else "any")
    assert page.locator("#bollinger-reference-form").evaluate("el => el.checkValidity()")
    with page.expect_response("**/bollinger-reference?*"):
        page.locator("#bollinger-reference-recalculate").click()
    expect(page.locator("#bollinger-reference-status")).to_have_attribute("data-state", "ready")
    query = state["reference_queries"][-1]
    assert {key: float(query[key][0]) for key in defaults} == defaults
    expect(page.locator('[data-level="initial_risk_pct"]')).to_have_text("4.00%")
    expect(page.locator("#bollinger-reference-position")).to_contain_text("12.40%")
    expect(page.locator("#bollinger-reference-holdings-note")).to_contain_text("2026-09-20")
    expect(page.locator("#bollinger-reference-holdings-note")).to_be_visible()
    expect(page.locator("#bollinger-reference-tranches")).to_contain_text("不表示已经执行")
    expect(page.locator("#bollinger-reference")).to_contain_text("加仓未启用")
    assert state["reads"] == 1 and state["refreshes"] == []


def test_reference_follows_price_basis_not_range_and_api_error_keeps_real_chart(
    reference_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = reference_page
    page.goto(state["url"], wait_until="networkidle")
    expect(page.locator("#bollinger-reference-status")).to_have_attribute("data-state", "ready")
    with page.expect_response("**/bollinger-reference?*"):
        page.locator("#daily-bars-adjustment").select_option("split_adjusted")
    expect(page.locator("#bollinger-reference-meta")).to_contain_text("拆股调整价格")
    assert state["reference_queries"][-1]["adjustment"] == ["split_adjusted"]
    requests = len(state["reference_queries"])
    page.locator('[data-months="3"]').click()
    page.evaluate(
        "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    assert len(state["reference_queries"]) == requests
    original = page.evaluate("dailyTestSeries[0].data()")
    state["reference_mode"] = "error"
    with page.expect_response("**/bollinger-reference?*"):
        page.locator("#bollinger-reference-recalculate").click()
    expect(page.locator("#bollinger-reference-status")).to_have_attribute("data-state", "error")
    expect(page.locator("#bollinger-reference-status")).to_contain_text("503")
    expect(page.locator("#daily-bars-chart")).to_be_visible()
    assert page.evaluate("dailyTestSeries[0].data()") == original
    assert state["reads"] == 1 and state["refreshes"] == []


def test_narrow_reference_shows_guardrails_technical_result_and_all_parameter_inputs(
    reference_page: tuple[Any, dict[str, Any]],
) -> None:
    from playwright.sync_api import expect

    page, state = reference_page
    data = state["reference"]
    data["technical_action"] = dict(data["action"])
    data["action"] = {"code": "blocked", "label": "参考暂停", "reason": "持仓快照时间异常"}
    data["guardrails"] = {"blocked": True, "reasons": ["持仓快照时间异常"]}
    data["data"]["stale"] = True
    data["position"]["reliable"] = False
    data["levels"]["supports"] = [
        {"price": 95 - index, "date": "2026-09-21", "confirmed_at": "2026-09-23"}
        for index in range(30)
    ]
    page.set_viewport_size({"width": 390, "height": 1200})
    page.goto(state["url"], wait_until="networkidle")
    expect(page.locator("#bollinger-reference-label")).to_have_text("参考暂停")
    expect(page.locator("#bollinger-reference-guardrails")).to_contain_text("持仓快照时间异常")
    expect(page.locator("#bollinger-reference-meta")).to_contain_text("已过期")
    expect(page.locator("#bollinger-reference-technical")).to_contain_text("首次回踩已确认")
    expect(page.locator("#bollinger-reference-position")).to_contain_text("输入状态：待核对")
    expect(page.locator("#bollinger-reference-position")).not_to_contain_text("12.40%")
    supports = page.locator("#bollinger-reference-supports")
    summary = page.locator("#bollinger-reference-supports-summary")
    expect(summary).to_have_text("已确认的支撑点（共 30 个）")
    expect(supports).to_be_hidden()
    summary.click()
    expect(supports).to_be_visible()
    expect(supports.locator("li")).to_have_count(30)
    expect(supports.locator("li").last).to_have_text(
        "66.00 · 低点日期 2026-09-21 · 确认日期 2026-09-23"
    )
    summary.click()
    expect(supports).to_be_hidden()
    page.locator("#bollinger-reference-parameters > summary").click()
    page.locator(".bollinger-reference-advanced > summary").click()
    inputs = page.locator('#bollinger-reference-form input[type="number"]')
    assert inputs.count() == 18
    for input_box in inputs.all():
        expect(input_box).to_be_visible()
    assert page.locator("#bollinger-reference").evaluate("el => el.scrollWidth <= el.clientWidth")
    expect(page.locator("#daily-bars-chart")).to_be_visible()
