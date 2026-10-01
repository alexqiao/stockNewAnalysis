"""Pure SEC parsers that preserve reporting intervals and availability timestamps."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
CONCEPTS = {
    "us-gaap": {
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "OperatingIncomeLoss",
        "EarningsPerShareBasic",
        "EarningsPerShareDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "CommonStockSharesOutstanding",
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "Assets",
        "AssetsCurrent",
        "Liabilities",
        "LiabilitiesCurrent",
        "LiabilitiesAndStockholdersEquity",
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "LongTermDebt",
        "LongTermDebtCurrent",
        "LongTermDebtNoncurrent",
        "ShortTermBorrowings",
    },
    "dei": {"EntityCommonStockSharesOutstanding"},
    "ifrs-full": {
        "Revenue",
        "ProfitLoss",
        "ProfitLossAttributableToOwnersOfParent",
        "BasicEarningsLossPerShare",
        "DilutedEarningsLossPerShare",
        "CashAndCashEquivalents",
        "CashFlowsFromUsedInOperatingActivities",
        "Assets",
        "CurrentAssets",
        "Liabilities",
        "CurrentLiabilities",
        "Equity",
        "NoncurrentLiabilities",
        "Borrowings",
        "CurrentBorrowings",
        "NoncurrentBorrowings",
    },
}
DISCLOSURE_FORMS = {"10-K", "10-Q", "8-K", "6-K", "20-F"}


def _context(payload: dict[str, Any], cik: int, now: datetime) -> datetime:
    if isinstance(cik, bool) or not isinstance(cik, int) or not 0 < cik < 10**10:
        raise ValueError("SEC CIK 必须为有效的正整数")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("SEC 观察时间必须包含时区")
    if "cik" in payload:
        payload_cik = payload["cik"]
        try:
            valid_type = (
                isinstance(payload_cik, int)
                and not isinstance(payload_cik, bool)
                or isinstance(payload_cik, str)
                and payload_cik.isascii()
                and payload_cik.isdigit()
            )
            matches = valid_type and int(payload_cik) == cik
        except (ValueError, TypeError, OverflowError):
            matches = False
        if not matches:
            raise ValueError("SEC 返回的 CIK 与请求证券不一致")
    return now.astimezone(UTC)


def _date(value: Any) -> date | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _filed_available(value: Any) -> datetime | None:
    filed = _date(value)
    if filed is None:
        return None
    try:
        return datetime.combine(filed + timedelta(days=1), time.min, NEW_YORK).astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def _accepted(value: Any) -> datetime | None:
    if not isinstance(value, str) or "T" not in value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _archive_index(cik: int, accession: str) -> str:
    directory = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{directory}/{accession}-index.html"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _recent_fiscal_window(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    annual_ends = [
        row["period_end"]
        for row in rows
        if row["period_start"] is not None
        and 330 <= (row["period_end"] - row["period_start"]).days + 1 <= 400
        and row["form"].removesuffix("/A") in {"10-K", "20-F", "40-F"}
    ]
    anchor = max(annual_ends) if annual_ends else None

    def financial_year(end: date) -> int:
        if anchor is None:
            return end.year
        # 52/53-week year ends can move a few days across calendar months.
        boundary = date(end.year, anchor.month, min(anchor.day, 28))
        boundary += timedelta(days=anchor.day - min(anchor.day, 28) + 7)
        return end.year if end <= boundary else end.year + 1

    latest = max(financial_year(row["period_end"]) for row in rows)
    return [row for row in rows if financial_year(row["period_end"]) >= latest - 2]


def parse_company_facts(
    payload: dict[str, Any],
    cik: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """Keep all accession/value versions for the latest three actual reporting years.

    FY/FP describe the containing filing, not necessarily a fact's comparative
    period. Period labels therefore encode observed duration, never an inferred quarter.
    """
    observed = _context(payload, cik, now)
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return []
    rows: dict[str, dict[str, Any]] = {}
    for taxonomy, allowed in CONCEPTS.items():
        concepts = facts.get(taxonomy)
        if not isinstance(concepts, dict):
            continue
        for concept in sorted(allowed):
            content = concepts.get(concept)
            if not isinstance(content, dict):
                continue
            units = content.get("units")
            if not isinstance(units, dict):
                continue
            for unit, entries in units.items():
                if (
                    not isinstance(unit, str)
                    or not 0 < len(unit) <= 40
                    or not isinstance(entries, list)
                ):
                    continue
                for raw in entries:
                    if not isinstance(raw, dict):
                        continue
                    value = _number(raw.get("val"))
                    end = _date(raw.get("end"))
                    start = _date(raw.get("start"))
                    filed = _date(raw.get("filed"))
                    available = _filed_available(raw.get("filed"))
                    accession = raw.get("accn")
                    form = raw.get("form")
                    if (
                        value is None
                        or end is None
                        or filed is None
                        or available is None
                        or available > observed
                        or end > filed
                        or ("start" in raw and start is None)
                        or (start is not None and start > end)
                        or not isinstance(accession, str)
                        or not ACCESSION.fullmatch(accession)
                        or not isinstance(form, str)
                        or not 0 < len(form) <= 24
                    ):
                        continue
                    fiscal_period = f"duration_{(end - start).days + 1}d" if start else "instant"
                    identity = [
                        cik,
                        taxonomy,
                        concept,
                        unit,
                        start,
                        end,
                        accession,
                        filed,
                        form,
                        value,
                    ]
                    fingerprint = _fingerprint(identity)
                    rows[fingerprint] = {
                        "fingerprint": fingerprint,
                        "source": "sec_companyfacts",
                        "source_url": _archive_index(cik, accession),
                        "concept": f"{taxonomy}:{concept}",
                        "value": value,
                        "unit": unit,
                        "period_start": start,
                        "period_end": end,
                        "fiscal_period": fiscal_period,
                        "accession": accession,
                        "form": form,
                        "available_at": available,
                        "observed_at": observed,
                        "raw_data": {
                            "sec_fact": dict(raw),
                            "label": content.get("label", ""),
                            "time_precision": "filing_date",
                            "cik": cik,
                        },
                    }
    kept = _recent_fiscal_window(list(rows.values()))
    return sorted(
        kept,
        key=lambda row: (
            row["period_end"],
            row["concept"],
            row["available_at"],
            row["accession"],
            row["fingerprint"],
        ),
        reverse=True,
    )


def _recent_column(recent: dict[str, Any], name: str, index: int) -> Any:
    column = recent.get(name)
    return column[index] if isinstance(column, list) and index < len(column) else None


def _document_url(cik: int, accession: str, document: Any) -> str | None:
    if not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", document):
        return None
    if any(part in {"", ".", ".."} for part in document.split("/")):
        return None
    directory = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{directory}/{quote(document, safe='/._-')}"
    )


def parse_sec_disclosures(
    payload: dict[str, Any],
    cik: int,
    now: datetime,
) -> list[dict[str, Any]]:
    """Return material recent filings as links; metadata must be removed before ORM creation."""
    observed = _context(payload, cik, now)
    filings = payload.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    forms = recent.get("form") if isinstance(recent, dict) else None
    if not isinstance(recent, dict) or not isinstance(forms, list):
        return []
    cutoff = observed - timedelta(days=365)
    entity_name = str(payload.get("name") or f"CIK {cik}")
    result: dict[str, dict[str, Any]] = {}
    for index, form in enumerate(forms):
        if not isinstance(form, str) or form.removesuffix("/A") not in DISCLOSURE_FORMS:
            continue
        accession = _recent_column(recent, "accessionNumber", index)
        if not isinstance(accession, str) or not ACCESSION.fullmatch(accession):
            continue
        document = _recent_column(recent, "primaryDocument", index)
        source_url = _document_url(cik, accession, document)
        if source_url is None:
            continue
        accepted = _accepted(_recent_column(recent, "acceptanceDateTime", index))
        filed = _recent_column(recent, "filingDate", index)
        published = accepted or _filed_available(filed)
        if published is None or published > observed or published < cutoff:
            continue
        report_date = _date(_recent_column(recent, "reportDate", index))
        title_date = report_date or _date(filed) or published.astimezone(NEW_YORK).date()
        title = f"{entity_name} · {form} · {title_date.isoformat()}"
        fingerprint = _fingerprint([cik, accession, form, source_url, published])
        result[fingerprint] = {
            "fingerprint": fingerprint,
            "source": "sec_submissions",
            "source_url": source_url,
            "title": title,
            "accession": accession,
            "form": form,
            "excerpt": "",
            "content_status": "link_only",
            "published_at": published,
            "available_at": published,
            "observed_at": observed,
            "metadata": {
                "archive_index_url": _archive_index(cik, accession),
                "archive_index_json_url": (
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                    f"{accession.replace('-', '')}/index.json"
                ),
                "primary_document": document,
                "filing_date": filed,
                "report_date": report_date.isoformat() if report_date else None,
                "time_precision": "acceptance_datetime" if accepted else "filing_date",
                "cik": cik,
            },
        }
    return sorted(
        result.values(), key=lambda row: (row["published_at"], row["accession"]), reverse=True
    )
