"""Parse official schedules; preserve unknown times and explicit revisions."""

import calendar
import re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup

BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
NEW_YORK = "America/New_York"


def _ics_text(value: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda match: " " if match[1] in "nN" else match[1], value)


def _ics_fields(text: str) -> dict[str, tuple[dict[str, str], str]]:
    result: dict[str, tuple[dict[str, str], str]] = {}
    nested_depth = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError("BLS 日历包含无效字段")
        header, value = line.split(":", 1)
        parts = header.split(";")
        name = parts[0].upper()
        if name == "BEGIN":
            nested_depth += 1
            continue
        if name == "END":
            nested_depth -= 1
            if nested_depth < 0:
                raise ValueError("BLS 事件包含不完整嵌套组件")
            continue
        if nested_depth:
            continue
        params = {}
        for item in parts[1:]:
            if "=" not in item:
                raise ValueError("BLS 日历包含无效字段参数")
            key, parameter = item.split("=", 1)
            params[key.upper()] = parameter.strip('"')
        if name in result and name in {"UID", "DTSTART", "RECURRENCE-ID", "STATUS"}:
            raise ValueError("BLS 日历包含重复关键字段")
        result[name] = (params, value.strip())
    if nested_depth:
        raise ValueError("BLS 事件包含不完整嵌套组件")
    return result


def _ics_start(
    params: dict[str, str], value: str, default_zone: str | None
) -> tuple[date, datetime | None, str]:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date(), None, "date"
    if not re.fullmatch(r"\d{8}T\d{6}Z?", value):
        raise ValueError("BLS 发布日期格式无法解析")
    naive = datetime.strptime(value.removesuffix("Z"), "%Y%m%dT%H%M%S")
    zone_name = params.get("TZID") or default_zone
    if value.endswith("Z"):
        if params.get("TZID"):
            raise ValueError("BLS UTC 时间不应同时指定 TZID")
        scheduled = naive.replace(tzinfo=UTC)
    elif zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("BLS 发布时区无法识别") from exc
        candidates = set()
        for fold in (0, 1):
            aware = naive.replace(tzinfo=zone, fold=fold)
            if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) == naive:
                candidates.add(aware.astimezone(UTC))
        if len(candidates) != 1:
            raise ValueError("BLS 发布时刻存在夏令时歧义或不存在")
        scheduled = next(iter(candidates))
    else:
        # Floating iCalendar times do not identify an absolute release instant.
        return naive.date(), None, "floating_time_without_timezone"
    return scheduled.astimezone(ZoneInfo(NEW_YORK)).date(), scheduled, "datetime"


def parse_bls_calendar(text: str) -> list[dict[str, Any]]:
    unfolded = re.sub(r"\r?\n[ \t]", "", text).strip().removeprefix("\ufeff")
    if not unfolded.startswith("BEGIN:VCALENDAR") or not unfolded.endswith("END:VCALENDAR"):
        raise ValueError("BLS 返回的不是完整日历")
    blocks = re.findall(r"BEGIN:VEVENT\r?\n(.*?)\r?\nEND:VEVENT", unfolded, re.DOTALL)
    if (
        not blocks
        or len(blocks) != unfolded.count("BEGIN:VEVENT")
        or len(blocks) != unfolded.count("END:VEVENT")
    ):
        raise ValueError("BLS 日历没有完整可解析事件")
    timezone_match = re.search(r"^X-WR-TIMEZONE:(.+)$", unfolded, re.MULTILINE)
    default_zone = timezone_match[1].strip() if timezone_match else None
    cancelled = bool(re.search(r"^METHOD:CANCEL\s*$", unfolded, re.MULTILINE))
    rows: dict[str, dict[str, Any]] = {}
    for block in blocks:
        fields = _ics_fields(block)
        params, value = fields.get("DTSTART", ({}, ""))
        title = _ics_text(fields.get("SUMMARY", ({}, ""))[1])
        uid = fields.get("UID", ({}, ""))[1]
        if not value or not title or not uid:
            raise ValueError("BLS 事件缺少 UID、标题或发布日期")
        if any(field in fields for field in ("RRULE", "RDATE", "EXDATE")):
            raise ValueError("BLS 日历包含尚不支持的重复事件规则")
        day, scheduled, precision = _ics_start(params, value, default_zone)
        status = fields.get("STATUS", ({}, "CONFIRMED"))[1].upper()
        if status not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
            raise ValueError("BLS 事件状态无法识别")
        sequence = fields.get("SEQUENCE", ({}, "0"))[1]
        if not sequence.isdigit():
            raise ValueError("BLS 事件修订序号无效")
        recurrence_id = fields.get("RECURRENCE-ID", ({}, ""))[1]
        key = f"bls:{uid}" + (f":{recurrence_id}" if recurrence_id else "")
        row = {
            "event_key": key,
            "title": title,
            "event_type": "macro_release",
            "scheduled_date": day,
            "scheduled_at": scheduled,
            "timezone": NEW_YORK,
            "status": "cancelled" if cancelled or status == "CANCELLED" else (
                "estimated" if status == "TENTATIVE" else "confirmed"
            ),
            "source": "bls_calendar",
            "source_url": BLS_URL,
            "details": {
                "uid": uid,
                "sequence": int(sequence),
                "recurrence_id": recurrence_id or None,
                "time_precision": precision,
                "source_timezone": params.get("TZID") or default_zone,
                "description": _ics_text(fields.get("DESCRIPTION", ({}, ""))[1]),
                "expectations": "官方日历不提供市场预期",
            },
        }
        existing = rows.get(key)
        if existing is None or int(sequence) > existing["details"]["sequence"]:
            rows[key] = row
        elif int(sequence) == existing["details"]["sequence"] and existing != row:
            raise ValueError("BLS 同一事件修订包含冲突日期")
    return list(rows.values())


def _meeting_end(year: int, month_text: str, days_text: str) -> date:
    months = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
    months.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})
    names = [name.strip().lower().rstrip(".") for name in month_text.split("/")]
    if not 1 <= len(names) <= 2 or any(name not in months for name in names):
        raise ValueError("FOMC 会议月份无法解析")
    match = re.match(r"^\s*(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?(?=\s|\*|\(|$)", days_text)
    if not match:
        raise ValueError("FOMC 会议日期无法解析")
    first, last = int(match[1]), int(match[2] or match[1])
    start_month, end_month = months[names[0]], months[names[-1]]
    end_year = year + int(end_month < start_month)
    first_day, last_day = date(year, start_month, first), date(end_year, end_month, last)
    if last_day < first_day or (last_day - first_day).days > 7:
        raise ValueError("FOMC 会议日期范围不合理")
    return last_day


def parse_fomc_calendar(text: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(text, "html.parser")
    result: dict[str, dict[str, Any]] = {}
    for panel in soup.select(".panel"):
        heading = panel.select_one(".panel-heading")
        match = re.search(r"(20\d{2})\s+FOMC", heading.get_text(" ", strip=True) if heading else "")
        meetings = panel.select(".fomc-meeting")
        if not match:
            if meetings:
                raise ValueError("FOMC 会议面板缺少有效年份")
            continue
        year = int(match[1])
        if not meetings:
            raise ValueError("FOMC 年度面板没有会议")
        for row in meetings:
            month_node = row.select_one(".fomc-meeting__month")
            days_node = row.select_one(".fomc-meeting__date")
            if not month_node or not days_node:
                raise ValueError("FOMC 会议缺少月份或日期")
            month_text = month_node.get_text(" ", strip=True)
            raw_days = days_node.get_text(" ", strip=True)
            date_copy = BeautifulSoup(str(days_node), "html.parser")
            old_dates = [
                node.get_text(" ", strip=True) for node in date_copy.select("s,del,strike")
            ]
            for node in date_copy.select("s,del,strike"):
                node.decompose()
            active_days = date_copy.get_text(" ", strip=True) or raw_days
            if old_dates and re.search(r"\bcancelled\b|\bcanceled\b", active_days.lower()):
                active_days = raw_days
            day = _meeting_end(year, month_text, active_days)
            # Explicit former dates preserve identity across a marked reschedule.
            original_match = re.search(
                r"(?:rescheduled|revised|changed|moved)\s+from\s+(\d{1,2}(?:\s*[-–—]\s*\d{1,2})?)",
                active_days,
                re.IGNORECASE,
            )
            original = (
                old_dates[0] if old_dates else (original_match[1] if original_match else None)
            )
            original_day = _meeting_end(year, month_text, original) if original else day
            annotation = raw_days.lower()
            cancelled = bool(re.search(r"\bcancelled\b|\bcanceled\b", annotation))
            rescheduled = bool(original or re.search(r"\brescheduled\b|\brevised\b", annotation))
            notation_vote = "notation vote" in annotation
            key = f"fomc:{original_day.isoformat()}"
            item = {
                "event_key": key,
                "title": (
                    "FOMC 书面表决 / 政策观察" if notation_vote else "FOMC 会议结束 / 政策决议观察"
                ),
                "event_type": "macro_policy",
                "scheduled_date": day,
                "scheduled_at": None,
                "timezone": NEW_YORK,
                "status": "cancelled" if cancelled else "scheduled",
                "source": "fomc_calendar",
                "source_url": FOMC_URL,
                "details": {
                    "panel_year": year,
                    "meeting_months": month_text,
                    "meeting_dates": raw_days,
                    "original_scheduled_date": original_day.isoformat() if original else None,
                    "rescheduled": rescheduled,
                    "notation_vote": notation_vote,
                    "economic_projections": "*" in raw_days,
                    "identity_basis": (
                        "original_meeting_end_date" if original else "meeting_end_date"
                    ),
                    "time_precision": "date",
                    "time_note": "页面仅提供日期，未来安排可能调整；不推定声明发布分钟",
                },
            }
            if key in result and result[key] != item:
                raise ValueError("FOMC 同一会议日期包含冲突安排")
            result[key] = item
    if not result:
        raise ValueError("FOMC 页面没有可解析会议")
    return list(result.values())
