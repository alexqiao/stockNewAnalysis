from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.models import Security
from trade_news_analysis.research_data_models import ResearchSourceState
from trade_news_analysis.services.research_data import (
    get_research_data,
    record_manual_calendar,
    record_manual_disclosure,
    record_manual_expectation,
    upsert_calendar,
    validate_official_url,
)

NOW = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)
URL = "https://www.sec.gov/Archives/edgar/data/1/report.htm"


def security_id(session: Session) -> int:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    return security.id


def add_calendar(session: Session, **updates: object) -> object:
    values = dict(
        event_key="q3",
        title="三季报",
        event_type="earnings",
        scheduled_date=date(2026, 9, 18),
        timezone="America/New_York",
        source_url=URL,
        now=NOW,
    )
    values.update(updates)
    return record_manual_calendar(session, security_id(session), **values)  # type: ignore[arg-type]


def test_manual_disclosure_keeps_actual_observation_time_and_deduplicates(session: Session) -> None:
    post = record_manual_disclosure(
        session,
        security_id(session),
        title="财报",
        source_url=URL,
        published_at=NOW - timedelta(days=2),
        excerpt="人工摘录",
        now=NOW,
    )
    again = record_manual_disclosure(
        session,
        security_id(session),
        title="财报",
        source_url=URL,
        published_at=NOW - timedelta(days=2),
        excerpt="人工摘录",
        now=NOW + timedelta(hours=1),
    )
    assert again.id == post.id
    assert post.available_at == NOW
    assert post.observed_at == NOW
    assert post.content_status == "user_excerpt"
    assert (
        get_research_data(session, security_id(session), NOW - timedelta(hours=1))["disclosures"]
        == []
    )
    assert len(get_research_data(session, security_id(session), NOW)["disclosures"]) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://www.sec.gov/report",
        "https://www.sec.gov.evil.test/report",
        "https://user:password@www.sec.gov/report",
        "https://www.sec.gov:8443/report",
        "https://127.0.0.1/report",
        "file:///tmp/report",
        "https://[broken",
    ],
)
def test_official_url_validation_rejects_unapproved_destinations(url: str) -> None:
    with pytest.raises(ValueError):
        validate_official_url(url)


def test_official_url_canonicalizes_and_drops_fragment() -> None:
    assert validate_official_url("https://WWW.SEC.GOV:443/report?q=a#section") == (
        "https://www.sec.gov/report?q=a"
    )


def test_explicit_security_ir_domain_is_accepted_only_for_that_host(session: Session) -> None:
    security = session.get(Security, security_id(session))
    assert security is not None
    security.provider_data = {"official_ir_url": "https://investor.example.com/events"}
    row = record_manual_disclosure(
        session, security.id, title="财报", source_url="https://investor.example.com/q3", now=NOW
    )
    assert row.source_url == "https://investor.example.com/q3"
    with pytest.raises(ValueError):
        record_manual_disclosure(
            session, security.id, title="财报", source_url="https://evil.example.com/q3", now=NOW
        )


def test_calendar_preserves_revisions_including_reversion(session: Session) -> None:
    add_calendar(session)
    add_calendar(session, now=NOW + timedelta(hours=1))
    assert (
        len(
            get_research_data(session, security_id(session), NOW + timedelta(hours=1))[
                "calendar_revisions"
            ]
        )
        == 1
    )
    add_calendar(session, scheduled_date=date(2026, 9, 20), now=NOW + timedelta(hours=2))
    add_calendar(session, scheduled_date=date(2026, 9, 18), now=NOW + timedelta(hours=3))
    before = get_research_data(session, security_id(session), NOW + timedelta(hours=1))
    after = get_research_data(session, security_id(session), NOW + timedelta(hours=4))
    assert before["calendar"][0]["revision"] == 1
    assert after["calendar"][0]["revision"] == 3
    assert len(after["calendar_revisions"]) == 3
    assert "_content_fingerprint" not in after["calendar"][0]["details"]
    json.dumps(after)


def test_calendar_rejects_naive_or_inconsistent_times(session: Session) -> None:
    with pytest.raises(ValueError, match="时区"):
        add_calendar(session, scheduled_at=datetime(2026, 9, 18, 9))
    with pytest.raises(ValueError, match="不一致"):
        add_calendar(session, scheduled_at=datetime(2026, 9, 19, 9, tzinfo=UTC))
    with pytest.raises(ValueError, match="时区"):
        add_calendar(session, timezone="Fake/Zone")


def test_manual_cancellation_updates_existing_provider_event_in_place(session: Session) -> None:
    ident = security_id(session)
    event_key = f"finnhub:{ident}:earnings:2026Q3"
    original = upsert_calendar(
        session, security_id=ident, event_key=event_key, title="财报",
        event_type="earnings", scheduled_date=date(2026, 9, 20), scheduled_at=None,
        timezone="America/New_York", status="provider_reported", source="finnhub",
        source_url="https://finnhub.io", details={"financial_period": "2026Q3"}, now=NOW,
    )
    cancelled = record_manual_calendar(
        session, ident, event_key=event_key, title="财报", event_type="earnings",
        scheduled_date=date(2026, 9, 20), source_url=URL, status="cancelled",
        now=NOW + timedelta(hours=1),
    )
    assert cancelled.event_key == original.event_key
    assert cancelled.revision == 2
    assert cancelled.details["financial_period"] == "2026Q3"
    assert original.status == "provider_reported"
    visible = get_research_data(session, ident, NOW + timedelta(hours=2))["calendar"]
    assert len(visible) == 1 and visible[0]["status"] == "cancelled"


def test_calendar_rejects_event_key_for_another_security(session: Session) -> None:
    first = security_id(session)
    second = session.scalar(select(Security).where(Security.symbol == "MSFT"))
    assert second is not None
    kwargs = dict(
        event_key="shared",
        title="财报",
        event_type="earnings",
        scheduled_date=date(2026, 9, 18),
        scheduled_at=None,
        timezone="America/New_York",
        status="scheduled",
        source="test",
        source_url=URL,
        now=NOW,
    )
    upsert_calendar(session, security_id=first, **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="其他证券"):
        upsert_calendar(session, security_id=second.id, **kwargs)  # type: ignore[arg-type]


def test_expectation_recorded_after_release_cannot_be_backdated(session: Session) -> None:
    add_calendar(session)
    early = record_manual_expectation(
        session,
        security_id(session),
        event_key="q3",
        metric="EPS",
        value=1.2,
        unit="USD/share",
        financial_period="2026Q3",
        source_url=URL,
        expected_at=NOW - timedelta(days=1),
        now=NOW,
    )
    assert early.is_pre_release is True
    later = NOW + timedelta(days=4)
    late = record_manual_expectation(
        session,
        security_id(session),
        event_key="q3",
        metric="EPS",
        value=1.3,
        unit="USD/share",
        financial_period="2026Q3",
        source_url=URL,
        expected_at=NOW - timedelta(days=1),
        now=later,
    )
    assert late.is_pre_release is False
    assert late.available_at == later
    assert len(get_research_data(session, security_id(session), NOW)["expectations"]) == 1
    with pytest.raises(ValueError, match="回填"):
        record_manual_expectation(
            session,
            security_id(session),
            event_key="q3",
            metric="EPS",
            value=1.3,
            unit="USD/share",
            financial_period="2026Q3",
            source_url=URL,
            observed_at=NOW,
            now=later,
        )


def test_date_only_release_does_not_assume_known_future_intraday_time(session: Session) -> None:
    add_calendar(session)
    value = record_manual_expectation(
        session,
        security_id(session),
        event_key="q3",
        metric="Revenue",
        value=100,
        unit="USD",
        financial_period="2026Q3",
        source_url=URL,
        now=datetime(2026, 9, 18, 12, tzinfo=UTC),
    )
    assert value.is_pre_release is False


def test_source_health_shows_stale_and_global_sources(session: Session) -> None:
    session.add(
        ResearchSourceState(
            source_key="BLS",
            security_id=None,
            capability="macro_calendar",
            status="available",
            coverage="official",
            last_attempt_at=NOW,
            last_success_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            items_last_run=2,
        )
    )
    session.flush()
    data = get_research_data(session, security_id(session), NOW + timedelta(hours=2))
    assert data["source_health"][0]["status"] == "stale"
    assert data["coverage_gaps"][0]["source"] == "BLS"
    json.dumps(data)


def test_manual_disclosure_rejects_future_or_naive_publication_time(session: Session) -> None:
    for timestamp in (NOW + timedelta(days=1), datetime(2026, 9, 15)):
        with pytest.raises(ValueError):
            record_manual_disclosure(
                session,
                security_id(session),
                title="财报",
                source_url=URL,
                published_at=timestamp,
                now=NOW,
            )
