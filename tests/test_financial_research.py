from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security
from trade_news_analysis.research_data_models import ResearchExpectation, ResearchFinancialFact
from trade_news_analysis.services.financial_research import get_financial_research
from trade_news_analysis.services.research_data import upsert_calendar

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
URL = "https://www.sec.gov/Archives/edgar/data/1/report.htm"
PERIODS = [
    (date(2025, 1, 1), date(2025, 3, 31)),
    (date(2025, 4, 1), date(2025, 6, 30)),
    (date(2025, 7, 1), date(2025, 9, 30)),
    (date(2025, 10, 1), date(2025, 12, 31)),
]


def sid(session: Session) -> int:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    return security.id


def fact(
    session: Session,
    start: date | None,
    end: date,
    value: float,
    *,
    concept: str = "us-gaap:Revenues",
    unit: str = "USD",
    observed: datetime | None = None,
    available: datetime | None = None,
) -> ResearchFinancialFact:
    observed = observed or NOW - timedelta(days=1)
    available = available or observed
    key = json.dumps([str(start), str(end), value, concept, unit, str(observed), str(available)])
    item = ResearchFinancialFact(
        fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        security_id=sid(session),
        source="sec_companyfacts",
        source_url=URL,
        concept=concept,
        value=value,
        unit=unit,
        period_start=start,
        period_end=end,
        fiscal_period="Q4",
        accession=str(observed),
        form="10-Q",
        available_at=available,
        observed_at=observed,
    )
    session.add(item)
    session.flush()
    return item


def quarters(session: Session) -> None:
    for index, (start, end) in enumerate(PERIODS):
        fact(session, start, end, (index + 1) * 10)


def test_four_contiguous_actual_quarters_produce_ttm(session: Session) -> None:
    quarters(session)
    result = get_financial_research(session, sid(session), NOW)
    metric = result["metrics"][0]
    assert metric["quarter"]["value"] == 40
    assert metric["ttm"]["value"] == 100
    assert metric["ttm"]["method"] == "four_contiguous_quarters"
    assert metric["ttm"]["period_start"] == "2025-01-01"
    assert len(metric["ttm"]["components"]) == 4
    json.dumps(result)


def test_restatement_only_affects_queries_after_both_observation_and_availability(
    session: Session,
) -> None:
    quarters(session)
    start, end = PERIODS[0]
    fact(session, start, end, 100, observed=NOW + timedelta(days=1), available=NOW)
    assert get_financial_research(session, sid(session), NOW)["metrics"][0]["ttm"]["value"] == 100
    assert (
        get_financial_research(session, sid(session), NOW + timedelta(days=2))["metrics"][0]["ttm"][
            "value"
        ]
        == 190
    )


def test_future_availability_hides_already_observed_fact(session: Session) -> None:
    fact(session, *PERIODS[0], 10, available=NOW + timedelta(days=2))
    assert get_financial_research(session, sid(session), NOW)["metrics"] == []


def test_annual_plus_comparable_ytd_produces_ttm(session: Session) -> None:
    fact(session, date(2025, 1, 1), date(2025, 12, 31), 400)
    fact(session, date(2025, 1, 1), date(2025, 6, 30), 150)
    fact(session, date(2026, 1, 1), date(2026, 6, 30), 200)
    ttm = get_financial_research(session, sid(session), NOW)["metrics"][0]["ttm"]
    assert ttm["value"] == 450
    assert ttm["period_start"] == "2025-07-01"
    assert ttm["period_end"] == "2026-06-30"


def test_ytd_with_different_start_or_period_length_cannot_be_compared(session: Session) -> None:
    fact(session, date(2025, 1, 1), date(2025, 12, 31), 400)
    fact(session, date(2025, 1, 1), date(2025, 3, 31), 150)
    fact(session, date(2026, 1, 1), date(2026, 6, 30), 200)
    assert (
        get_financial_research(session, sid(session), NOW)["metrics"][0]["ttm"]["status"]
        == "unknown"
    )


def test_overlapping_quarters_never_sum_to_ttm(session: Session) -> None:
    for index, (start, end) in enumerate(PERIODS):
        fact(session, start - timedelta(days=1) if index == 1 else start, end, 10)
    assert (
        get_financial_research(session, sid(session), NOW)["metrics"][0]["ttm"]["status"]
        == "unknown"
    )


def test_units_and_nonadditive_indicators_are_never_mixed(session: Session) -> None:
    for index, (start, end) in enumerate(PERIODS):
        fact(session, start, end, 10, unit="EUR" if index == 3 else "USD")
        fact(session, start, end, 20, concept="tushare:roe", unit="percent")
    assert all(
        metric["ttm"]["status"] == "unknown"
        for metric in get_financial_research(session, sid(session), NOW)["metrics"]
    )


def test_fiscal_label_without_real_start_date_is_not_a_quarter(session: Session) -> None:
    fact(session, None, date(2026, 6, 30), 25)
    item = get_financial_research(session, sid(session), NOW)["metrics"][0]
    assert item["latest"]["period_kind"] == "instant_or_unspecified"
    assert item["quarter"]["status"] == "unknown"


def event(
    session: Session, *, now: datetime, details: dict[str, Any], day: date = date(2026, 9, 15)
) -> Any:
    return upsert_calendar(
        session,
        security_id=sid(session),
        event_key="finnhub:q2",
        title="财报",
        event_type="earnings",
        scheduled_date=day,
        scheduled_at=None,
        timezone="America/New_York",
        status="provider_reported",
        source="finnhub",
        source_url="https://finnhub.io/docs/api/earnings-calendar",
        details={"year": 2026, "quarter": 2, **details},
        now=now,
    )


def expectation(
    session: Session,
    calendar: Any,
    *,
    now: datetime,
    value: float = 1,
    period: str = "2026Q2",
    unit: str = "USD/share",
    metric: str = "eps",
) -> None:
    row = ResearchExpectation(
        fingerprint=hashlib.sha256(f"{calendar.id}:{now}:{value}:{unit}".encode()).hexdigest(),
        security_id=sid(session),
        event_key="finnhub:q2",
        calendar_revision_id=calendar.id,
        metric=metric,
        value=value,
        unit=unit,
        financial_period=period,
        source="finnhub",
        source_url="https://finnhub.io/docs/api/earnings-calendar",
        estimate_kind="provider_estimate",
        is_pre_release=True,
        available_at=now,
        observed_at=now,
    )
    session.add(row)
    session.flush()


def test_pre_release_same_period_units_compare_zero_actual(session: Session) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={})
    expectation(session, initial, now=early, value=1)
    event(session, now=NOW, details={"epsActual": 0, "eps_unit": "USD/share"})
    surprise = get_financial_research(session, sid(session), NOW)["surprises"][0]
    assert surprise["status"] == "available"
    assert surprise["actual"] == 0
    assert surprise["difference"] == -1
    assert surprise["surprise_pct"] == -100


def test_zero_expectation_has_absolute_difference_without_percentage(session: Session) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={})
    expectation(session, initial, now=early, value=0)
    event(session, now=NOW, details={"epsActual": 1, "eps_unit": "USD/share"})
    surprise = get_financial_research(session, sid(session), NOW)["surprises"][0]
    assert surprise["difference"] == 1
    assert surprise["surprise_pct"] is None


def test_actual_currency_is_not_inferred_from_security_quote_currency(session: Session) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={})
    expectation(session, initial, now=early)
    event(session, now=NOW, details={"epsActual": 2})
    assert get_financial_research(session, sid(session), NOW)["surprises"][0]["status"] == "unknown"


def test_period_or_unit_mismatch_prevents_comparison(session: Session) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={})
    expectation(session, initial, now=early, period="2026Q1")
    expectation(session, initial, now=early, unit="CNY/share")
    event(session, now=NOW, details={"epsActual": 2, "eps_unit": "USD/share"})
    assert get_financial_research(session, sid(session), NOW)["surprises"][0]["status"] == "unknown"


def test_date_revisions_cannot_make_post_release_expectation_pre_release(session: Session) -> None:
    early = NOW - timedelta(days=5)
    original = event(session, now=early, details={})
    reported = event(
        session, now=NOW - timedelta(days=1), details={"epsActual": 2, "eps_unit": "USD/share"}
    )
    expectation(session, original, now=NOW - timedelta(hours=12))
    event(session, now=NOW, day=date(2026, 9, 30), details=reported.details)
    result = get_financial_research(session, sid(session), NOW)
    assert result["surprises"][0]["status"] == "unknown"


def test_historical_query_does_not_see_later_actual_result(session: Session) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={})
    expectation(session, initial, now=early)
    event(session, now=NOW, details={"epsActual": 2, "eps_unit": "USD/share"})
    assert get_financial_research(session, sid(session), early)["surprises"] == []


def test_actual_publication_time_excludes_estimate_recorded_after_early_release(
    session: Session,
) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={}, day=date(2026, 9, 30))
    expectation(session, initial, now=NOW - timedelta(hours=12))
    event(
        session,
        now=NOW,
        day=date(2026, 9, 30),
        details={
            "financial_period": "2026Q2",
            "epsActual": 2,
            "eps_unit": "USD/share",
            "actual_published_at": (NOW - timedelta(days=1)).isoformat(),
        },
    )
    assert get_financial_research(session, sid(session), NOW)["surprises"][0]["status"] == "unknown"


def test_expectation_cannot_reference_calendar_revision_unknown_at_its_recording_time(
    session: Session,
) -> None:
    initial = event(session, now=NOW - timedelta(days=3), details={})
    expectation(session, initial, now=NOW - timedelta(days=4))
    event(session, now=NOW, details={"epsActual": 2, "eps_unit": "USD/share"})
    assert get_financial_research(session, sid(session), NOW)["surprises"][0]["status"] == "unknown"


@pytest.mark.parametrize(
    "unit",
    ["unknown", " UNKNOWN ", "unknown/share", "USD/unknown", "N/A", "未确认", " ", "--",
     "not_available"],
)
@pytest.mark.parametrize(
    ("metric", "actual_key", "unit_key"),
    [("eps", "epsActual", "eps_unit"), ("revenue", "revenueActual", "revenue_unit")],
)
def test_matching_unknown_units_do_not_create_pre_release_surprise(
    session: Session, unit: str, metric: str, actual_key: str, unit_key: str
) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={unit_key: unit})
    expectation(session, initial, now=early, value=1, unit=unit, metric=metric)
    assert get_financial_research(session, sid(session), early)["surprises"] == []

    event(session, now=NOW, details={actual_key: 2, unit_key: unit})
    surprise = get_financial_research(session, sid(session), NOW)["surprises"][0]
    assert surprise["actual"] == 2
    assert surprise["status"] == "unknown"
    assert surprise["expected"] is None
    assert surprise["difference"] is None
    assert surprise["surprise_pct"] is None
    assert "报告单位" in surprise["reason"]


def test_later_confirmed_actual_unit_does_not_relabel_unknown_pre_release_estimate(
    session: Session,
) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={"eps_unit": "unknown"})
    expectation(session, initial, now=early, value=1, unit="unknown")
    event(session, now=NOW, details={"epsActual": 2, "eps_unit": "USD/share"})
    surprise = get_financial_research(session, sid(session), NOW)["surprises"][0]
    assert surprise["status"] == "unknown"
    assert surprise["difference"] is None


def test_metric_release_time_and_source_are_independent_of_later_other_metric(
    session: Session,
) -> None:
    early = NOW - timedelta(days=5)
    initial = event(session, now=early, details={}, day=date(2026, 9, 30))
    expectation(session, initial, now=NOW - timedelta(hours=12))
    details = {
        "epsActual": 2,
        "eps_unit": "USD/share",
        "eps_source_url": URL,
        "eps_published_at": (NOW - timedelta(days=1)).isoformat(),
        "revenueActual": 500,
        "revenue_unit": "USD",
        "revenue_source_url": URL + "?revenue",
        "revenue_published_at": NOW.isoformat(),
        "actual_published_at": NOW.isoformat(),
    }
    event(session, now=NOW, day=date(2026, 9, 30), details=details)
    rows = get_financial_research(session, sid(session), NOW)["surprises"]
    eps = next(row for row in rows if row["metric"] == "eps")
    revenue = next(row for row in rows if row["metric"] == "revenue")
    assert eps["status"] == "unknown"
    assert eps["source_url"] == URL
    assert eps["actual_published_at"] == details["eps_published_at"]
    assert revenue["source_url"] == URL + "?revenue"
    assert revenue["actual_published_at"] == details["revenue_published_at"]

    # A later correction cannot turn an estimate observed after the first release into a forecast.
    event(
        session, now=NOW + timedelta(hours=1), day=date(2026, 9, 30),
        details={**details, "eps_published_at": NOW.isoformat()},
    )
    eps = get_financial_research(session, sid(session), NOW + timedelta(hours=1))["surprises"][0]
    assert eps["status"] == "unknown"
