from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from trade_news_analysis.services.market_research import calendar_for_market, quote_session_age
from trade_news_analysis.services.official_calendars import parse_bls_calendar, parse_fomc_calendar


def ics(*events: str, header: str = "") -> str:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + header
        + "".join(f"BEGIN:VEVENT\r\n{event}\r\nEND:VEVENT\r\n" for event in events)
        + "END:VCALENDAR\r\n"
    )


def event(start: str, *, uid: str = "cpi", extra: str = "") -> str:
    return f"UID:{uid}\r\nSUMMARY:Consumer Price Index\r\nDTSTART{start}\r\n{extra}"


def meeting(month: str, days: str) -> str:
    return (
        '<div class="row fomc-meeting">'
        f'<div class="fomc-meeting__month"><strong>{month}</strong></div>'
        f'<div class="fomc-meeting__date">{days}</div></div>'
    )


def panel(year: int, *meetings: str) -> str:
    return (
        '<div class="panel panel-default"><div class="panel-heading">'
        f"<h4>{year} FOMC Meetings</h4></div>{''.join(meetings)}</div>"
    )


def test_bls_unfolds_lines_and_unescapes_text() -> None:
    text = ics(
        "UID:employment\r\nSUMMARY:Employment\\, wages\\; hours "
        "\r\n and earnings\r\nDTSTART;TZID=America/New_York:20260904T083000\r\n"
        "DESCRIPTION:First line\\nSecond line\\\\details"
    )
    row = parse_bls_calendar(text)[0]
    assert row["event_key"] == "bls:employment"
    assert row["title"] == "Employment, wages; hours and earnings"
    assert row["details"]["description"] == "First line Second line\\details"
    assert row["scheduled_at"] == datetime(2026, 9, 4, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize(("value", "hour"), [("20260109T083000", 13), ("20260904T083000", 12)])
def test_bls_named_timezone_observes_summer_and_winter(value: str, hour: int) -> None:
    row = parse_bls_calendar(ics(event(f';TZID="America/New_York":{value}')))[0]
    assert row["scheduled_at"].hour == hour
    assert row["scheduled_at"].utcoffset() == timedelta(0)
    assert row["details"]["time_precision"] == "datetime"


def test_bls_utc_converts_release_date_to_new_york() -> None:
    row = parse_bls_calendar(ics(event(":20260918T003000Z")))[0]
    assert row["scheduled_date"] == date(2026, 9, 17)
    assert row["scheduled_at"] == datetime(2026, 9, 18, 0, 30, tzinfo=UTC)


def test_bls_date_and_floating_time_keep_date_precision() -> None:
    rows = parse_bls_calendar(
        ics(event(";VALUE=DATE:20260918"), event(":20260918T083000", uid="floating"))
    )
    assert all(row["scheduled_at"] is None for row in rows)
    assert rows[0]["details"]["time_precision"] == "date"
    assert rows[1]["details"]["time_precision"] == "floating_time_without_timezone"
    explicit = parse_bls_calendar(
        ics(event(":20260918T083000"), header="X-WR-TIMEZONE:America/New_York\r\n")
    )[0]
    assert explicit["scheduled_at"] == datetime(2026, 9, 18, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize("value", ["20260308T023000", "20261101T013000"])
def test_bls_rejects_missing_or_ambiguous_dst_instant(value: str) -> None:
    with pytest.raises(ValueError, match="夏令时"):
        parse_bls_calendar(ics(event(f";TZID=America/New_York:{value}")))


def test_bls_keeps_uid_across_reschedule_and_latest_sequence() -> None:
    first = event(":20260918T123000Z", extra="SEQUENCE:0")
    revision = event(":20260921T123000Z", extra="SEQUENCE:2\r\nSTATUS:CANCELLED")
    rows = parse_bls_calendar(ics(revision, first))
    assert len(rows) == 1
    assert rows[0]["event_key"] == "bls:cpi"
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["scheduled_date"] == date(2026, 9, 21)
    assert rows[0]["details"]["sequence"] == 2
    assert parse_bls_calendar(ics(first, header="METHOD:CANCEL\r\n"))[0]["status"] == "cancelled"


def test_bls_recurrence_instance_has_separate_identity() -> None:
    rows = parse_bls_calendar(
        ics(
            event(":20260918T123000Z", extra="RECURRENCE-ID:20260918T123000Z"),
            event(":20261016T123000Z", extra="RECURRENCE-ID:20261016T123000Z"),
        )
    )
    assert len({row["event_key"] for row in rows}) == 2


@pytest.mark.parametrize(
    "text",
    [
        "<html>Access denied</html>",
        "BEGIN:VCALENDAR\nEND:VCALENDAR",
        ics("SUMMARY:Broken\nDTSTART:20260918T123000Z"),
        ics(event(";TZID=Not/AZone:20260918T083000")),
        ics(event(";TZID=America/New_York:20260918T123000Z")),
        ics(event(":20260918T123000Z", extra="RRULE:FREQ=MONTHLY")),
        ics(event(":20260918T123000Z"), event(":20260919T123000Z")),
        ics(event(":20260918T123000Z"))[:-18],
        ics(event(":20260918T123000Z"), "UID:incomplete"),
    ],
)
def test_bls_fails_instead_of_silently_accepting_bad_or_partial_calendar(text: str) -> None:
    with pytest.raises(ValueError):
        parse_bls_calendar(text)


def test_fomc_cross_months_panel_year_and_unknown_release_minute() -> None:
    rows = parse_fomc_calendar(
        panel(2024, meeting("Apr/May", "30-1"))
        + panel(2023, meeting("Jan/Feb", "31-1"))
        + panel(2026, meeting("Dec/Jan", "31-1*"))
    )
    assert [row["scheduled_date"] for row in rows] == [
        date(2024, 5, 1), date(2023, 2, 1), date(2027, 1, 1)
    ]
    assert all(row["scheduled_at"] is None for row in rows)
    assert rows[2]["details"]["economic_projections"] is True


def test_fomc_identity_does_not_change_with_reordering_or_insertions() -> None:
    march, september = meeting("March", "17-18*"), meeting("September", "15-16*")
    old = parse_fomc_calendar(panel(2026, march, september))
    new = parse_fomc_calendar(
        panel(2026, september, meeting("August", "22 (notation vote)"), march)
    )
    assert {row["scheduled_date"]: row["event_key"] for row in old}.items() <= {
        row["scheduled_date"]: row["event_key"] for row in new
    }.items()
    assert new[1]["details"]["notation_vote"] is True
    assert "书面表决" in new[1]["title"]


@pytest.mark.parametrize("days", ["18-19 (rescheduled from 17-18)", "<s>17-18</s> 18-19"])
def test_fomc_explicit_reschedule_keeps_original_identity(days: str) -> None:
    old = parse_fomc_calendar(panel(2026, meeting("March", "17-18")))[0]
    new = parse_fomc_calendar(panel(2026, meeting("March", days)))[0]
    assert old["event_key"] == new["event_key"]
    assert new["scheduled_date"] == date(2026, 3, 19)
    assert new["details"]["rescheduled"] is True
    assert new["details"]["original_scheduled_date"] == "2026-03-18"


@pytest.mark.parametrize("days", ["17-18 (cancelled)", "<s>17-18</s> (cancelled)"])
def test_fomc_cancelled_meeting_is_not_active(days: str) -> None:
    row = parse_fomc_calendar(panel(2026, meeting("March", days)))[0]
    assert row["status"] == "cancelled"
    assert row["scheduled_date"] == date(2026, 3, 18)


@pytest.mark.parametrize(
    "text",
    [
        "<html>Access denied</html>",
        panel(2026),
        panel(2026, meeting("September", "TBD")),
        panel(2026, meeting("Unknown", "1-2")),
        panel(2026, meeting("September", "16-15")),
        panel(2026, meeting("September", "17-181")),
        panel(2026, meeting("February", "30-31")),
        panel(2026, meeting("September", "15-16"), meeting("September", "TBD")),
        panel(2026, meeting("September", "15-16")).replace("2026 FOMC", "FOMC"),
        panel(2026, meeting("September", "15-16"), meeting("September", "16 (cancelled)")),
    ],
)
def test_fomc_fails_on_unrecognized_or_conflicting_meetings(text: str) -> None:
    with pytest.raises(ValueError):
        parse_fomc_calendar(text)


@pytest.mark.parametrize("market", ["A", "US", "HK"])
def test_exchange_calendars_cover_current_date_in_all_supported_markets(market: str) -> None:
    calendar = calendar_for_market(market)
    observed = calendar.session_close("2026-09-16").to_pydatetime()
    current = datetime(2026, 9, 17, 22, tzinfo=UTC)
    assert quote_session_age(market, observed, current) == 1
