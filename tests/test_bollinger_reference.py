from __future__ import annotations

import math
from copy import deepcopy
from datetime import date, timedelta
from statistics import mean, pstdev
from typing import Any

import pytest

from trade_news_analysis.services.bollinger_reference import (
    BollingerParameters,
    analyze_bollinger,
)


def bars_for(prices: list[float]) -> list[dict[str, Any]]:
    return [
        {
            "date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
            "open": close,
            "high": close + 0.3,
            "low": close - 0.3,
            "close": close,
            "volume": 1000,
        }
        for i, close in enumerate(prices)
    ]


def entry_bars() -> list[dict[str, Any]]:
    prices = [100 + 2 * math.sin(i / 2) for i in range(130)]
    prices += [100 + 0.15 * math.sin(i / 2) for i in range(30)]
    prices += [104] * 5
    prices.append(mean(prices[-19:]))
    bars = bars_for(prices)
    bars[-1]["volume"] = 500
    return bars


def evaluate(bars: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return analyze_bollinger(bars, adjustment="split_adjusted", **kwargs)


def check(result: dict[str, Any], key: str) -> bool | None:
    return next(item["passed"] for item in result["checks"] if item["key"] == key)


def test_bands_use_population_deviation_and_entire_input_is_unchanged() -> None:
    bars = entry_bars()
    saved = deepcopy(bars)
    result = evaluate(bars)
    closes = [bar["close"] for bar in bars[-20:]]
    assert result["levels"]["middle"] == pytest.approx(mean(closes))
    assert result["levels"]["upper"] == pytest.approx(mean(closes) + 2 * pstdev(closes))
    assert result["levels"]["lower"] == pytest.approx(mean(closes) - 2 * pstdev(closes))
    assert bars == saved


def test_default_positive_setup_records_only_close_confirmed_first_pullback() -> None:
    bars = entry_bars()
    result = evaluate(bars)
    assert result["status"] == "ready"
    assert result["action"]["code"] == "entry_reference"
    assert result["setup"]["state"] == "pullback_confirmed"
    assert [item["code"] for item in result["events"]] == [
        "squeeze",
        "breakout",
        "pullback_confirmed",
    ]
    assert result["setup"]["breakout_date"] == bars[-6]["date"]
    assert result["setup"]["first_pullback_date"] == bars[-1]["date"]
    assert check(result, "gap") is False
    assert check(result, "contraction") is False
    assert check(result, "volume") is True
    assert result["levels"]["initial_risk_pct"] < 8
    assert result["add_on"]["enabled"] is False
    assert result["add_on"]["tranches"] == [20, 20, 20, 40]
    assert "首次加仓仍需" in " ".join(result["add_on"]["conditions"])


def test_future_append_or_change_does_not_rewrite_confirmed_events() -> None:
    bars = entry_bars()
    extra = bars_for([item["close"] for item in bars] + [103, 98, 105, 104])
    extra[: len(bars)] = deepcopy(bars)
    full = evaluate(extra)
    for length in range(142, len(bars) + 1):
        past = evaluate(extra[:length])
        assert past["events"] == [
            item for item in full["events"] if item["date"] <= extra[length - 1]["date"]
        ]
    assert evaluate(bars)["events"] == full["events"][:3]


def test_breakout_day_cannot_count_as_first_pullback() -> None:
    bars = entry_bars()[:-5]
    bars[-1]["low"] = 99
    result = evaluate(bars)
    assert result["setup"]["state"] == "waiting_pullback"
    assert result["setup"]["first_pullback_date"] is None
    assert result["action"]["code"] == "wait"


def test_failed_first_touch_is_never_replaced_with_second_touch() -> None:
    bars = entry_bars()
    bars[-1]["volume"] = 1000
    first = evaluate(bars)
    assert first["setup"]["state"] == "pullback_failed"
    following = deepcopy(bars[-1])
    following["date"] = (date.fromisoformat(following["date"]) + timedelta(days=1)).isoformat()
    following["volume"] = 100
    bars.append(following)
    second = evaluate(bars)
    assert second["setup"]["first_pullback_date"] == bars[-2]["date"]
    assert second["action"]["code"] == "wait"
    assert "当前不是首次买点" in second["action"]["reason"]
    assert sum(item["code"].startswith("pullback_") for item in second["events"]) == 1


@pytest.mark.parametrize("kind", ["same_day_stop", "gap_below_middle", "overextended"])
def test_ambiguous_intraday_path_or_chasing_never_becomes_entry(kind: str) -> None:
    bars = entry_bars()
    base = evaluate(bars)
    if kind == "same_day_stop":
        bars[-1]["low"] = base["levels"]["initial_stop"]
    elif kind == "gap_below_middle":
        bars[-1]["open"] = bars[-1]["low"] = 100
    else:
        bars[-1]["close"] += 1
        bars[-1]["high"] = bars[-1]["close"] + 0.3
    result = evaluate(bars)
    assert result["setup"]["state"] == "pullback_failed"
    assert result["action"]["code"] == "wait"
    assert "fill_price" not in str(result)


def test_initial_stop_uses_only_pivots_confirmed_before_breakout() -> None:
    bars = entry_bars()
    result = evaluate(bars)
    altered = deepcopy(bars)
    altered[-5]["low"] = 99
    # A new lower pivot confirmed after breakout cannot move its frozen initial stop.
    later = evaluate(altered)
    assert later["levels"]["initial_stop"] == result["levels"]["initial_stop"]
    assert all(item["confirmed_at"] > item["date"] for item in result["levels"]["supports"])


def test_no_confirmed_swing_low_does_not_invent_initial_stop() -> None:
    bars = bars_for([100.0] * 160 + [104.0] * 5 + [101.0])
    bars[-1]["volume"] = 100
    result = evaluate(bars)
    assert result["setup"]["breakout_date"] is not None
    assert result["levels"]["initial_stop"] is None
    assert result["action"]["code"] != "entry_reference"


def test_stop_distance_threshold_blocks_otherwise_qualified_entry() -> None:
    result = evaluate(entry_bars(), parameters={"max_stop_pct": 0.5})
    assert check(result, "initial_risk") is False
    assert result["setup"]["state"] == "pullback_failed"


def test_missing_volume_does_not_mean_zero_or_allow_entry() -> None:
    bars = entry_bars()
    bars[-1]["volume"] = None
    result = evaluate(bars)
    assert check(result, "volume") is None
    assert check(result, "volume_available") is False
    assert result["action"]["code"] != "entry_reference"


def test_adjustment_is_consistent_and_falls_back_for_whole_history() -> None:
    bars = entry_bars()
    for bar in bars:
        for key in ("open", "high", "low", "close"):
            bar[f"adj_{key}"] = bar[key] * 0.5
    adjusted = analyze_bollinger(bars)
    base = evaluate(bars)
    assert adjusted["price_basis"] == "total_return_adjusted"
    assert adjusted["levels"]["middle"] == pytest.approx(base["levels"]["middle"] / 2)
    assert adjusted["levels"]["initial_stop"] == pytest.approx(base["levels"]["initial_stop"] / 2)
    bars[0]["adj_low"] = None
    fallback = analyze_bollinger(bars)
    assert fallback["price_basis"] == "split_adjusted"
    assert fallback["events"] == base["events"]
    assert any("整段" in item for item in fallback["warnings"])


@pytest.mark.parametrize("count", [0, 19, 141])
def test_insufficient_history_is_explicit(count: int) -> None:
    result = evaluate(entry_bars()[:count])
    assert result["status"] == "insufficient"
    assert result["events"] == []


@pytest.mark.parametrize("corruption", ["duplicate", "reverse", "nan", "contradiction"])
def test_invalid_ohlc_or_dates_return_safe_status(corruption: str) -> None:
    bars = entry_bars()
    if corruption == "duplicate":
        bars[-1]["date"] = bars[-2]["date"]
    elif corruption == "reverse":
        bars.reverse()
    elif corruption == "nan":
        bars[-1]["close"] = float("nan")
    else:
        bars[-1]["high"] = bars[-1]["low"] - 1
    result = evaluate(bars)
    assert result["status"] == "invalid_data"
    assert result["action"]["code"] == "blocked"


def test_invalid_parameters_return_safe_status_and_fixed_periods_are_visible() -> None:
    assert evaluate(entry_bars(), parameters={"squeeze_days": 0})["status"] == "invalid_data"
    assert evaluate(entry_bars(), parameters={"bb_period": 10})["status"] == "invalid_data"
    assert BollingerParameters().model_dump()["bb_period"] == 20


@pytest.mark.parametrize("reliable,status", [(False, "long"), (True, "flat"), (True, "unknown")])
def test_unreliable_or_nonlong_position_cannot_trigger_stop_or_trailing(
    reliable: bool,
    status: str,
) -> None:
    result = evaluate(
        entry_bars(),
        position={
            "status": status,
            "reliable": reliable,
            "average_cost": 50,
            "current_stop": 110,
        },
    )
    assert result["action"]["code"] not in {"stop_triggered", "trailing_reference"}
    assert result["position"]["profit_pct"] is None
    assert result["levels"]["trailing_stop"] is None


def test_known_long_stop_uses_latest_close_not_historical_low_or_entry_stop() -> None:
    bars = entry_bars()
    current = bars[-1]["close"]
    position = {
        "status": "long",
        "reliable": True,
        "average_cost": current,
        "current_stop": current - 0.1,
    }
    assert bars[-1]["low"] < position["current_stop"]
    result = evaluate(bars, position=position)
    assert result["action"]["code"] == "wait"
    position["current_stop"] = current
    triggered = evaluate(bars, position=position)
    assert triggered["action"]["code"] == "stop_triggered"
    assert "未推定成交价格" in triggered["action"]["reason"]
    warmup = evaluate(bars[-1:], position=position)
    assert warmup["status"] == "insufficient"
    assert warmup["action"]["code"] == "stop_triggered"


@pytest.mark.parametrize("current_stop", [None, 99.0])
def test_trailing_uses_second_clustered_support_and_never_lowers_recorded_stop(
    current_stop: float | None,
) -> None:
    bars = entry_bars()
    result = evaluate(
        bars,
        position={
            "status": "long",
            "reliable": True,
            "average_cost": 90,
            "current_stop": current_stop,
        },
    )
    assert result["action"]["code"] == "trailing_reference"
    supports = result["levels"]["supports"]
    assert len(supports) == 2
    assert supports[0]["price"] > supports[1]["price"]
    assert result["levels"]["trailing_stop"] == max(current_stop or 0, supports[1]["price"])
    assert "未执行" in result["action"]["reason"] or "尚未执行" in result["action"]["reason"]
    assert result["add_on"]["enabled"] is False


def test_profit_gate_and_no_support_prevent_trailing() -> None:
    bars = entry_bars()
    result = evaluate(
        bars,
        position={
            "status": "long",
            "reliable": True,
            "average_cost": bars[-1]["close"] / 1.09,
            "current_stop": None,
        },
    )
    assert result["levels"]["trailing_stop"] is None
    assert result["action"]["code"] != "entry_reference"
    no_support = evaluate(
        bars_for([100.0] * 170),
        position={
            "status": "long",
            "reliable": True,
            "average_cost": 80,
            "current_stop": None,
        },
    )
    assert no_support["levels"]["supports"] == []
    assert no_support["levels"]["trailing_stop"] is None


def test_continuing_squeeze_keeps_setup_live_but_old_squeeze_expires() -> None:
    prices = [100 + 2 * math.sin(i / 2) for i in range(130)] + [100.0] * 60 + [104.0]
    active = evaluate(bars_for(prices))
    assert active["setup"]["state"] == "waiting_pullback"
    # Stretch the post-squeeze expansion without a qualifying breakout.
    drift = [100 + 2 * math.sin(i / 2) for i in range(130)] + [100.0] * 60
    drift += [100 + i * 0.1 for i in range(1, 15)]
    expired = evaluate(bars_for(drift), parameters={"pullback_max_bars": 2, "breakout_min_pct": 10})
    assert any(item["code"] == "squeeze_expired" for item in expired["events"])


def test_squeeze_first_confirmed_today_cannot_break_out_until_later() -> None:
    prices = [100 + 2 * math.sin(i / 2) for i in range(130)] + [100.0] * 11 + [104.0]
    result = evaluate(bars_for(prices), parameters={"squeeze_percentile": 100})
    assert result["events"][-1]["code"] == "squeeze"
    assert result["events"][-1]["date"] == bars_for(prices)[-1]["date"]
    prior_width = 4 * pstdev(prices[-21:-1]) / mean(prices[-21:-1])
    current_width = 4 * pstdev(prices[-20:]) / mean(prices[-20:])
    assert prices[-1] > mean(prices[-20:]) + 2 * pstdev(prices[-20:])
    assert current_width > prior_width * 1.1
    assert not any(item["code"] == "breakout" for item in result["events"])
    assert result["setup"]["breakout_date"] is None


@pytest.mark.parametrize(
    "direction,prices",
    [
        ("rising", [80 + i * 0.3 for i in range(170)]),
        ("falling", [140 - i * 0.3 for i in range(170)]),
        ("flat", [100.0] * 170),
        ("unknown", [100.0] * 20),
    ],
)
def test_lower_band_directions_are_observations_not_add_on_or_sell_instructions(
    direction: str,
    prices: list[float],
) -> None:
    result = evaluate(
        bars_for(prices),
        position={
            "status": "long",
            "reliable": True,
            "average_cost": prices[-1],
            "current_stop": None,
        },
    )
    lower = result["lower_band"]
    assert lower["direction"] == direction
    assert lower["lookback"] == 5
    assert lower["threshold_pct"] == 0.5
    if direction == "unknown":
        assert lower["slope_pct"] is None
        assert check(result, "lower_band_rising") is None
    else:
        current = mean(prices[-20:]) - 2 * pstdev(prices[-20:])
        prior = mean(prices[-25:-5]) - 2 * pstdev(prices[-25:-5])
        assert lower["slope_pct"] == pytest.approx((current / prior - 1) * 100)
        assert check(result, "lower_band_rising") is (direction == "rising")
    assert result["add_on"]["enabled"] is False
    assert result["action"]["code"] == "wait"
    assert "定义尚待确认" not in " ".join(result["add_on"]["conditions"])
