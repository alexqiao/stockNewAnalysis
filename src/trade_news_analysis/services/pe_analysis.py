"""Auditable PE valuation calculations derived from the reference workbook."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PEAnalysisProfile, Security, utc_now
from ..schemas import PEAnalysisUpdate
from .providers import FundamentalSnapshot

FORECAST_YEARS = 4
SOURCE_TTL = timedelta(hours=24)


def default_assumptions() -> list[dict[str, Any]]:
    return [
        {
            "year_offset": year_offset,
            "revenue_growth": None,
            "net_income_growth": None,
            "pe_low": 15.0,
            "pe_high": 20.0,
        }
        for year_offset in range(1, FORECAST_YEARS + 1)
    ]


def get_or_create_profile(session: Session, security: Security) -> PEAnalysisProfile:
    profile = session.scalar(
        select(PEAnalysisProfile).where(PEAnalysisProfile.security_id == security.id)
    )
    if profile is None:
        profile = PEAnalysisProfile(
            security_id=security.id,
            assumptions=default_assumptions(),
        )
        session.add(profile)
        session.flush()
    return profile


def apply_update(profile: PEAnalysisProfile, payload: PEAnalysisUpdate) -> None:
    overrides = payload.overrides
    profile.fiscal_year_override = overrides.fiscal_year
    profile.price_override = overrides.price
    profile.shares_outstanding_override = overrides.shares_outstanding
    profile.revenue_override = overrides.revenue
    profile.net_income_override = overrides.net_income
    profile.assumptions = [item.model_dump() for item in payload.assumptions]
    profile.updated_at = utc_now()


def apply_snapshot(
    profile: PEAnalysisProfile,
    snapshot: FundamentalSnapshot,
    attempted_at: datetime | None = None,
) -> None:
    now = attempted_at or utc_now()
    profile.source_fiscal_year = snapshot.fiscal_year
    profile.source_price = snapshot.price
    profile.source_market_cap = snapshot.market_cap
    profile.source_shares_outstanding = snapshot.shares_outstanding
    profile.source_revenue = snapshot.revenue
    profile.source_net_income = snapshot.net_income
    profile.source_name = snapshot.source
    profile.source_attempted_at = now
    profile.source_fetched_at = now
    missing = _missing_base_fields(
        snapshot.fiscal_year,
        snapshot.price,
        snapshot.revenue,
        snapshot.net_income,
        snapshot.shares_outstanding,
    )
    profile.source_status = "partial" if missing else "ready"
    profile.source_error = f"缺少自动数据：{'、'.join(missing)}" if missing else None
    profile.updated_at = now


def record_refresh_error(
    profile: PEAnalysisProfile, error: Exception, attempted_at: datetime | None = None
) -> None:
    now = attempted_at or utc_now()
    profile.source_status = "error"
    profile.source_error = str(error)[:2000]
    profile.source_attempted_at = now
    profile.updated_at = now


def is_refresh_recommended(
    profile: PEAnalysisProfile | None, now: datetime | None = None
) -> bool:
    if profile is None or profile.source_attempted_at is None:
        return True
    current = now or utc_now()
    attempted = profile.source_attempted_at
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current - attempted >= SOURCE_TTL


def calculate_forecast(
    *,
    fiscal_year: int | None,
    current_price: float | None,
    revenue: float | None,
    net_income: float | None,
    shares_outstanding: float | None,
    assumptions: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    missing = _missing_base_fields(
        fiscal_year, current_price, revenue, net_income, shares_outstanding
    )
    if missing:
        warnings.append(f"缺少有效基础数据：{'、'.join(missing)}")

    rows: list[dict[str, Any]] = []
    previous_revenue = revenue if revenue is not None and revenue > 0 else None
    previous_net_income = net_income
    assumptions_complete = True
    positive_eps_count = 0

    for item in assumptions:
        year_offset = int(item["year_offset"])
        revenue_growth = item.get("revenue_growth")
        net_income_growth = item.get("net_income_growth")
        if revenue_growth is None or net_income_growth is None:
            assumptions_complete = False

        projected_revenue = (
            previous_revenue * (1 + float(revenue_growth))
            if previous_revenue is not None and revenue_growth is not None
            else None
        )
        projected_net_income = (
            previous_net_income * (1 + float(net_income_growth))
            if previous_net_income is not None and net_income_growth is not None
            else None
        )
        net_margin = (
            projected_net_income / projected_revenue
            if projected_net_income is not None
            and projected_revenue is not None
            and projected_revenue != 0
            else None
        )
        eps = (
            projected_net_income / shares_outstanding
            if projected_net_income is not None
            and shares_outstanding is not None
            and shares_outstanding > 0
            else None
        )
        if eps is not None and eps > 0:
            positive_eps_count += 1
        price_low = eps * float(item["pe_low"]) if eps is not None and eps > 0 else None
        price_high = eps * float(item["pe_high"]) if eps is not None and eps > 0 else None
        cagr_low = _cagr(price_low, current_price, year_offset)
        cagr_high = _cagr(price_high, current_price, year_offset)
        valuation = _valuation_comparison(
            year=fiscal_year + year_offset if fiscal_year is not None else None,
            current_price=current_price,
            eps=eps,
            pe_low=float(item["pe_low"]),
            pe_high=float(item["pe_high"]),
            price_low=price_low,
            price_high=price_high,
        )
        rows.append(
            {
                "year_offset": year_offset,
                "year": fiscal_year + year_offset if fiscal_year is not None else None,
                "revenue_growth": revenue_growth,
                "revenue": projected_revenue,
                "net_income_growth": net_income_growth,
                "net_income": projected_net_income,
                "net_margin": net_margin,
                "eps": eps,
                "pe_low": item["pe_low"],
                "pe_high": item["pe_high"],
                "price_low": price_low,
                "price_high": price_high,
                "cagr_low": cagr_low,
                "cagr_high": cagr_high,
                **valuation,
            }
        )
        previous_revenue = projected_revenue
        previous_net_income = projected_net_income

    if not assumptions_complete:
        warnings.append("请填写四个年度的营收增速和净利润增速")
    if not missing and assumptions_complete and positive_eps_count < FORECAST_YEARS:
        warnings.append("预测 EPS 非正的年度不适用 PE 目标价与 CAGR")

    if missing:
        status = "needs_data"
    elif not assumptions_complete:
        status = "needs_input"
    elif positive_eps_count == 0:
        status = "not_applicable"
    else:
        status = "ready"
    return status, rows, warnings


def analysis_response(
    security: Security,
    profile: PEAnalysisProfile | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    assumptions = profile.assumptions if profile and profile.assumptions else default_assumptions()
    overrides = {
        "fiscal_year": profile.fiscal_year_override if profile else None,
        "price": profile.price_override if profile else None,
        "shares_outstanding": profile.shares_outstanding_override if profile else None,
        "revenue": profile.revenue_override if profile else None,
        "net_income": profile.net_income_override if profile else None,
    }
    source_data = {
        "fiscal_year": profile.source_fiscal_year if profile else None,
        "price": profile.source_price if profile else None,
        "market_cap": profile.source_market_cap if profile else None,
        "shares_outstanding": profile.source_shares_outstanding if profile else None,
        "revenue": profile.source_revenue if profile else None,
        "net_income": profile.source_net_income if profile else None,
    }
    effective_inputs: dict[str, dict[str, Any]] = {}
    for field in ("fiscal_year", "price", "shares_outstanding", "revenue", "net_income"):
        override = overrides[field]
        effective_inputs[field] = {
            "value": override if override is not None else source_data[field],
            "provenance": "manual" if override is not None else "auto",
        }
    status, forecast, warnings = calculate_forecast(
        fiscal_year=effective_inputs["fiscal_year"]["value"],
        current_price=effective_inputs["price"]["value"],
        revenue=effective_inputs["revenue"]["value"],
        net_income=effective_inputs["net_income"]["value"],
        shares_outstanding=effective_inputs["shares_outstanding"]["value"],
        assumptions=assumptions,
    )
    if profile and profile.source_error:
        warnings.insert(0, profile.source_error)
    last_row = forecast[-1] if forecast else None
    valuation = _current_valuation(forecast, effective_inputs["price"]["value"])
    summary = {
        "status": status,
        "year": last_row["year"] if last_row else None,
        "price_low": last_row["price_low"] if last_row else None,
        "price_high": last_row["price_high"] if last_row else None,
        "cagr_low": last_row["cagr_low"] if last_row else None,
        "cagr_high": last_row["cagr_high"] if last_row else None,
        "valuation_status": valuation["status"],
        "valuation_label": valuation["label"],
        "valuation_year": valuation["year"],
        "current_implied_pe": valuation["current_implied_pe"],
        "price_change_low": valuation["price_change_low"],
        "price_change_high": valuation["price_change_high"],
        "updated_at": profile.updated_at if profile else None,
    }
    return {
        "security": {
            "id": security.id,
            "market": security.market,
            "symbol": security.symbol,
            "name": security.name,
            "currency": security.currency,
        },
        "status": status,
        "source": {
            "name": profile.source_name if profile else "investormate/yfinance",
            "status": profile.source_status if profile else "uninitialized",
            "error": profile.source_error if profile else None,
            "attempted_at": profile.source_attempted_at if profile else None,
            "fetched_at": profile.source_fetched_at if profile else None,
        },
        "source_data": source_data,
        "overrides": overrides,
        "effective_inputs": effective_inputs,
        "assumptions": assumptions,
        "forecast": forecast,
        "valuation": valuation,
        "warnings": warnings,
        "summary": summary,
        "refresh_recommended": is_refresh_recommended(profile, now),
        "model_notes": [
            "市值仅展示，不参与估值计算。",
            "当前估值判断使用第一预测年度 EPS 对应的隐含 PE；第四年用于观察长期目标空间和 CAGR。",
            "“偏高/偏低”仅表示相对你填写的盈利与 PE 区间假设，不代表绝对价值判断。",
            "预测期内总股本保持不变，不包含分红、回购、稀释或汇率收益。",
            "15x/20x 是参考表默认假设，不构成投资建议。",
        ],
    }


def _missing_base_fields(
    fiscal_year: int | None,
    price: float | None,
    revenue: float | None,
    net_income: float | None,
    shares_outstanding: float | None,
) -> list[str]:
    missing = []
    if fiscal_year is None:
        missing.append("基准财年")
    if price is None or price <= 0:
        missing.append("当前价")
    if revenue is None or revenue <= 0:
        missing.append("营收")
    if net_income is None:
        missing.append("净利润")
    if shares_outstanding is None or shares_outstanding <= 0:
        missing.append("总股本")
    return missing


def _cagr(target_price: float | None, current_price: float | None, years: int) -> float | None:
    if (
        target_price is None
        or target_price <= 0
        or current_price is None
        or current_price <= 0
        or years <= 0
    ):
        return None
    return (target_price / current_price) ** (1 / years) - 1


def _valuation_comparison(
    *,
    year: int | None,
    current_price: float | None,
    eps: float | None,
    pe_low: float,
    pe_high: float,
    price_low: float | None,
    price_high: float | None,
) -> dict[str, Any]:
    unavailable = {
        "current_implied_pe": None,
        "price_change_low": None,
        "price_change_high": None,
        "valuation_status": "unavailable",
        "valuation_label": "暂无法判断",
        "valuation_reason": "缺少有效当前价或正的预测 EPS，暂不能比较当前估值。",
    }
    if (
        current_price is None
        or current_price <= 0
        or eps is None
        or eps <= 0
        or price_low is None
        or price_high is None
    ):
        return unavailable

    implied_pe = current_price / eps
    price_change_low = price_low / current_price - 1
    price_change_high = price_high / current_price - 1
    year_label = str(year) if year is not None else "该年度"
    basis = (
        f"当前价 {current_price:.2f} ÷ {year_label} 年预测 EPS {eps:.2f} "
        f"= {implied_pe:.2f}x 隐含 PE"
    )
    assumption_range = f"你设定的 {pe_low:.2f}x–{pe_high:.2f}x PE 区间"

    if current_price < price_low:
        return {
            "current_implied_pe": implied_pe,
            "price_change_low": price_change_low,
            "price_change_high": price_change_high,
            "valuation_status": "below_range",
            "valuation_label": "当前价偏低",
            "valuation_reason": f"{basis}，低于{assumption_range}。",
        }
    if current_price > price_high:
        return {
            "current_implied_pe": implied_pe,
            "price_change_low": price_change_low,
            "price_change_high": price_change_high,
            "valuation_status": "above_range",
            "valuation_label": "当前价偏高",
            "valuation_reason": f"{basis}，高于{assumption_range}。",
        }
    return {
        "current_implied_pe": implied_pe,
        "price_change_low": price_change_low,
        "price_change_high": price_change_high,
        "valuation_status": "within_range",
        "valuation_label": "当前价位于合理区间",
        "valuation_reason": f"{basis}，位于{assumption_range}内。",
    }


def _current_valuation(
    forecast: list[dict[str, Any]], current_price: float | None
) -> dict[str, Any]:
    row = forecast[0] if forecast else None
    if row is None:
        return {
            "basis": "first_forecast_year",
            "year": None,
            "current_price": current_price,
            "eps": None,
            "pe_low": None,
            "pe_high": None,
            "price_low": None,
            "price_high": None,
            "current_implied_pe": None,
            "price_change_low": None,
            "price_change_high": None,
            "status": "unavailable",
            "label": "暂无法判断",
            "reason": "缺少第一预测年度数据，暂不能判断当前估值。",
        }
    return {
        "basis": "first_forecast_year",
        "year": row["year"],
        "current_price": current_price,
        "eps": row["eps"],
        "pe_low": row["pe_low"],
        "pe_high": row["pe_high"],
        "price_low": row["price_low"],
        "price_high": row["price_high"],
        "current_implied_pe": row["current_implied_pe"],
        "price_change_low": row["price_change_low"],
        "price_change_high": row["price_change_high"],
        "status": row["valuation_status"],
        "label": row["valuation_label"],
        "reason": row["valuation_reason"],
    }
