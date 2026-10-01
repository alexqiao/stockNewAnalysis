from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.research_data_models import ResearchCalendarRevision
from trade_news_analysis.services import official_calendars
from trade_news_analysis.services.research_data import upsert_calendar
from trade_news_analysis.services.research_refresh import refresh_research_data

NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)


def fomc(*meetings: tuple[int, str, str]) -> str:
    return "".join(
        '<div class="panel"><div class="panel-heading">'
        f'{year} FOMC Meetings</div><div class="fomc-meeting">'
        f'<div class="fomc-meeting__month">{month}</div>'
        f'<div class="fomc-meeting__date">{days}</div></div></div>'
        for year, month, days in meetings
    )


def bls(*meetings: tuple[str, str]) -> str:
    return (
        "BEGIN:VCALENDAR\n"
        + "".join(
            f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:Release {uid}\n"
            f"DTSTART;TZID=America/New_York:{day}T083000\nEND:VEVENT\n"
            for uid, day in meetings
        )
        + "END:VCALENDAR"
    )


def seed(
    session: Session,
    key: str,
    day: date,
    *,
    source: str = "fomc_calendar",
    status: str = "scheduled",
    scheduled_at: datetime | None = None,
) -> ResearchCalendarRevision:
    return upsert_calendar(
        session,
        security_id=None,
        event_key=key,
        title="Original calendar title",
        event_type="macro_policy",
        scheduled_date=day,
        scheduled_at=scheduled_at,
        timezone="America/New_York",
        status=status,
        source=source,
        source_url=(
            official_calendars.FOMC_URL if source == "fomc_calendar" else official_calendars.BLS_URL
        ),
        details={"original_evidence": {"retained": True}},
        now=NOW - timedelta(days=1),
    )


def refresh(
    session: Session,
    settings: Settings,
    *,
    fomc_text: str | Exception | None = None,
    bls_text: str | Exception | None = None,
    now: datetime = NOW,
) -> dict[str, Any]:
    def transport(url: str) -> object:
        if url == official_calendars.FOMC_URL:
            value = fomc_text if fomc_text is not None else fomc((2026, "October", "27-28"))
        elif url == official_calendars.BLS_URL:
            value = (
                bls_text if bls_text is not None else bls(("oct", "20261002"), ("nov", "20261106"))
            )
        else:
            raise ValueError("No macro actuals in calendar-only fixture")
        if isinstance(value, Exception):
            raise value
        return value

    return refresh_research_data(session, settings, [], now, transport)


def revisions(session: Session, key: str) -> list[ResearchCalendarRevision]:
    return list(
        session.scalars(
            select(ResearchCalendarRevision)
            .where(ResearchCalendarRevision.event_key == key)
            .order_by(ResearchCalendarRevision.revision)
        )
    )


def test_fomc_removed_future_event_gets_immutable_cancelled_revision(
    session: Session, settings: Settings
) -> None:
    original = seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    refresh(session, settings)
    rows = revisions(session, original.event_key)
    assert len(rows) == 2
    assert rows[0].status == "scheduled"
    assert rows[1].status == "cancelled"
    assert rows[1].scheduled_date == original.scheduled_date
    assert rows[1].title == original.title
    assert rows[1].source_url == original.source_url
    assert rows[1].details["original_evidence"] == {"retained": True}
    assert rows[1].details["source_removed"] is True
    assert "未推断改期" in rows[1].details["withdrawal_reason"]
    assert rows[1].details["reconciliation_coverage"] == {"kind": "panel_years", "years": [2026]}
    assert rows[1].observed_at.replace(tzinfo=UTC) == NOW
    assert rows[1].available_at.replace(tzinfo=UTC) == NOW
    assert revisions(session, "fomc:2026-10-28")[-1].status == "scheduled"


def test_fomc_reconciles_only_covered_years_source_and_future_latest_status(
    session: Session, settings: Settings
) -> None:
    keep = [
        seed(session, "past", date(2026, 9, 1)),
        seed(session, "uncovered-year", date(2028, 1, 1)),
        seed(session, "already-cancelled", date(2026, 11, 1), status="cancelled"),
        seed(session, "already-completed", date(2026, 11, 2), status="completed"),
        seed(session, "other-source", date(2026, 11, 3), source="other_official"),
        seed(session, "date-only-today", NOW.date()),
    ]
    seed(session, "next-year-covered", date(2027, 2, 1))
    refresh(
        session, settings,
        fomc_text=fomc((2026, "October", "27-28"), (2027, "March", "16-17")),
    )
    assert all(len(revisions(session, item.event_key)) == 1 for item in keep)
    assert revisions(session, "next-year-covered")[-1].status == "cancelled"


def test_bls_reconciles_only_inside_returned_date_range(
    session: Session, settings: Settings
) -> None:
    seed(session, "bls:removed", date(2026, 10, 15), source="bls_calendar")
    seed(session, "bls:before", date(2026, 10, 1), source="bls_calendar")
    seed(session, "bls:after", date(2026, 11, 7), source="bls_calendar")
    refresh(session, settings)
    removed = revisions(session, "bls:removed")[-1]
    assert removed.status == "cancelled"
    assert removed.details["reconciliation_coverage"] == {
        "kind": "date_range", "start": "2026-10-02", "end": "2026-11-06"
    }
    assert len(revisions(session, "bls:before")) == 1
    assert len(revisions(session, "bls:after")) == 1


def test_today_timed_future_can_be_withdrawn_but_past_time_is_retained(
    session: Session, settings: Settings
) -> None:
    seed(
        session, "bls:today-future", NOW.date(), source="bls_calendar",
        scheduled_at=NOW + timedelta(minutes=30),
    )
    seed(
        session, "bls:today-past", NOW.date(), source="bls_calendar",
        scheduled_at=NOW - timedelta(minutes=30),
    )
    refresh(session, settings, bls_text=bls(("today", "20260917"), ("nov", "20261106")))
    assert revisions(session, "bls:today-future")[-1].status == "cancelled"
    assert len(revisions(session, "bls:today-past")) == 1


def test_repeated_refresh_does_not_duplicate_withdrawal_and_reappearance_is_revision(
    session: Session, settings: Settings
) -> None:
    seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    refresh(session, settings)
    refresh(session, settings, now=NOW + timedelta(hours=7))
    assert len(revisions(session, "fomc:2026-11-05")) == 2
    refresh(
        session, settings, fomc_text=fomc((2026, "November", "4-5")),
        now=NOW + timedelta(hours=14),
    )
    rows = revisions(session, "fomc:2026-11-05")
    assert len(rows) == 3
    assert rows[-1].status == "scheduled"
    assert not rows[-1].details.get("source_removed")


@pytest.mark.parametrize("failure", ["<html>Denied</html>", RuntimeError("Unavailable"), ""])
def test_failed_or_empty_source_never_withdraws_events(
    session: Session, settings: Settings, failure: str | Exception
) -> None:
    seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    seed(session, "bls:removed", date(2026, 10, 15), source="bls_calendar")
    report = refresh(session, settings, fomc_text=failure, bls_text=failure)
    sources = {item["source"]: item for item in report["sources"]}
    assert sources["fomc_calendar"]["status"] == "degraded"
    assert sources["bls_calendar"]["status"] == "degraded"
    assert len(revisions(session, "fomc:2026-11-05")) == 1
    assert len(revisions(session, "bls:removed")) == 1


def test_empty_parser_output_never_withdraws_events(
    session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    monkeypatch.setattr(official_calendars, "parse_fomc_calendar", lambda _text: [])
    report = refresh(session, settings)
    state = next(row for row in report["sources"] if row["source"] == "fomc_calendar")
    assert state["status"] == "degraded"
    assert len(revisions(session, "fomc:2026-11-05")) == 1


def test_partial_invalid_calendar_rolls_back_all_source_updates(
    session: Session, settings: Settings
) -> None:
    seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    refresh(
        session, settings,
        fomc_text=fomc((2026, "October", "27-28"), (2026, "November", "TBD")),
    )
    assert len(revisions(session, "fomc:2026-11-05")) == 1
    assert not revisions(session, "fomc:2026-10-28")


def test_latest_override_from_another_source_is_respected(
    session: Session, settings: Settings
) -> None:
    seed(session, "fomc:2026-11-05", date(2026, 11, 5))
    seed(session, "fomc:2026-11-05", date(2026, 11, 5), source="manual_official")
    refresh(session, settings)
    rows = revisions(session, "fomc:2026-11-05")
    assert len(rows) == 2
    assert rows[-1].status == "scheduled"
    assert rows[-1].source == "manual_official"
