from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import exchange_calendars as xcals
import pandas as pd
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.models import Security
from trade_news_analysis.risk_models import MarketResearchSnapshot
from trade_news_analysis.services.market_research import (
    MarketResearchService,
    analyze_market_frame,
    closed_bars,
    get_market_research,
    quote_session_age,
)
from trade_news_analysis.services.risk import save_security_risk

NOW = datetime(2026, 9, 16, 22, tzinfo=UTC)


def market_frame(
    *,
    price: float = 100,
    start: str = "2026-08-03",
    end: str = "2026-09-16",
) -> pd.DataFrame:
    index = xcals.get_calendar("XNYS").sessions_in_range(start, end)
    frame = pd.DataFrame(
        {
            "Open": price,
            "High": price + 2,
            "Low": price - 2,
            "Close": price,
            "Volume": 10000.0,
            "Amount": price * 10000,
        },
        index=index,
    )
    for field in ("Open", "High", "Low", "Close"):
        frame[f"Adj {field}"] = frame[field]
    frame.attrs.update(
        source="fixture",
        price_basis="raw",
        analysis_price_basis="total_return_adjusted",
        adjustment_status="verified",
        volume_unit="shares",
        amount_unit="USD",
        currency="USD",
        amount_method="provider_reported",
    )
    return frame


class FixtureProvider:
    name = "fixture"

    def __init__(self, frames: dict[str, pd.DataFrame | Exception]):
        self.frames = frames
        self.calls: list[tuple[str, str]] = []

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        self.calls.append((market, symbol))
        result = self.frames[symbol]
        if isinstance(result, Exception):
            raise result
        return result.copy()

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        raise AssertionError("行业基准不能自动替换为市场基准")


def apple(session: Session) -> Security:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    return security


def test_market_returns_use_actual_sessions_and_exact_industry_alignment(session: Session) -> None:
    security = apple(session)
    frame = market_frame()
    frame.loc[frame.index[-1], ["Close", "Adj Close"]] = 101
    benchmark = market_frame()
    result = analyze_market_frame(security, frame, now=NOW, benchmark_frame=benchmark)

    assert result["status"] == "ready"
    for horizon in (1, 5, 20):
        data = result["horizons"][str(horizon)]
        assert data["start_date"] == frame.index[-horizon - 1].date().isoformat()
        assert data["absolute_return_pct"] == pytest.approx(1)
        assert data["industry_excess_return_pct"] == pytest.approx(1)
    assert result["liquidity"]["volume_ratio_20"] == pytest.approx(1)
    assert result["liquidity"]["average_amount_20"] == 1_000_000


def test_missing_benchmark_is_unknown_without_losing_stock_observations(session: Session) -> None:
    result = analyze_market_frame(apple(session), market_frame(), now=NOW)

    assert result["horizons"]["5"]["absolute_return_pct"] == 0
    assert result["horizons"]["5"]["industry_excess_return_pct"] is None
    assert result["horizons"]["5"]["blockers"]


def test_default_sector_benchmark_is_used_by_refresh_without_saving_settings(
    session: Session, settings: Settings,
) -> None:
    security = apple(session)
    provider = FixtureProvider({"AAPL": market_frame(), "XLK": market_frame()})
    result = MarketResearchService(settings, provider).refresh(
        session, [security.id], now=NOW,
    )["results"][0]
    assert result["benchmark"]["symbol"] == "XLK"
    assert "近似对照" in result["benchmark"]["label"]
    assert result["horizons"]["5"]["industry_excess_return_pct"] == 0
    assert not result["horizons"]["5"]["blockers"]


def test_unadjusted_prices_do_not_create_return_samples(session: Session) -> None:
    frame = market_frame()
    frame.attrs["adjustment_status"] = "unavailable"
    result = analyze_market_frame(apple(session), frame, now=NOW)

    assert result["status"] == "blocked"
    assert result["horizons"]["5"]["absolute_return_pct"] is None


def test_split_adjustment_protects_returns_and_blocks_unadjusted_volume_sizing(
    session: Session,
) -> None:
    frame = market_frame()
    for field in ("Open", "High", "Low", "Close"):
        frame.loc[frame.index[:-1], field] *= 2
    result = analyze_market_frame(apple(session), frame, now=NOW)

    assert result["horizons"]["5"]["absolute_return_pct"] == 0
    assert result["liquidity"]["company_action_in_volume_window"] is True
    assert result["liquidity"]["sizing_volume_ready"] is False


def test_half_day_close_and_chinese_holiday_are_calendar_driven() -> None:
    half_day = market_frame(start="2026-11-27", end="2026-11-27")
    assert not closed_bars(
        half_day, "US", "America/New_York", datetime(2026, 11, 27, 17, 59, tzinfo=UTC)
    )
    assert (
        len(
            closed_bars(
                half_day, "US", "America/New_York", datetime(2026, 11, 27, 18, 1, tzinfo=UTC)
            )
        )
        == 1
    )
    last_close = datetime(2026, 9, 30, 7, tzinfo=UTC)
    assert quote_session_age("A", last_close, datetime(2026, 10, 7, 12, tzinfo=UTC)) == 0
    assert quote_session_age("A", last_close, datetime(2026, 10, 9, 12, tzinfo=UTC)) == 2
    assert quote_session_age("A", last_close, datetime(2030, 10, 9, 12, tzinfo=UTC)) is None


def test_suspension_does_not_make_old_quote_fresh(session: Session) -> None:
    frame = market_frame(end="2026-09-10")
    result = analyze_market_frame(apple(session), frame, now=NOW)

    assert result["status"] == "blocked"
    assert result["quote"]["age_sessions"] > 1


def test_user_configured_benchmark_is_persisted_and_refresh_failure_is_visible(
    session: Session,
    settings: Settings,
) -> None:
    security = apple(session)
    save_security_risk(
        session,
        security.id,
        {
            "benchmark_market": "US",
            "benchmark_symbol": "XLK",
            "benchmark_currency": "USD",
        },
    )
    service = MarketResearchService(
        settings,
        FixtureProvider(
            {
                "AAPL": market_frame(),
                "XLK": market_frame(),
            }
        ),
    )
    report = service.refresh(session, [security.id], now=NOW)
    assert report["count"] == 1
    result = get_market_research(session, security.id, now=NOW)
    assert result["benchmark"]["symbol"] == "XLK"
    assert result["horizons"]["5"]["industry_return_pct"] == 0
    assert (
        get_market_research(session, security.id, now=datetime(2026, 9, 21, 22, tzinfo=UTC))[
            "status"
        ]
        == "stale"
    )
    bad = MarketResearchService(settings, FixtureProvider({"AAPL": RuntimeError("unavailable")}))
    bad.refresh(session, [security.id], now=NOW)
    assert get_market_research(session, security.id, now=NOW)["status"] == "error"


def test_refresh_requests_failed_shared_benchmark_once(
    session: Session, settings: Settings,
) -> None:
    securities = session.scalars(
        select(Security).where(Security.market == "US", Security.symbol.in_(["AAPL", "MSFT"]))
    ).all()
    assert len(securities) == 2
    for security in securities:
        save_security_risk(
            session, security.id,
            {"benchmark_market": "US", "benchmark_symbol": "XLK", "benchmark_currency": "USD"},
        )
    provider = FixtureProvider({
        "AAPL": market_frame(), "MSFT": market_frame(), "XLK": RuntimeError("unavailable"),
    })

    report = MarketResearchService(settings, provider).refresh(
        session, [security.id for security in securities], now=NOW,
    )

    assert provider.calls.count(("US", "XLK")) == 1
    for result in report["results"]:
        assert result["status"] == "ready"
        assert result["horizons"]["5"]["industry_excess_return_pct"] is None
        assert result["horizons"]["5"]["blockers"]


@pytest.mark.parametrize("benchmark_symbol", ["AAPL", "MSFT"])
def test_refresh_shares_stock_and_benchmark_requests(
    session: Session, settings: Settings, benchmark_symbol: str,
) -> None:
    security = apple(session)
    save_security_risk(
        session, security.id,
        {
            "benchmark_market": "US", "benchmark_symbol": benchmark_symbol,
            "benchmark_currency": "USD",
        },
    )
    provider = FixtureProvider({"AAPL": market_frame(), "MSFT": market_frame()})

    security_ids = list(session.scalars(
        select(Security.id).where(Security.market == "US", Security.symbol.in_(["AAPL", "MSFT"]))
    ))
    MarketResearchService(settings, provider).refresh(session, security_ids, now=NOW)

    assert sorted(provider.calls) == [("US", "AAPL"), ("US", "MSFT")]
    result = get_market_research(session, security.id, now=NOW)
    assert result["horizons"]["5"]["industry_excess_return_pct"] == 0
    assert result["quote"]["as_of"] == "2026-09-16T20:00:00+00:00"


@pytest.mark.parametrize("initial_failure", [False, True])
def test_refresh_retries_next_round_without_relabelling_old_prices(
    session: Session, settings: Settings, initial_failure: bool,
) -> None:
    security = apple(session)
    provider = FixtureProvider({
        "AAPL": RuntimeError("unavailable") if initial_failure else market_frame(),
    })
    service = MarketResearchService(settings, provider)
    first = service.refresh(session, [security.id], now=NOW)["results"][0]
    if initial_failure:
        assert first["status"] == "error"
        assert first["blockers"] == ["行情刷新失败（RuntimeError），请核对来源覆盖与权限"]
    provider.frames["AAPL"] = market_frame(price=150, end="2026-09-10")

    second = service.refresh(
        session, [security.id], now=NOW + timedelta(minutes=5),
    )["results"][0]

    assert provider.calls.count(("US", "AAPL")) == 2
    assert provider.calls.count(("US", "XLK")) == (1 if initial_failure else 2)
    assert second["quote"]["price"] == 150
    assert second["quote"]["as_of"] == "2026-09-10T20:00:00+00:00"
    assert second["status"] == "blocked"
    assert second["quote"]["age_sessions"] > 1


def test_shared_stock_failure_keeps_the_stock_specific_error(
    session: Session, settings: Settings,
) -> None:
    security = apple(session)
    save_security_risk(
        session, security.id,
        {"benchmark_market": "US", "benchmark_symbol": "MSFT", "benchmark_currency": "USD"},
    )
    securities = session.scalars(
        select(Security).where(Security.market == "US", Security.symbol.in_(["AAPL", "MSFT"]))
    ).all()
    provider = FixtureProvider({"AAPL": market_frame(), "MSFT": RuntimeError("unavailable")})

    report = MarketResearchService(settings, provider).refresh(
        session, [item.id for item in securities], now=NOW,
    )

    assert provider.calls.count(("US", "MSFT")) == 1
    for result in report["results"]:
        if result["security_id"] == security.id:
            assert result["status"] == "ready"
            assert result["horizons"]["5"]["industry_excess_return_pct"] is None
        else:
            assert result["status"] == "error"
            assert result["blockers"] == ["行情刷新失败（RuntimeError），请核对来源覆盖与权限"]


def test_default_refresh_only_fetches_active_watchlist_but_explicit_ids_remain_available(
    session: Session, settings: Settings,
) -> None:
    microsoft = session.scalar(select(Security).where(Security.symbol == "MSFT"))
    assert microsoft is not None and microsoft.watchlist_entry is not None
    microsoft.watchlist_entry.active = False
    outside_watchlist = Security(
        market="US", exchange="NASDAQ", symbol="NVDA", name="NVIDIA", active=True,
    )
    session.add(outside_watchlist)
    session.flush()
    provider = FixtureProvider({
        "AAPL": market_frame(), "MSFT": market_frame(), "NVDA": market_frame(),
    })
    service = MarketResearchService(settings, provider)

    default = service.refresh(session, now=NOW)

    assert default["count"] == 1
    assert provider.calls == [("US", "AAPL"), ("US", "XLK")]
    provider.calls.clear()
    explicit = service.refresh(session, [microsoft.id, outside_watchlist.id], now=NOW)

    assert explicit["count"] == 2
    assert sorted(provider.calls) == [("US", "MSFT"), ("US", "NVDA")]
    assert all(result["status"] == "ready" for result in explicit["results"])


def test_isolated_market_fetch_allows_concurrent_writes_and_reuses_failed_benchmark(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    with session_factory() as session:
        securities = list(session.scalars(
            select(Security).where(Security.symbol.in_(["AAPL", "MSFT"]))
        ))
        ids = [security.id for security in securities]
        for security in securities:
            save_security_risk(session, security.id, {
                "benchmark_market": "US", "benchmark_symbol": "XLK", "benchmark_currency": "USD",
            })
        session.commit()
    write_errors: list[Exception] = []

    class ConcurrentProvider(FixtureProvider):
        def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
            try:
                with session_factory() as other:
                    other.execute(text("PRAGMA busy_timeout=50"))
                    security = other.get(Security, ids[0])
                    assert security is not None
                    security.business_summary = f"User edit during {symbol}"
                    other.commit()
            except Exception as exc:
                write_errors.append(exc)
                raise
            return super().history(market, symbol, period)

    provider = ConcurrentProvider({
        "AAPL": market_frame(), "MSFT": market_frame(), "XLK": RuntimeError("unavailable"),
    })
    report = MarketResearchService(settings, provider).refresh_isolated(
        session_factory, ids, now=NOW,
    )

    assert not write_errors
    assert report["count"] == 2
    assert provider.calls.count(("US", "XLK")) == 1
    assert all(row["status"] == "ready" for row in report["results"])
    with session_factory() as session:
        assert session.scalar(select(func.count(MarketResearchSnapshot.id))) == 2


def test_isolated_market_refresh_keeps_previous_security_when_later_save_fails(
    monkeypatch: pytest.MonkeyPatch, session_factory: SessionFactory, settings: Settings,
) -> None:
    service = MarketResearchService(settings, FixtureProvider({
        "AAPL": market_frame(), "MSFT": market_frame(), "XLK": market_frame(),
    }))
    saved: list[int] = []
    save = service._save

    def fail_second(
        session: Session, timestamp: datetime, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if saved:
            raise RuntimeError("synthetic save failure")
        saved.append(payload["security_id"])
        return save(session, timestamp, payload)

    monkeypatch.setattr(service, "_save", fail_second)
    with pytest.raises(RuntimeError, match="synthetic save failure"):
        service.refresh_isolated(session_factory, now=NOW)

    with session_factory() as session:
        snapshots = list(session.scalars(select(MarketResearchSnapshot)))
        assert len(snapshots) == 1
        assert snapshots[0].security_id == saved[0]


def test_session_market_refresh_leaves_manual_changes_uncommitted(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    with session_factory() as session:
        security = apple(session)
        original = security.name
        security.name = "Uncommitted manual change"
        MarketResearchService(settings, FixtureProvider({"AAPL": market_frame()})).refresh(
            session, [security.id], now=NOW,
        )
        session.rollback()
    with session_factory() as session:
        assert apple(session).name == original
        assert session.scalar(select(func.count(MarketResearchSnapshot.id))) == 0


@pytest.mark.parametrize("change", ["currency", "basis", "dates"])
def test_incompatible_industry_comparison_is_unknown(session: Session, change: str) -> None:
    benchmark = market_frame()
    if change == "currency":
        benchmark.attrs["currency"] = "HKD"
    elif change == "basis":
        benchmark.attrs["analysis_price_basis"] = "split_adjusted"
    else:
        benchmark = benchmark.drop(benchmark.index[-3])
    result = analyze_market_frame(
        apple(session), market_frame(), now=NOW, benchmark_frame=benchmark
    )
    assert result["horizons"]["5"]["industry_excess_return_pct"] is None
