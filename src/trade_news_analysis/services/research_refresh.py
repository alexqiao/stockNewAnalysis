"""Bounded refresh of existing providers and public official research sources."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db import SessionFactory
from ..decision_models import MacroObservationVintage
from ..models import Security, Watchlist
from ..research_data_models import (
    ResearchCalendarRevision,
    ResearchDisclosure,
    ResearchExpectation,
    ResearchFinancialFact,
    ResearchSourceState,
)
from .market_research import number, utc
from .providers import TushareClient
from .research_data import upsert_calendar

ALLOWED_HOSTS = {
    "www.sec.gov",
    "data.sec.gov",
    "finnhub.io",
    "www.bls.gov",
    "www.federalreserve.gov",
    "api.bls.gov",
}

ResearchStore = Session | SessionFactory
SourceWriter = Callable[[Session], int]


@contextmanager
def _research_session(store: ResearchStore, *, write: bool = False) -> Iterator[Session]:
    if isinstance(store, Session):
        # Session-based callers retain ownership of their transaction.
        yield store
    else:
        with store() as session:
            yield session
            if write:
                session.commit()


class _SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        if (
            urlsplit(newurl).hostname != urlsplit(req.full_url).hostname
            or urlsplit(newurl).scheme != "https"
        ):
            raise ValueError("来源重定向超出允许的官方地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ResearchTransport:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.opener = build_opener(_SameHostRedirect())
        self.last_sec_call = 0.0

    def __call__(self, url: str) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("未知研究数据端点")
        if parsed.hostname.endswith("sec.gov"):
            time.sleep(max(0, 0.25 - (time.monotonic() - self.last_sec_call)))
            self.last_sec_call = time.monotonic()
        headers = {"User-Agent": self.settings.http_user_agent}
        if parsed.hostname == "finnhub.io":
            query = parse_qsl(parsed.query, keep_blank_values=True)
            tokens = [value for key, value in query if key == "token"]
            if tokens:
                headers["X-Finnhub-Token"] = tokens[-1]
            url = urlunsplit(parsed._replace(
                query=urlencode([(key, value) for key, value in query if key != "token"])
            ))
        request = Request(url, headers=headers)
        with self.opener.open(
            request, timeout=min(self.settings.request_timeout_seconds, 20)
        ) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError("响应超出研究抓取大小上限")
        text = raw.decode("utf-8", errors="replace")
        return json.loads(text) if text.lstrip().startswith(("{", "[")) else text


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def save_macro_vintages(
    session: Session, payload: Any, series: str, unit: str, now: datetime
) -> int:
    if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS 实测指标不可用")
    count = 0
    for item in (payload.get("Results") or {}).get("series") or []:
        if item.get("seriesID") != series:
            continue
        for row in item.get("data") or []:
            if not re.fullmatch(r"M(?:0[1-9]|1[0-2])", str(row.get("period"))):
                continue
            value = number(row.get("value"))
            if value is None:
                continue
            period = f"{row.get('year')}-{row['period'][1:]}"
            previous = session.scalar(
                select(MacroObservationVintage)
                .where(
                    MacroObservationVintage.series == series,
                    MacroObservationVintage.period == period,
                )
                .order_by(MacroObservationVintage.id.desc())
                .limit(1)
            )
            if previous is not None and previous.value == value:
                continue
            values = {
                "fingerprint": _hash([series, period, value, previous.id if previous else None]),
                "series": series,
                "period": period,
                "value": value,
                "unit": unit,
                "source_url": f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series}",
                "available_at": now,
                "observed_at": now,
                "metadata_values": {
                    "raw": row,
                    "previous_value": previous.value if previous else None,
                    "version_note": "采集后观察到修订"
                    if previous
                    else "首次采集值，非历史发布初值",
                },
            }
            count += _save_unique(session, MacroObservationVintage, values)
    return count


def _date(value: Any) -> date | None:
    try:
        text = str(value)
        return (
            datetime.strptime(text, "%Y%m%d").date()
            if len(text) == 8
            else date.fromisoformat(text[:10])
        )
    except ValueError:
        return None


def _save_unique(session: Session, model: Any, values: dict[str, Any]) -> bool:
    if session.scalar(select(model.id).where(model.fingerprint == values["fingerprint"])):
        return False
    session.add(model(**values))
    session.flush()
    return True


def _source_failure(key: str, exc: Exception) -> tuple[str, str]:
    # Never store the exception URL/body: provider responses may echo credentials.
    if isinstance(exc, HTTPError):
        if exc.code in {401, 403}:
            if key.startswith("finnhub"):
                reason = (
                    "凭据未通过认证，请核对 Finnhub API Key"
                    if exc.code == 401 else "该接口拒绝访问，请核对当前 Finnhub 凭据的接口权限"
                )
            elif key.startswith("bls_"):
                reason = "BLS 官方站点拒绝当前请求；需核对网络出口或站点访问限制"
            else:
                reason = "来源拒绝当前请求，请核对访问权限及网络出口"
            return "restricted", f"HTTP {exc.code}：{reason}。已保存资料保留，其他来源继续刷新。"
        if exc.code == 429:
            return "degraded", "HTTP 429：来源请求频率受限，稍后重试；已保存资料保留。"
        return "degraded", f"HTTP {exc.code}：来源暂未返回可用数据；已保存资料保留，稍后重试。"
    if isinstance(exc, TimeoutError):
        return "degraded", "连接来源超时，请检查网络连接后重试；已保存资料保留。"
    return "degraded", f"{type(exc).__name__}：本次刷新失败；已保存资料保留，请稍后重试。"


def _run_source(
    store: ResearchStore,
    key: str,
    security_id: int | None,
    capability: str,
    now: datetime,
    prepare: Callable[[], SourceWriter] | None,
    missing: str = "",
) -> dict[str, Any]:
    query = select(ResearchSourceState).where(ResearchSourceState.source_key == key)
    with _research_session(store) as session:
        state = session.scalar(query)
        if state is not None and state.expires_at and utc(state.expires_at) > now:
            return {"source": key, "status": state.status, "cached": True}

    save: SourceWriter | None = None
    failure: Exception | None = None
    if prepare is not None:
        try:
            save = prepare()
        except Exception as exc:
            failure = exc

    with _research_session(store, write=True) as session:
        state = session.scalar(query)
        if state is None:
            state = ResearchSourceState(
                source_key=key, security_id=security_id, capability=capability
            )
            session.add(state)
        state.last_attempt_at = now
        if prepare is None:
            state.status, state.message = "missing", missing
            state.coverage = "unavailable"
            state.expires_at = now + timedelta(hours=6)
        elif save is not None:
            try:
                with session.begin_nested():
                    count = save(session)
                state.status, state.items_last_run = "available", count
                state.last_success_at = now
                state.coverage = "partial"
                state.message = f"本次新增或核对 {count} 条；仅代表接口返回范围"
                state.expires_at = now + timedelta(hours=6)
            except Exception as exc:
                failure = exc
        if failure is not None:
            state.status, state.message = _source_failure(key, failure)
            if state.last_success_at is None:
                state.coverage = "unavailable"
            state.expires_at = now + timedelta(minutes=30)
        session.flush()
        report = {"source": key, "status": state.status, "cached": False}
    return report


def _save_sec_rows(
    session: Session, security_id: int, cik: int, model: Any, rows: list[dict[str, Any]]
) -> int:
    security = session.get(Security, security_id)
    if security is None:
        raise ValueError("证券已不存在，无法保存研究资料")
    security.provider_data = {**(security.provider_data or {}), "sec_cik": cik}
    return sum(_save_unique(session, model, {**row, "security_id": security_id}) for row in rows)


def _sec_facts(security: Security, cik: int, fetch: Any, now: datetime) -> SourceWriter:
    from .sec_research import parse_company_facts

    payload = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
    rows = parse_company_facts(payload, cik, now)
    if not rows:
        raise ValueError("没有可验证的标准财务事实")
    return partial(
        _save_sec_rows, security_id=security.id, cik=cik, model=ResearchFinancialFact, rows=rows
    )


def _sec_disclosures(
    security: Security, cik: int, fetch: Any, now: datetime
) -> SourceWriter:
    from .sec_research import parse_sec_disclosures

    payload = fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    rows = parse_sec_disclosures(payload, cik, now)
    return partial(
        _save_sec_rows, security_id=security.id, cik=cik, model=ResearchDisclosure,
        rows=[{key: value for key, value in row.items() if key != "metadata"} for row in rows],
    )


def _sec_attachments(
    store: ResearchStore, security: Security, fetch: Any, now: datetime
) -> SourceWriter:
    with _research_session(store) as session:
        filings = session.scalars(
            select(ResearchDisclosure)
            .where(
                ResearchDisclosure.security_id == security.id,
                ResearchDisclosure.form.in_(["8-K", "6-K"]),
                ResearchDisclosure.source == "sec_submissions",
            )
            .order_by(ResearchDisclosure.published_at.desc())
            .limit(2)
        ).all()
        if not filings:
            upstream = session.scalar(
                select(ResearchSourceState).where(
                    ResearchSourceState.source_key == f"sec_submissions:{security.id}"
                )
            )
            if upstream is None or upstream.status != "available":
                raise ValueError("尚未获得可用的 SEC 公告目录，无法检查附件")
    rows = []
    for filing in filings:
        directory = filing.source_url.rsplit("/", 1)[0] + "/"
        index_url = directory + filing.accession + "-index.html"
        document = BeautifulSoup(str(fetch(index_url)), "html.parser")
        attachments = []
        for row in document.select("table.tableFile tr"):
            cells = row.find_all("td")
            anchor = cells[2].find("a") if len(cells) >= 4 else None
            if anchor is None:
                continue
            description = cells[1].get_text(" ", strip=True)
            kind = cells[3].get_text(" ", strip=True)
            if not (
                kind.startswith("EX-99")
                or re.search(r"earnings|results|financial", description, re.I)
            ):
                continue
            link = urljoin(index_url, str(anchor.get("href", "")))
            if not link.startswith(directory) or not link.lower().endswith((".htm", ".html")):
                continue
            attachments.append((link, description))
        for url, description in attachments[:2]:
            page = BeautifulSoup(str(fetch(url)), "html.parser")
            for item in page(["script", "style"]):
                item.decompose()
            excerpt = page.get_text(" ", strip=True)[:16000]
            values = {
                "security_id": security.id,
                "source": "sec_attachment",
                "source_url": url,
                "title": description or f"{security.symbol} 财报附件",
                "form": filing.form,
                "accession": filing.accession,
                "excerpt": excerpt,
                "content_status": "excerpt",
                "published_at": filing.published_at,
                "available_at": filing.available_at,
                "observed_at": now,
            }
            values["fingerprint"] = _hash([url, excerpt])
            rows.append(values)
    return lambda session: sum(_save_unique(session, ResearchDisclosure, row) for row in rows)


def _finnhub(
    session: Session,
    security: Security,
    fetch: Any,
    settings: Settings,
    now: datetime,
    *,
    dividends: bool = False,
) -> int:
    return _prepare_finnhub(security, fetch, settings, now, dividends=dividends)(session)


def _prepare_finnhub(
    security: Security,
    fetch: Any,
    settings: Settings,
    now: datetime,
    *,
    dividends: bool = False,
) -> SourceWriter:
    assert settings.finnhub_api_key is not None
    parameters = {
        "symbol": security.symbol,
        "from": (now.date() - timedelta(days=30)).isoformat(),
        "to": (now.date() + timedelta(days=120)).isoformat(),
        "token": settings.finnhub_api_key.get_secret_value(),
    }
    path = "stock/dividend" if dividends else "calendar/earnings"
    payload = fetch(f"https://finnhub.io/api/v1/{path}?{urlencode(parameters)}")
    if isinstance(payload, dict) and payload.get("error"):
        raise ValueError("Finnhub 权限或请求失败")
    rows = (
        payload
        if dividends
        else payload.get("earningsCalendar")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list):
        raise ValueError("Finnhub 返回格式异常")
    return partial(
        _save_finnhub, security=security, rows=rows, path=path, now=now, dividends=dividends
    )


def _save_finnhub(
    session: Session, security: Security, rows: list[Any], path: str, now: datetime,
    *, dividends: bool,
) -> int:
    count = 0
    for item in rows:
        day = _date(item.get("date"))
        if day is None:
            continue
        period = (
            f"{item.get('year')}Q{item.get('quarter')}"
            if item.get("year") and item.get("quarter")
            else ""
        )
        key = f"finnhub:{security.id}:{path}:{period or day.isoformat()}"
        latest = session.scalar(
            select(ResearchCalendarRevision)
            .where(ResearchCalendarRevision.event_key == key)
            .order_by(ResearchCalendarRevision.revision.desc())
            .limit(1)
        )
        if latest is not None and latest.source == "manual_official":
            # A provider refresh must not undo an explicitly checked official revision.
            count += 1
            continue
        record = upsert_calendar(
            session,
            security_id=security.id,
            event_key=key,
            title=f"{security.symbol} {'除息' if dividends else '财报'}",
            event_type="dividend" if dividends else "earnings",
            scheduled_date=day,
            scheduled_at=None,
            timezone=security.timezone,
            status="provider_reported",
            source="finnhub",
            source_url="https://finnhub.io/docs/api/stock-dividends"
            if dividends
            else "https://finnhub.io/docs/api/earnings-calendar",
            details={
                **item,
                "time_note": "仅保留提供方盘前/盘后标记，不推定具体分钟",
                "estimate_note": "Finnhub 提供方预期，不等同纯市场一致预期",
                "unit_note": "接口未提供可核验报告币种时单位未知；报价币种不能代替财报币种",
            },
            now=now,
        )
        count += 1
        if dividends:
            continue
        release_start = datetime.combine(day, datetime.min.time(), ZoneInfo(security.timezone))
        for estimate_key, metric, unit in (
            ("epsEstimate", "eps", "unknown"),
            ("revenueEstimate", "revenue", "unknown"),
        ):
            value = number(item.get(estimate_key))
            if value is None:
                continue
            values = {
                "security_id": security.id,
                "event_key": key,
                "calendar_revision_id": record.id,
                "metric": metric,
                "value": value,
                "unit": unit,
                "source": "finnhub",
                "source_url": "https://finnhub.io/docs/api/earnings-calendar",
                "estimate_kind": "provider_estimate",
                "is_pre_release": now < release_start
                and all(item.get(key) is None for key in ("epsActual", "revenueActual")),
                "available_at": now,
                "observed_at": now,
                "financial_period": period,
                "expected_at": None,
            }
            values["fingerprint"] = _hash([key, record.id, metric, value, unit])
            _save_unique(session, ResearchExpectation, values)
    return count


def _prepare_tushare(
    security: Security,
    settings: Settings,
    now: datetime,
    capability: str,
) -> SourceWriter:
    assert settings.tushare_token is not None
    client = TushareClient(
        settings.tushare_token.get_secret_value(), settings.request_timeout_seconds
    )
    rows = client.query(capability, {"ts_code": security.symbol})
    return partial(_save_tushare, security=security, rows=rows, now=now, capability=capability)


def _save_tushare(
    session: Session, security: Security, rows: list[dict[str, Any]], now: datetime, capability: str
) -> int:
    count = 0
    for item in rows:
        if capability == "fina_indicator":
            end, filed = _date(item.get("end_date")), _date(item.get("ann_date"))
            if end is None or filed is None or end < now.date() - timedelta(days=1100):
                continue
            for metric in ("eps", "roe", "netprofit_yoy", "or_yoy", "netprofit_margin"):
                value = number(item.get(metric))
                if value is None:
                    continue
                values: dict[str, Any] = {
                    "security_id": security.id,
                    "source": "tushare_fina_indicator",
                    "source_url": "https://tushare.pro/document/2?doc_id=79",
                    "concept": f"tushare:{metric}",
                    "value": value,
                    "unit": "CNY/share" if metric == "eps" else "percent",
                    "period_start": None,
                    "period_end": end,
                    "fiscal_period": "provider_reported",
                    "form": "financial_indicator",
                    "accession": f"{security.symbol}:{filed}:{end}",
                    "available_at": datetime.combine(
                        filed + timedelta(days=1), datetime.min.time(), ZoneInfo("Asia/Shanghai")
                    ),
                    "observed_at": now,
                    "raw_data": item,
                }
                if values["available_at"] > now:
                    continue
                values["fingerprint"] = _hash([security.id, metric, value, end, filed])
                count += _save_unique(session, ResearchFinancialFact, values)
            continue
        date_field = "ex_date" if capability == "dividend" else "actual_date"
        day = _date(item.get(date_field))
        confirmed = day is not None
        day = day or _date(item.get("pre_date"))
        if day is None or day < now.date() - timedelta(days=40):
            continue
        period = str(item.get("end_date") or item.get("ann_date") or day)
        upsert_calendar(
            session,
            security_id=security.id,
            event_key=f"tushare:{security.id}:{capability}:{period}",
            title=f"{security.symbol} {'除息' if capability == 'dividend' else '财报披露'}",
            event_type="dividend" if capability == "dividend" else "earnings",
            scheduled_date=day,
            scheduled_at=None,
            timezone="Asia/Shanghai",
            status="confirmed" if confirmed else "estimated",
            source="tushare",
            source_url="https://tushare.pro/document/2?doc_id="
            + ("103" if capability == "dividend" else "162"),
            details=item,
            now=now,
        )
        count += 1
    return count


def refresh_research_data(
    session: ResearchStore,
    settings: Settings,
    security_ids: list[int] | None = None,
    now: datetime | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """Refresh sources; a factory isolates writes, while a Session keeps caller ownership."""
    from .official_calendars import BLS_URL, FOMC_URL, parse_bls_calendar, parse_fomc_calendar

    current = utc(now or datetime.now(UTC))
    fetch = transport or ResearchTransport(settings)
    query = select(Security).join(Watchlist).where(Watchlist.active.is_(True))
    if security_ids is not None:
        query = select(Security).where(Security.id.in_(security_ids))
    with _research_session(session) as reader:
        securities = list(reader.scalars(query))
    reports: list[dict[str, Any]] = []
    directory: dict[str, int] | Exception | None = None

    def cik_for(security: Security) -> int:
        nonlocal directory
        configured = (security.provider_data or {}).get("sec_cik") or (
            security.provider_data or {}
        ).get("cik")
        if configured and str(configured).isdigit():
            return int(configured)
        if directory is None:
            try:
                payload = fetch("https://www.sec.gov/files/company_tickers.json")
                if not isinstance(payload, dict):
                    raise ValueError("SEC 证券目录格式异常")
                directory = {
                    str(row["ticker"]).upper(): int(row["cik_str"])
                    for row in payload.values()
                    if isinstance(row, dict) and row.get("ticker") and row.get("cik_str")
                }
            except Exception as exc:
                directory = exc
        if isinstance(directory, Exception):
            raise directory
        cik = directory.get(security.symbol.upper().replace(".", "-"))
        if cik is None:
            raise ValueError("SEC 目录未匹配该证券")
        return cik

    for security in securities:
        if security.market == "US":
            for capability, callback in (
                (
                    "companyfacts",
                    lambda s=security: _sec_facts(s, cik_for(s), fetch, current),
                ),
                (
                    "submissions",
                    lambda s=security: _sec_disclosures(s, cik_for(s), fetch, current),
                ),
                ("attachments", lambda s=security: _sec_attachments(session, s, fetch, current)),
            ):
                reports.append(
                    _run_source(
                        session,
                        f"sec_{capability}:{security.id}",
                        security.id,
                        capability,
                        current,
                        callback if settings.sec_edgar_enabled else None,
                        "SEC 来源已关闭",
                    )
                )
            for dividends in (False, True):
                reports.append(
                    _run_source(
                        session,
                        f"finnhub_{'dividend' if dividends else 'earnings'}:{security.id}",
                        security.id,
                        "company_calendar",
                        current,
                        (
                            partial(
                                _prepare_finnhub,
                                security,
                                fetch,
                                settings,
                                current,
                                dividends=dividends,
                            )
                        )
                        if settings.finnhub_configured
                        else None,
                        "未配置现有 Finnhub 凭据，可登记官方 IR 事件",
                    )
                )
        elif security.market == "A":
            for capability in ("disclosure_date", "dividend", "fina_indicator"):
                reports.append(
                    _run_source(
                        session,
                        f"tushare_{capability}:{security.id}",
                        security.id,
                        capability,
                        current,
                        (partial(_prepare_tushare, security, settings, current, capability))
                        if settings.tushare_token
                        else None,
                        "未配置 Tushare；可登记巨潮或交易所原始披露",
                    )
                )
        else:
            reports.append(
                _run_source(
                    session,
                    f"hkex_public:{security.id}",
                    security.id,
                    "official_disclosure",
                    current,
                    None,
                    "支持 HKEXnews 官方原文登记；没有启用付费实时公告流",
                )
            )

    for source, url, parser in (
        ("bls_calendar", BLS_URL, parse_bls_calendar),
        ("fomc_calendar", FOMC_URL, parse_fomc_calendar),
    ):

        def prepare_calendar(
            url: str = url, parser: Any = parser, source: str = source
        ) -> SourceWriter:
            rows = parser(str(fetch(url)))
            if not rows:
                raise ValueError("官方日历解析为空，无法核对已移除事件")
            return partial(_save_calendar, rows=rows, source=source, current=current)

        reports.append(
            _run_source(session, source, None, "macro_calendar", current, prepare_calendar)
        )
    for series, unit in (("CES0000000001", "thousand_people"), ("LNS14000000", "percent")):

        def prepare_actual(series: str = series, unit: str = unit) -> SourceWriter:
            url = f"https://api.bls.gov/publicAPI/v2/timeseries/data/{series}"
            return partial(
                save_macro_vintages, payload=fetch(url), series=series, unit=unit, now=current
            )

        reports.append(
            _run_source(
                session, f"bls_actual:{series}", None, "macro_actual", current, prepare_actual
            )
        )
    return {"sources": reports, "securities": len(securities)}


def _save_calendar(
    session: Session, rows: list[dict[str, Any]], source: str, current: datetime
) -> int:
    present_keys = {values["event_key"] for values in rows}
    days = [values["scheduled_date"] for values in rows]
    years = {
        values.get("details", {}).get("panel_year")
        for values in rows
        if isinstance(values.get("details", {}).get("panel_year"), int)
    }
    coverage = (
        {"kind": "panel_years", "years": sorted(years)}
        if source == "fomc_calendar"
        else {
            "kind": "date_range",
            "start": min(days).isoformat(),
            "end": max(days).isoformat(),
        }
    )
    count = 0
    for values in rows:
        if (
            current.date() - timedelta(days=30)
            <= values["scheduled_date"]
            <= current.date() + timedelta(days=370)
        ):
            upsert_calendar(session, security_id=None, **values, now=current)
            count += 1

    # Reconcile only complete parser output, within the source's proven coverage.
    # Compare the latest revision across sources so a later override is respected.
    latest: dict[str, ResearchCalendarRevision] = {}
    for record in session.scalars(
        select(ResearchCalendarRevision)
        .where(
            ResearchCalendarRevision.security_id.is_(None),
            ResearchCalendarRevision.observed_at <= current,
            ResearchCalendarRevision.available_at <= current,
        )
        .order_by(
            ResearchCalendarRevision.revision.desc(), ResearchCalendarRevision.id.desc()
        )
    ):
        latest.setdefault(record.event_key, record)
    for record in latest.values():
        if (
            record.source != source
            or record.event_key in present_keys
            or record.status
            in {"cancelled", "withdrawn", "released", "reported", "completed"}
        ):
            continue
        covered = (
            record.scheduled_date.year in years
            if source == "fomc_calendar"
            else min(days) <= record.scheduled_date <= max(days)
        )
        future = (
            utc(record.scheduled_at) > current
            if record.scheduled_at
            else (
                record.scheduled_date
                > current.astimezone(ZoneInfo(record.timezone)).date()
            )
        )
        if not covered or not future:
            continue
        original_details = {
            key: value
            for key, value in record.details.items()
            if key != "_content_fingerprint"
        }
        upsert_calendar(
            session,
            security_id=None,
            event_key=record.event_key,
            title=record.title,
            event_type=record.event_type,
            scheduled_date=record.scheduled_date,
            scheduled_at=utc(record.scheduled_at) if record.scheduled_at else None,
            timezone=record.timezone,
            status="cancelled",
            source=record.source,
            source_url=record.source_url,
            details={
                **original_details,
                "withdrawal_reason": "官方当前日历已不再列出，需复核；未推断改期关系",
                "source_removed": True,
                "source_removal_observed_at": current.isoformat(),
                "status_before_withdrawal": record.status,
                "reconciliation_coverage": coverage,
            },
            now=current,
        )
        count += 1
    return count
