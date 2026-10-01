from __future__ import annotations

import statistics
from typing import Any

import pytest

from . import test_daily_bars_browser as chart_fixtures

daily_page = chart_fixtures.daily_page
decisions_client = chart_fixtures.decisions_client


def set_prices(state: dict[str, Any], count: int, *, constant: bool = False) -> None:
    bars = state["payload"]["bars"][:count]
    for index, bar in enumerate(bars):
        close = 84 if constant else 50 + (index * 7) % 13 + (index % 3) ** 2
        factor = 0.5 if constant else 0.65 + (index % 4) * 0.05
        for key, value in {"open": close, "high": close + 1,
                           "low": close - 1, "close": close}.items():
            bar[key] = value
            bar[f"adj_{key}"] = value * factor
    state["payload"] = {
        **state["payload"], "bars": bars,
        "latest_trade_date": bars[-1]["date"],
        "coverage": {"start": bars[0]["date"], "end": bars[-1]["date"]},
    }


def expected_bands(bars: list[dict[str, Any]], field: str) -> list[float]:
    closes = [bar[field] for bar in bars[-20:]]
    middle = statistics.fmean(closes)
    offset = 2 * statistics.pstdev(closes)
    return [middle + offset, middle, middle - offset]


@pytest.mark.parametrize("width", [1280, 390])
def test_bollinger_prices_hover_toggle_and_adjustment_are_local(
    daily_page: tuple[Any, dict[str, Any]], width: int,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    set_prices(state, 25)
    bars = state["payload"]["bars"]
    page.set_viewport_size({"width": width, "height": 1200})
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    toggle = page.locator("#daily-bars-bollinger")
    expect(toggle).to_be_checked()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    band_data = page.evaluate("dailyTestSeries.slice(6, 9).map(series => series.data())")
    assert len(band_data) == 3
    for track in band_data:
        assert len(track) == 6
        assert track[0]["time"] == bars[19]["date"]
    for index in range(19, len(bars)):
        expected = expected_bands(bars[:index + 1], "adj_close")
        actual = [track[index - 19]["value"] for track in band_data]
        assert actual == pytest.approx(expected)

    page.locator("#daily-bars-chart").scroll_into_view_if_needed()
    position = page.evaluate("""() => {
      const bar = dailyTestSeries[0].data().at(-3);
      const rect = document.getElementById('daily-bars-chart').getBoundingClientRect();
      return {x: rect.x + dailyTestChart.timeScale().timeToCoordinate(bar.time),
              y: rect.y + dailyTestSeries[0].priceToCoordinate(bar.close)};
    }""")
    page.mouse.move(position["x"], position["y"])
    expect(page.locator("#daily-bars-quote")).to_contain_text(bars[-3]["date"])
    hovered = expected_bands(bars[:-2], "adj_close")
    for key, value in zip(("upper", "middle", "lower"), hovered, strict=True):
        expect(page.locator(f'[data-bollinger-value="{key}"]')).to_have_text(f"{value:.2f}")

    toggle.uncheck()
    page.locator("#daily-bars-adjustment").select_option("split_adjusted")
    page.locator('[data-months="3"]').click()
    assert page.evaluate("dailyTestSeries.slice(6, 9).every(series => !series.options().visible)")
    actual = page.evaluate("dailyTestSeries.slice(6, 9).map(series => series.data().at(-1).value)")
    assert actual == pytest.approx(expected_bands(bars, "close"))
    toggle.check()
    assert page.evaluate("dailyTestSeries.slice(6, 9).every(series => series.options().visible)")
    assert state["reads"] == 1 and state["refreshes"] == []


@pytest.mark.parametrize("count", [19, 20])
def test_bollinger_needs_twenty_sessions_and_flat_prices_have_zero_width(
    daily_page: tuple[Any, dict[str, Any]], count: int,
) -> None:
    from playwright.sync_api import expect

    page, state = daily_page
    set_prices(state, count, constant=True)
    page.goto(state["url"])
    expect(page.locator("#daily-bars-status")).to_have_attribute("data-state", "ready")
    tracks = page.evaluate("dailyTestSeries.slice(6, 9).map(series => series.data())")
    assert len(tracks) == 3
    for track in tracks:
        assert len(track) == count - 19
        if count == 20:
            assert track[0]["value"] == 42
            assert track[0]["time"] == state["payload"]["bars"][-1]["date"]
    for key in ("upper", "middle", "lower"):
        expect(page.locator(f'[data-bollinger-value="{key}"]')).to_have_text(
            "42.00" if count == 20 else "—"
        )
