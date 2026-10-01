from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Any
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trade_news_analysis.config import Settings
from trade_news_analysis.db import SessionFactory
from trade_news_analysis.decision_models import MacroObservationVintage
from trade_news_analysis.models import IngestionRun, Security
from trade_news_analysis.research_data_models import (
    ResearchCalendarRevision,
    ResearchExpectation,
    ResearchFinancialFact,
    ResearchSourceState,
)
from trade_news_analysis.services.research_data import ResearchDataService, upsert_calendar
from trade_news_analysis.services.research_refresh import (
    ResearchTransport,
    _finnhub,
    _run_source,
    _source_failure,
    refresh_research_data,
    save_macro_vintages,
)

NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)


def test_finnhub_credentials_use_header_not_request_url(settings: Settings) -> None:
    transport = ResearchTransport(settings)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"earningsCalendar": []}'
    opener = MagicMock()
    opener.open.return_value = response
    transport.opener = opener
    transport("https://finnhub.io/api/v1/calendar/earnings?symbol=AAPL&token=test-secret")
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://finnhub.io/api/v1/calendar/earnings?symbol=AAPL"
    assert request.get_header("X-finnhub-token") == "test-secret"


def fake_transport(url: str) -> object:
    if "company_tickers" in url:
        return {"0": {"ticker": "AAPL", "cik_str": 320193}}
    if "companyfacts" in url:
        return {
            "cik": 320193,
            "facts": {
                "us-gaap": {
                    "Revenues": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2026-01-01",
                                    "end": "2026-03-31",
                                    "val": 0,
                                    "filed": "2026-04-28",
                                    "accn": "0000320193-26-000001",
                                    "form": "10-Q",
                                    "fy": 2026,
                                    "fp": "Q1",
                                },
                            ]
                        }
                    }
                }
            },
        }
    if "submissions" in url:
        return {"cik": "0000320193", "filings": {"recent": {}}}
    if "bls.ics" in url:
        return ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:bls-1\n"
                "DTSTART;TZID=America/New_York:20261002T083000\n"
                "SUMMARY:Employment situation\nEND:VEVENT\nEND:VCALENDAR")
    if "fomccalendars" in url:
        return ('<div class="panel"><div class="panel-heading">2026 FOMC Meetings</div>'
                '<div class="fomc-meeting"><div class="fomc-meeting__month">September</div>'
                '<div class="fomc-meeting__date">15-16</div></div></div>')
    if "api.bls.gov" in url:
        series = url.rsplit("/", 1)[-1]
        return {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": series,
                        "data": [{"year": "2026", "period": "M08", "value": "4.2"}],
                    },
                ]
            },
        }
    if "finnhub" in url:
        raise HTTPError(url, 403, "secret=DO_NOT_LEAK", Message(), None)
    raise AssertionError("未预期的端点")


def test_refresh_preserves_zero_caches_sources_and_sanitizes_denial(
    session: Session,
    settings: Settings,
) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security
    settings.finnhub_api_key = SecretStr("DO_NOT_LEAK")
    calls = []

    def fetch(url: str) -> object:
        calls.append(url)
        return fake_transport(url)

    refresh_research_data(session, settings, [security.id], NOW, fetch)
    fact = session.scalar(select(ResearchFinancialFact))
    assert fact and fact.value == 0
    states = session.scalars(select(ResearchSourceState)).all()
    denied = [state for state in states if state.source_key.startswith("finnhub")]
    assert all(state.status == "restricted" for state in denied)
    assert all("403" in state.message and "DO_NOT_LEAK" not in state.message for state in denied)
    assert all(state.coverage == "unavailable" for state in denied)
    assert all("接口权限" in state.message for state in denied)
    count = len(calls)
    refresh_research_data(session, settings, [security.id], NOW + timedelta(minutes=1), fetch)
    assert len(calls) == count
    assert session.scalar(select(func.count(ResearchFinancialFact.id))) == 1


def test_macro_initial_observation_and_revision_are_distinct(session: Session) -> None:
    payload: dict[str, Any] = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {
            "series": [
                {
                    "seriesID": "CES0000000001",
                    "data": [{"year": "2026", "period": "M08", "value": "100"}],
                }
            ]
        },
    }
    assert save_macro_vintages(session, payload, "CES0000000001", "thousand_people", NOW) == 1
    assert save_macro_vintages(session, payload, "CES0000000001", "thousand_people", NOW) == 0
    payload["Results"]["series"][0]["data"][0]["value"] = "101"
    assert (
        save_macro_vintages(
            session, payload, "CES0000000001", "thousand_people", NOW + timedelta(days=1)
        )
        == 1
    )
    rows = session.scalars(
        select(MacroObservationVintage).order_by(MacroObservationVintage.id)
    ).all()
    assert rows[0].metadata_values["previous_value"] is None
    assert rows[1].metadata_values["previous_value"] == 100


@pytest.mark.parametrize("status", ["reported", "cancelled", "confirmed"])
def test_provider_refresh_preserves_checked_official_revision(
    session: Session, settings: Settings, status: str,
) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None
    settings.finnhub_api_key = SecretStr("test-key")
    checked = upsert_calendar(
        session, security_id=security.id,
        event_key=f"finnhub:{security.id}:calendar/earnings:2026Q3",
        title="已核对财报", event_type="earnings", scheduled_date=NOW.date(),
        scheduled_at=NOW, timezone="UTC", status=status, source="manual_official",
        source_url="https://www.sec.gov/Archives/report.htm",
        details={"epsActual": 0, "eps_unit": "USD/share", "financial_period": "2026Q3"},
        now=NOW,
    )
    _finnhub(
        session, security,
        lambda _: {"earningsCalendar": [{"date": "2026-10-01", "year": 2026, "quarter": 3,
                                         "epsActual": None, "epsEstimate": 1}]},
        settings, NOW + timedelta(hours=7),
    )
    latest = session.scalar(
        select(ResearchCalendarRevision).order_by(ResearchCalendarRevision.id.desc())
    )
    assert latest is checked
    assert latest.details["epsActual"] == 0
    assert session.scalar(select(func.count(ResearchExpectation.id))) == 0


def test_failed_sec_directory_cannot_report_available_attachments(
    session: Session, settings: Settings,
) -> None:
    security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
    assert security is not None

    def denied(url: str) -> object:
        raise HTTPError(url, 403, "test", Message(), None)

    result = refresh_research_data(session, settings, [security.id], NOW, denied)
    states = {row["source"].split(":")[0]: row["status"] for row in result["sources"]}
    assert states["sec_companyfacts"] == states["sec_submissions"] == "restricted"
    assert states["sec_attachments"] == "degraded"


@pytest.mark.parametrize("key,code,status,reason", [
    ("finnhub_dividend:1", 403, "restricted", "接口权限"),
    ("finnhub_earnings:1", 401, "restricted", "凭据未通过认证"),
    ("bls_calendar", 403, "restricted", "网络出口"),
    ("bls_actual:CES0000000001", 403, "restricted", "网络出口"),
    ("sec_companyfacts:1", 429, "degraded", "频率受限"),
    ("sec_companyfacts:1", 503, "degraded", "暂未返回可用数据"),
])
def test_source_errors_have_actionable_messages_without_request_secrets(
    key: str, code: int, status: str, reason: str,
) -> None:
    actual, message = _source_failure(
        key, HTTPError("https://example.com?token=secret", code, "secret", Message(), None),
    )
    assert actual == status
    assert reason in message
    assert "secret" not in message
    assert "example.com" not in message


def test_research_run_is_marked_running_before_fetching_sources(
    monkeypatch: pytest.MonkeyPatch, session_factory: SessionFactory, settings: Settings,
) -> None:
    from trade_news_analysis.services import (
        action_evaluation,
        market_research,
        research_cycle,
        research_data,
    )
    from trade_news_analysis.services.coordinator import PipelineCoordinator

    coordinator = PipelineCoordinator(session_factory, settings)
    run_id = coordinator.ingestion.create_run("research")

    def refresh(
        _self: object, factory: SessionFactory, _ids: list[int] | None
    ) -> dict[str, object]:
        with factory() as session:
            run = session.get(IngestionRun, run_id)
            assert run is not None and run.status == "running"
        return {}

    monkeypatch.setattr(research_data.ResearchDataService, "refresh_isolated", refresh)
    monkeypatch.setattr(market_research.MarketResearchService, "refresh_isolated", lambda *args: {})
    monkeypatch.setattr(research_cycle, "capture_research_state", lambda *args: {"snapshots": 0})
    monkeypatch.setattr(action_evaluation, "evaluate_action_snapshots", lambda *args: {})
    try:
        coordinator._execute_research(run_id, [])
        with session_factory() as session:
            run = session.get(IngestionRun, run_id)
            assert run is not None and run.status == "completed"
    finally:
        coordinator.shutdown()


def test_isolated_refresh_allows_writes_during_fetch_and_keeps_successful_sources(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    with session_factory() as session:
        security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
        assert security is not None
        security_id = security.id
    calls: list[str] = []
    write_errors: list[Exception] = []

    def fetch(url: str) -> object:
        calls.append(url)
        try:
            with session_factory() as other:
                other.execute(text("PRAGMA busy_timeout=50"))
                target = other.get(Security, security_id)
                assert target is not None
                target.business_summary = f"User edit during request {len(calls)}"
                other.commit()
                if "submissions" in url:
                    assert other.scalar(select(func.count(ResearchFinancialFact.id))) == 1
        except Exception as exc:
            write_errors.append(exc)
            raise
        if "submissions" in url:
            raise TimeoutError("synthetic upstream failure")
        return fake_transport(url)

    report = ResearchDataService(settings, fetch).refresh_isolated(
        session_factory, [security_id], NOW,
    )

    assert not write_errors
    assert len(calls) >= 7
    states = {item["source"]: item["status"] for item in report["sources"]}
    assert states[f"sec_companyfacts:{security_id}"] == "available"
    assert states[f"sec_submissions:{security_id}"] == "degraded"
    assert states["bls_calendar"] == "available"
    with session_factory() as session:
        assert session.scalar(select(func.count(ResearchFinancialFact.id))) == 1
        assert session.scalar(select(func.count(MacroObservationVintage.id))) == 2


def test_isolated_source_save_failure_rolls_back_only_that_source(
    session_factory: SessionFactory,
) -> None:
    def save_good(session: Session) -> int:
        session.add(Security(market="US", exchange="TEST", symbol="GOOD", name="Good"))
        return 1

    def save_bad(session: Session) -> int:
        session.add(Security(market="US", exchange="TEST", symbol="BAD", name="Bad"))
        session.flush()
        raise ValueError("synthetic persistence failure")

    _run_source(session_factory, "good", None, "test", NOW, lambda: save_good)
    report = _run_source(session_factory, "bad", None, "test", NOW, lambda: save_bad)

    assert report["status"] == "degraded"
    with session_factory() as session:
        assert session.scalar(select(Security).where(Security.symbol == "GOOD")) is not None
        assert session.scalar(select(Security).where(Security.symbol == "BAD")) is None
        states = {
            row.source_key: row.status for row in session.scalars(select(ResearchSourceState))
        }
        assert states == {"good": "available", "bad": "degraded"}


def test_session_refresh_does_not_commit_manual_changes(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    with session_factory() as session:
        security = session.scalar(select(Security).where(Security.symbol == "AAPL"))
        assert security is not None
        original, security_id = security.name, security.id
        security.name = "Uncommitted manual change"
        ResearchDataService(settings, fake_transport).refresh(session, [security_id], NOW)
        session.rollback()
    with session_factory() as session:
        security = session.get(Security, security_id)
        assert security is not None and security.name == original
        assert session.scalar(select(func.count(ResearchFinancialFact.id))) == 0
        assert session.scalar(select(func.count(ResearchSourceState.id))) == 0


def test_failed_sec_directory_is_shared_within_run_and_retried_next_run(
    session_factory: SessionFactory, settings: Settings,
) -> None:
    directory_calls = 0
    with session_factory() as session:
        security_ids = list(session.scalars(
            select(Security.id).where(Security.symbol.in_(["AAPL", "MSFT"]))
        ))

    def fetch(url: str) -> object:
        nonlocal directory_calls
        if "company_tickers" in url:
            directory_calls += 1
            if directory_calls == 1:
                raise HTTPError(url, 403, "synthetic denial", Message(), None)
            return {
                "0": {"ticker": "AAPL", "cik_str": 320193},
                "1": {"ticker": "MSFT", "cik_str": 789019},
            }
        if "submissions" in url:
            return {"cik": url.rsplit("CIK", 1)[1].split(".", 1)[0], "filings": {"recent": {}}}
        return fake_transport(url)

    service = ResearchDataService(settings, fetch)
    first = service.refresh_isolated(session_factory, security_ids, NOW)
    assert directory_calls == 1
    assert all(item["status"] == "restricted" for item in first["sources"]
               if item["source"].startswith(("sec_companyfacts", "sec_submissions")))
    second = service.refresh_isolated(session_factory, security_ids, NOW + timedelta(minutes=31))
    assert directory_calls == 2
    assert all(item["status"] == "available" for item in second["sources"]
               if item["source"].startswith("sec_submissions"))
