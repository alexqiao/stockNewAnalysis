"""Editable planning assumptions; account facts are never defaulted here."""

from __future__ import annotations

from typing import Any

from ..models import Security

RISK_DEFAULTS = {
    "max_weight": (0.20, "默认 20%，可修改"),
    "risk_budget_pct": (0.01, "默认组合净值的 1%，可修改"),
    "sector_limit_pct": (0.40, "默认 40%，可修改"),
    "max_participation_pct": (0.01, "默认日均成交量的 1%，可修改"),
    "fee_bps": (10, "默认估算 10 基点；请按实际费率修改"),
    "slippage_bps": (10, "默认估算 10 基点，可修改"),
}

# Approximate sector comparisons, not claims that a stock is an ETF constituent.
# Fund sectors: https://www.ssga.com/us/en/individual/capabilities/equities/sector-investing/select-sector-etfs
SECTORS = {
    "XLK": ("科技", (
        "technology", "computer hardware", "consumer electronics", "semiconductors",
        "semiconductor equipment & materials", "software - infrastructure",
        "software - application", "information technology services", "半导体", "消费电子", "软件",
    )),
    "XLF": ("金融", (
        "financial services", "financials", "credit services", "banks - regional",
        "banks - diversified", "capital markets", "asset management", "金融", "银行",
    )),
    "XLI": ("工业", (
        "industrials", "aerospace & defense", "specialty industrial machinery",
        "industrial distribution", "工业", "航空航天与国防",
    )),
    "XLV": ("医疗", (
        "healthcare", "health care", "medical instruments & supplies", "medical devices",
        "biotechnology", "drug manufacturers - general", "医疗", "生物技术",
    )),
    "XLY": ("可选消费", (
        "consumer cyclical", "consumer discretionary", "auto manufacturers", "internet retail",
        "汽车制造", "互联网零售与云计算", "可选消费",
    )),
    "XLC": ("通信服务", (
        "communication services", "internet content & information", "telecom services",
        "互联网服务", "通信服务",
    )),
    "XLP": ("必需消费", ("consumer defensive", "consumer staples", "必需消费")),
    "XLE": ("能源", ("energy", "oil & gas integrated", "能源")),
    "XLB": ("材料", ("basic materials", "materials", "材料")),
    "XLRE": ("房地产", ("real estate", "房地产")),
    "XLU": ("公用事业", ("utilities", "公用事业")),
}
BENCHMARK_FIELDS = (
    "benchmark_market", "benchmark_symbol", "benchmark_currency", "benchmark_label",
)


def sector_key(security: Security) -> str | None:
    metadata = security.provider_data or {}
    labels = {str(metadata.get("sector") or "").strip().casefold(),
              (security.industry or "").strip().casefold()}
    for symbol, (_, aliases) in SECTORS.items():
        if labels.intersection(aliases) or (security.market == "US" and security.symbol == symbol):
            return symbol
    return f"industry:{security.industry.strip().casefold()}" if security.industry.strip() else None


def benchmark_inputs(security: Security, saved: dict[str, Any]) -> dict[str, Any]:
    """Keep a custom benchmark coherent; never substitute a broad market for an industry."""
    market = saved.get("benchmark_market") or security.market
    currency = saved.get("benchmark_currency") or {
        "US": "USD", "A": "CNY", "HK": "HKD",
    }.get(market)
    symbol = saved.get("benchmark_symbol")
    label = saved.get("benchmark_label")
    sector = sector_key(security)
    if not symbol and market == security.market == "US" and sector in SECTORS:
        symbol = sector
        label = label or f"按已有行业资料默认使用{SECTORS[sector][0]}板块 ETF，作为近似对照，可修改"
    elif symbol:
        label = label or "自选行业基准"
    return dict(zip(BENCHMARK_FIELDS, (market, symbol, currency, label), strict=True))
