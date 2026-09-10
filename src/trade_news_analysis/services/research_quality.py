"""Deterministic completeness checks for company-level research inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Security


def company_data_quality(security: Security) -> tuple[float, list[str]]:
    gaps: list[str] = []
    score = 0.0
    if security.industry:
        score += 0.2
    else:
        gaps.append("行业分类")
    if security.business_summary:
        score += 0.5
    else:
        gaps.append("主营业务与收入结构")
    if security.market_cap is not None:
        score += 0.3
    else:
        gaps.append("市值")
    return round(score, 2), gaps


def apply_company_data_caps(
    security: Security, dimensions: dict[str, float]
) -> tuple[dict[str, float], list[str]]:
    adjusted = dict(dimensions)
    _, gaps = company_data_quality(security)
    if not security.business_summary:
        adjusted["business_purity"] = min(adjusted["business_purity"], 1.0)
    if security.market_cap is None:
        adjusted["scale_elasticity"] = min(adjusted["scale_elasticity"], 2.0)
    if not security.industry:
        adjusted["transmission_clarity"] = min(
            adjusted["transmission_clarity"], 3.0
        )
    return adjusted, gaps
