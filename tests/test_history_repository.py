from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.daily_bar_models import DailyBar, DailyBarRevision, DailyBarSyncState
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.risk_models import MarketResearchSnapshot
from trade_news_analysis.services.daily_bars import DailyBarService, get_daily_bars
from trade_news_analysis.services.history_repository import HistoryRepository, cached_history
from trade_news_analysis.services.market_research import MarketResearchService, analyze_market_frame
from trade_news_analysis.services.risk import (
    build_risk_plan,
    save_portfolio_risk,
    save_security_risk,
)

from .test_daily_bars import NOW, Provider, apple_id, frame
from .test_market_research import apple, market_frame


class RangeProvider:
    name = "yfinance"

    def __init__(self, data: pd.DataFrame):
        self.data = data.copy()
        self.data.attrs["source"] = "yfinance"
        self.calls: list[tuple[date, date]] = []

    def history_range(
        self, market: str, symbol: str, start: date, end: date,
        provider_data: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        self.calls.append((start, end))
        return self.data.copy()

    def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
        raise AssertionError("验证和研究必须按实际日期请求")

    def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
        raise AssertionError("此测试不请求市场基准")


def test_read_only_repository_never_fetches_missing_data(session: Session) -> None:
    provider = RangeProvider(frame())
    repository = HistoryRepository(session, provider)
    result = repository.history(apple(session), date(2026, 9, 25), date(2026, 9, 28))
    assert result.empty
    assert provider.calls == []
    assert session.scalar(select(func.count()).select_from(DailyBar)) == 0


def test_background_stages_writes_and_subsequent_reads_share_chart_values(
    session: Session,
) -> None:
    security = apple(session)
    provider = RangeProvider(frame())
    repository = HistoryRepository(session, provider, allow_fetch=True)
    start, end = date(2026, 9, 25), date(2026, 9, 28)
    result = repository.history(security, start, end)
    assert provider.calls == [(start, end + timedelta(days=1))]
    assert session.scalar(select(func.count()).select_from(DailyBar)) == 0
    assert result.attrs["adjustment_status"] == "verified"
    repository.persist(session, NOW)
    session.commit()
    cached = HistoryRepository(session, provider, allow_fetch=True).history(security, start, end)
    chart = get_daily_bars(session, security, now=NOW)
    assert len(provider.calls) == 1
    assert cached.attrs["cache"] == "local"
    assert chart["source_version"] == cached.attrs["source_version"]
    assert chart["analysis_price_basis"] == cached.attrs["analysis_price_basis"]
    assert chart["bars"][0]["adj_open"] == cached.iloc[0]["Adj Open"]
    assert chart["bars"][1]["close"] == cached.iloc[1]["Close"]


def test_revision_keeps_old_and_new_prices_but_identical_refresh_is_not_a_revision(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    identifier = apple_id(session_factory)
    provider = Provider(frame(), frame(), frame(adjusted=94), frame(adjusted=94))
    service = DailyBarService(settings, provider)
    service.refresh_isolated(session_factory, [identifier], now=NOW)
    service.refresh_isolated(
        session_factory, [identifier], now=NOW + timedelta(minutes=1), force=True,
    )
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(DailyBarRevision)) == 1
        state = session.get(DailyBarSyncState, identifier)
        assert state is not None
        first_version = state.source_version
    service.refresh_isolated(
        session_factory, [identifier], now=NOW + timedelta(minutes=2), force=True,
    )
    with session_factory() as session:
        revisions = list(session.scalars(select(DailyBarRevision).order_by(DailyBarRevision.id)))
        assert len(revisions) == 2
        assert revisions[-1].previous_version == first_version
        change = revisions[-1].changes[0]
        assert change["before"]["adj_close"] == 95
        assert change["after"]["adj_close"] == 94
        assert set(session.scalars(select(DailyBar.adj_close))) == {94}
        revisions[0].source = "modified"
        with pytest.raises(ValueError, match="不可修改"):
            session.flush()


def test_unverified_cache_never_silently_becomes_verified(session: Session) -> None:
    security = apple(session)
    provider = RangeProvider(frame(adjusted=None))
    repository = HistoryRepository(session, provider, allow_fetch=True)
    repository.history(security, date(2026, 9, 25), date(2026, 9, 28))
    repository.persist(session, NOW)
    session.commit()
    result = cached_history(session, security, date(2026, 9, 25), date(2026, 9, 28))
    assert result.attrs["adjustment_status"] == "unavailable"
    result = HistoryRepository(session, provider).history(
        security, date(2026, 9, 25), date(2026, 9, 28),
    )
    assert result["Adj Open"].isna().all()


def test_a_share_retains_existing_provider_and_legacy_provider_uses_max(
    session: Session,
) -> None:
    security = apple(session)
    security.market, security.currency, security.timezone = "A", "CNY", "Asia/Shanghai"
    periods = []

    class LegacyProvider:
        name = "legacy"

        def history(self, market: str, symbol: str, period: str = "6mo") -> pd.DataFrame:
            periods.append(period)
            return pd.DataFrame({"Open": [100], "Close": [101]},
                                index=pd.to_datetime(["2024-01-02"]))

        def benchmark_history(self, market: str, period: str = "6mo") -> pd.DataFrame:
            raise AssertionError

    result = HistoryRepository(session, LegacyProvider(), allow_fetch=True).history(
        security, date(2024, 1, 2), date(2024, 1, 3),
    )
    assert periods == ["max"]
    assert result.index[0].date() == date(2024, 1, 2)
    assert result.attrs["source_version"]


def test_only_current_final_close_can_supply_a_raw_quote(session: Session) -> None:
    security = apple(session)
    provider = RangeProvider(frame())
    repository = HistoryRepository(session, provider, allow_fetch=True)
    data = repository.history(security, date(2026, 9, 25), date(2026, 9, 28))
    repository.persist(session, NOW)
    session.commit()
    result = analyze_market_frame(security, data, now=NOW)
    assert result["quote"]["price_basis"] == "raw"
    assert result["quote"]["history_price_basis"] == "split_adjusted"
    historical = cached_history(session, security, date(2026, 9, 25), date(2026, 9, 25))
    past = analyze_market_frame(security, historical,
                                now=datetime(2026, 9, 25, 22, tzinfo=UTC))
    assert past["quote"]["price_basis"] == "split_adjusted"


def test_revision_of_overlap_must_refetch_whole_saved_basis(session: Session) -> None:
    security = apple(session)
    old = RangeProvider(frame(("2025-01-02", "2026-09-25", "2026-09-28")))
    repo = HistoryRepository(session, old, allow_fetch=True)
    repo.history(security, date(2025, 1, 2), date(2026, 9, 28))
    repo.persist(session, NOW)
    session.commit()
    # Unverified metadata forces a cache miss without declaring a suspension.
    state = session.get(DailyBarSyncState, security.id)
    assert state is not None
    state.currency = "unknown"
    session.commit()
    changed = RangeProvider(frame(adjusted=94))
    repo = HistoryRepository(session, changed, allow_fetch=True)
    with pytest.raises(ValueError, match="完整修订缺少"):
        repo.history(security, date(2026, 9, 25), date(2026, 9, 28))
    assert changed.calls[-1] == (date(2025, 1, 2), date(2026, 9, 29))
    assert set(session.scalars(select(DailyBar.adj_close))) == {95}


def test_metadata_only_change_creates_a_revision(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    identifier = apple_id(session_factory)
    changed = frame()
    changed.attrs["timezone"] = "US/Eastern"
    service = DailyBarService(settings, Provider(frame(), changed))
    service.refresh_isolated(session_factory, [identifier], now=NOW)
    service.refresh_isolated(
        session_factory, [identifier], now=NOW + timedelta(minutes=1), force=True,
    )
    with session_factory() as session:
        revisions = list(session.scalars(select(DailyBarRevision).order_by(DailyBarRevision.id)))
        assert len(revisions) == 2
        assert revisions[1].changes == []
        assert revisions[1].metadata_json["timezone"] == "US/Eastern"
        assert revisions[1].previous_version == revisions[0].source_version


def test_current_cached_close_supports_risk_sizing_without_refreshing_quote_time(
    session: Session, settings: Settings,
) -> None:
    security = apple(session)
    data = market_frame(start="2026-01-02", end="2026-09-28")
    data.attrs.update(price_basis="split_adjusted", timezone="America/New_York")
    provider = RangeProvider(data)
    repository = HistoryRepository(session, provider, allow_fetch=True)
    repository.history(security, date(2026, 1, 2), date(2026, 9, 28))
    repository.persist(session, NOW)
    session.commit()
    save_portfolio_risk(session, {
        "total_value": 100_000, "available_cash": 80_000, "currency": "USD",
    })
    save_security_risk(session, security.id, {
        "current_weight": 0, "current_quantity": 0, "max_weight": 0.2,
        "risk_budget_pct": 0.01, "stop_price": 95, "sector_limit_pct": 0.4,
        "sector_current_weight": 0, "lot_size": 1, "max_participation_pct": 0.1,
        "fee_bps": 10, "slippage_bps": 10,
    })
    calls_before = len(provider.calls)
    MarketResearchService(settings, provider).refresh(session, [security.id], now=NOW)
    # The missing benchmark may fetch, but the cached stock does not.
    assert len(provider.calls) <= calls_before + 1
    risk = build_risk_plan(session, security.id, now=NOW)
    assert risk["status"] == "ready"
    assert risk["max_buy_quantity"] > 0
    assert risk["quote"]["as_of"] == "2026-09-28T20:00:00+00:00"
    assert risk["quote"]["history_price_basis"] == "split_adjusted"


def test_cached_rebuild_never_fetches_and_does_not_replace_good_snapshot_with_a_gap(
    session: Session, session_factory: SessionFactory, settings: Settings,
) -> None:
    security = apple(session)
    data = market_frame(start="2026-08-03", end="2026-09-28")
    data.attrs.update(price_basis="split_adjusted", timezone="America/New_York")
    provider = RangeProvider(data)
    repository = HistoryRepository(session, provider, allow_fetch=True)
    repository.history(security, date(2026, 8, 3), date(2026, 9, 28))
    repository.persist(session, NOW)
    session.commit()
    calls_before = len(provider.calls)
    service = MarketResearchService(settings, provider)
    result = service.refresh_cached(session_factory, [security.id], now=NOW)
    assert result["updated_security_ids"] == [security.id]
    assert result["skipped"] == 0
    assert len(provider.calls) == calls_before
    with session_factory() as writer:
        writer.execute(delete(DailyBar).where(
            DailyBar.security_id == security.id, DailyBar.trade_date == date(2026, 9, 25),
        ))
        writer.commit()
    failed = service.refresh_cached(session_factory, [security.id], now=NOW)
    assert failed["updated_security_ids"] == []
    assert failed["skipped_ids"] == [security.id]
    assert len(provider.calls) == calls_before
    with session_factory() as reader:
        assert reader.scalar(select(func.count()).select_from(MarketResearchSnapshot)) == 1


def test_first_research_refresh_and_chart_have_same_committed_source_version(
    session: Session, session_factory: SessionFactory, settings: Settings,
) -> None:
    security = apple(session)
    data = market_frame(start="2026-04-01", end="2026-09-28")
    data.attrs.update(price_basis="split_adjusted", timezone="America/New_York")
    service = MarketResearchService(settings, RangeProvider(data))
    result = service.refresh_isolated(session_factory, [security.id], now=NOW)["results"][0]
    with session_factory() as reader:
        chart = get_daily_bars(reader, security, now=NOW)
    assert result["source_version"] == chart["source_version"]
    assert result["quote"]["source_version"] == chart["source_version"]


def test_two_staged_observations_keep_their_versions_and_never_splice_adjustments(
    session: Session,
) -> None:
    security = apple(session)

    class ChangingProvider(RangeProvider):
        def history_range(self, market, symbol, start, end, provider_data=None):
            self.calls.append((start, end))
            result = frame(adjusted=95 if len(self.calls) == 1 else 90)
            result.attrs["source"] = "yfinance"
            return result

    provider = ChangingProvider(frame())
    repository = HistoryRepository(session, provider, allow_fetch=True)
    first = repository.history(security, date(2026, 9, 25), date(2026, 9, 28))
    second = repository.history(security, date(2026, 9, 28), date(2026, 9, 28))
    assert len(provider.calls) == 3
    assert provider.calls[-1] == (date(2026, 9, 25), date(2026, 9, 29))
    versions = repository.persist(session, NOW)
    session.flush()
    assert set(session.scalars(select(DailyBar.adj_close))) == {90}
    revisions = list(session.scalars(select(DailyBarRevision).order_by(DailyBarRevision.id)))
    assert versions[first.attrs["source_version"]] == revisions[0].source_version
    assert versions[second.attrs["source_version"]] == revisions[1].source_version
    earlier = repository.history(security, date(2026, 9, 25), date(2026, 9, 28))
    assert earlier["Adj Close"].tolist() == [95, 95]
    assert earlier.attrs["source_version"] == revisions[0].source_version
