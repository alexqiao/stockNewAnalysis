from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from trade_news_analysis.research_data_models import ResearchDisclosure, ResearchFinancialFact
from trade_news_analysis.services.sec_research import parse_company_facts, parse_sec_disclosures

CIK = 320193
NOW = datetime(2026, 9, 17, 12, tzinfo=UTC)
ACCESSION = "0000320193-26-000001"


def fact(**changes: Any) -> dict[str, Any]:
    return {
        "val": 100,
        "start": "2025-10-01",
        "end": "2026-06-30",
        "filed": "2026-08-01",
        "accn": ACCESSION,
        "form": "10-Q",
        "fy": 2026,
        "fp": "Q3",
        **changes,
    }


def company(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cik": CIK,
        "facts": {"us-gaap": {"Revenues": {"label": "Revenue", "units": {"USD": rows}}}},
    }


def submission(**changes: Any) -> dict[str, Any]:
    return {
        "accessionNumber": ACCESSION,
        "filingDate": "2026-08-01",
        "acceptanceDateTime": "2026-08-01T20:31:00.000Z",
        "reportDate": "2026-06-30",
        "form": "10-Q",
        "primaryDocument": "aapl-20260630.htm",
        **changes,
    }


def submissions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    columns = {key: [row.get(key) for row in rows] for key in submission()}
    return {"cik": "0000320193", "name": "Apple Inc.", "filings": {"recent": columns}}


def test_facts_preserve_actual_intervals_versions_zero_and_stable_identity() -> None:
    rows = [
        fact(val=0),
        fact(val=0, frame="CY2026Q2"),
        fact(val=-10),
        fact(val=5, accn="0000320193-26-000002", form="10-Q/A", filed="2026-08-02"),
    ]
    result = parse_company_facts(company(rows), CIK, NOW)
    assert len(result) == 3
    assert {row["value"] for row in result} == {0, -10, 5}
    assert {row["fiscal_period"] for row in result} == {"duration_273d"}
    assert all(row["period_start"] == date(2025, 10, 1) for row in result)
    assert all(row["raw_data"]["sec_fact"]["fp"] == "Q3" for row in result)
    assert all(row["concept"] == "us-gaap:Revenues" for row in result)
    assert all(row["observed_at"] == NOW for row in result)
    assert {row["fingerprint"] for row in result} == {
        row["fingerprint"]
        for row in parse_company_facts(company(rows), CIK, NOW + timedelta(days=1))
    }
    for row in result:
        assert ResearchFinancialFact(security_id=1, **row).source == "sec_companyfacts"
        assert row["source_url"].endswith(f"/{row['accession']}-index.html")


@pytest.mark.parametrize(
    "invalid", [True, False, "12", None, float("inf"), float("-inf"), float("nan"), 10**400]
)
def test_nonfinite_or_non_numeric_facts_are_rejected(invalid: Any) -> None:
    assert parse_company_facts(company([fact(val=invalid)]), CIK, NOW) == []


def test_only_selected_standard_concepts_are_kept() -> None:
    instant = fact()
    instant.pop("start")
    payload = company([fact()])
    payload["facts"]["us-gaap"]["NotAStandardRevenue"] = {"units": {"USD": [fact()]}}
    payload["facts"]["custom"] = {"Revenues": {"units": {"USD": [fact()]}}}
    payload["facts"]["dei"] = {
        "EntityCommonStockSharesOutstanding": {"units": {"shares": [instant]}}
    }
    payload["facts"]["ifrs-full"] = {"CashAndCashEquivalents": {"units": {"USD": [instant]}}}
    result = parse_company_facts(payload, CIK, NOW)
    assert {row["concept"] for row in result} == {
        "us-gaap:Revenues",
        "dei:EntityCommonStockSharesOutstanding",
        "ifrs-full:CashAndCashEquivalents",
    }
    assert len([row for row in result if row["fiscal_period"] == "instant"]) == 2


def test_latest_three_fiscal_years_use_period_dates_not_containing_filing_fy() -> None:
    rows = [
        fact(start=f"{year - 1}-10-01", end=f"{year}-09-30", form="10-K", fy=2026, fp="FY")
        for year in range(2022, 2026)
    ]
    rows += [fact(), fact(start="2023-10-01", end="2023-12-31", fy=2026)]
    result = parse_company_facts(company(rows), CIK, NOW)
    assert {row["period_end"] for row in result} == {
        date(2024, 9, 30),
        date(2025, 9, 30),
        date(2026, 6, 30),
        date(2023, 12, 31),
    }


@pytest.mark.parametrize(
    ("filed", "available"),
    [
        ("2026-01-15", datetime(2026, 1, 16, 5, tzinfo=UTC)),
        ("2026-07-15", datetime(2026, 7, 16, 4, tzinfo=UTC)),
    ],
)
def test_filing_date_only_becomes_available_next_new_york_midnight(
    filed: str,
    available: datetime,
) -> None:
    payload = company([fact(start="2025-01-01", end="2025-12-31", filed=filed)])
    assert parse_company_facts(payload, CIK, available - timedelta(microseconds=1)) == []
    result = parse_company_facts(payload, CIK, available)
    assert result[0]["available_at"] == available


@pytest.mark.parametrize(
    "changes",
    [
        {"end": "2026-08-02"},
        {"start": "2026-07-01"},
        {"start": None},
        {"filed": "2026-02-30"},
        {"end": "2026-2-01"},
        {"accn": "../escape"},
        {"form": None},
    ],
)
def test_invalid_periods_and_filing_identifiers_are_skipped(changes: dict[str, Any]) -> None:
    assert parse_company_facts(company([fact(**changes)]), CIK, NOW) == []


def test_instant_only_facts_use_recent_period_years_and_ignore_malformed_units() -> None:
    rows = []
    for year in range(2022, 2027):
        row = fact(end=f"{year}-06-30", fy=2026)
        row.pop("start")
        rows.append(row)
    payload = company(rows)
    payload["facts"]["us-gaap"]["Revenues"]["units"].update({"bad": None, "": [fact()]})
    result = parse_company_facts(payload, CIK, NOW)
    assert {row["period_end"].year for row in result} == {2024, 2025, 2026}


def test_disclosures_keep_relevant_forms_and_complete_source_metadata() -> None:
    forms = ["10-K", "10-Q", "8-K", "6-K", "20-F", "10-Q/A", "4", "S-8"]
    payload = submissions(
        [
            submission(form=form, accessionNumber=f"0000320193-26-{index:06d}")
            for index, form in enumerate(forms)
        ]
    )
    result = parse_sec_disclosures(payload, CIK, NOW)
    assert {row["form"] for row in result} == set(forms[:6])
    for row in result:
        assert row["published_at"] == datetime(2026, 8, 1, 20, 31, tzinfo=UTC)
        assert row["available_at"] == row["published_at"]
        assert row["observed_at"] == NOW
        assert row["title"] == f"Apple Inc. · {row['form']} · 2026-06-30"
        assert row["excerpt"] == ""
        assert row["content_status"] == "link_only"
        directory = row["accession"].replace("-", "")
        prefix = f"https://www.sec.gov/Archives/edgar/data/320193/{directory}/"
        assert row["source_url"] == prefix + "aapl-20260630.htm"
        assert row["metadata"]["archive_index_url"] == prefix + row["accession"] + "-index.html"
        assert row["metadata"]["archive_index_json_url"] == prefix + "index.json"
        orm_fields = {key: value for key, value in row.items() if key != "metadata"}
        assert ResearchDisclosure(security_id=1, **orm_fields).source == "sec_submissions"


@pytest.mark.parametrize("accepted", [None, "", "2026-08-01T09:00:00", "invalid"])
def test_disclosure_missing_exact_time_is_conservative(accepted: Any) -> None:
    payload = submissions([submission(acceptanceDateTime=accepted)])
    available = datetime(2026, 8, 2, 4, tzinfo=UTC)
    assert parse_sec_disclosures(payload, CIK, available - timedelta(microseconds=1)) == []
    result = parse_sec_disclosures(payload, CIK, available)
    assert result[0]["published_at"] == available
    assert result[0]["metadata"]["time_precision"] == "filing_date"


def test_disclosures_filter_old_and_future_without_fallback_from_future_acceptance() -> None:
    cutoff = NOW - timedelta(days=365)
    payload = submissions(
        [
            submission(
                acceptanceDateTime=value.isoformat(), accessionNumber=f"0000320193-26-{i:06d}"
            )
            for i, value in enumerate(
                [cutoff - timedelta(seconds=1), cutoff, NOW, NOW + timedelta(seconds=1)]
            )
        ]
    )
    result = parse_sec_disclosures(payload, CIK, NOW)
    assert {row["published_at"] for row in result} == {cutoff, NOW}


def test_disclosure_identity_is_stable_and_duplicates_are_collapsed() -> None:
    row = submission(acceptanceDateTime="2026-08-01T16:31:00-04:00")
    payload = submissions([row, row])
    result = parse_sec_disclosures(payload, CIK, NOW)
    assert len(result) == 1
    assert result[0]["published_at"] == datetime(2026, 8, 1, 20, 31, tzinfo=UTC)
    again = parse_sec_disclosures(payload, CIK, NOW + timedelta(days=1))
    assert again[0]["fingerprint"] == result[0]["fingerprint"]


@pytest.mark.parametrize(
    "document",
    [
        "../secret.htm",
        "/absolute.htm",
        "a//b.htm",
        "a/./b.htm",
        "https://example.com/document",
        "evil?redirect=x",
        "",
        None,
    ],
)
def test_disclosure_links_cannot_escape_archive(document: Any) -> None:
    assert (
        parse_sec_disclosures(submissions([submission(primaryDocument=document)]), CIK, NOW) == []
    )


def test_short_parallel_columns_and_malformed_payloads_are_safe() -> None:
    payload = submissions([submission(), submission(accessionNumber="0000320193-26-000002")])
    payload["filings"]["recent"]["primaryDocument"].pop()
    assert len(parse_sec_disclosures(payload, CIK, NOW)) == 1
    malformed_payloads: list[dict[str, Any]] = [
        {},
        {"facts": []},
        {"filings": []},
        {"filings": {"recent": []}},
    ]
    for malformed in malformed_payloads:
        assert parse_company_facts(malformed, CIK, NOW) == []
        assert parse_sec_disclosures(malformed, CIK, NOW) == []


@pytest.mark.parametrize("parser", [parse_company_facts, parse_sec_disclosures])
def test_issuer_and_observation_context_are_validated(parser: Any) -> None:
    with pytest.raises(ValueError, match="CIK"):
        parser({"cik": 123}, CIK, NOW)
    with pytest.raises(ValueError, match="CIK"):
        parser({"cik": CIK + 0.5}, CIK, NOW)
    with pytest.raises(ValueError, match="CIK"):
        parser({}, True, NOW)
    with pytest.raises(ValueError, match="时区"):
        parser({}, CIK, NOW.replace(tzinfo=None))
