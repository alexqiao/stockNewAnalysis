"""Read-only, source-labelled research inputs for security impact analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from .financial_research import get_financial_research
from .market_research import get_market_research
from .research_workflow import list_claims


def _metric_priority(metric: dict[str, Any]) -> int:
    concept = str(metric.get("concept", "")).rsplit(":", 1)[-1].lower().replace("_", "")
    for rank, terms in enumerate((
        ("revenue", "salesrevenue"), ("netincome", "profitloss"),
        ("operatingincome", "grossprofit"), ("operatingactivities", "cashflow"),
        ("earningspershare", "eps"), ("cashandcash", "debt", "assets", "liabilities"),
    )):
        if any(term in concept for term in terms):
            return rank
    return 6


def _timestamp(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).timestamp()
    except (ValueError, TypeError):
        return float("-inf")


def material_research_hash(context: dict[str, Any]) -> str:
    """Only facts and human reviews invalidate analysis; quote movement does not."""
    ignored = {
        "as_of", "observed_at", "fetched_at", "created_at", "updated_at", "reviewed_at",
        "fact_id", "calendar_revision_id", "expectation_id", "periods_count",
    }

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in ignored}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    payload = clean({key: context.get(key) for key in ("financial", "x_review_records")})
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str,
    ).encode()).hexdigest()


def build_analysis_context(session: Session, security_id: int) -> dict[str, Any]:
    financial = get_financial_research(session, security_id)
    market = get_market_research(session, security_id)
    claims = list_claims(session, security_id)
    metrics = sorted(financial["metrics"], key=lambda item: (
        _metric_priority(item), -_timestamp(item["latest"].get("period_end")),
        -_timestamp(item["latest"].get("available_at")), str(item["concept"]),
    ))
    selected: list[dict[str, Any]] = []
    covered = set()
    for metric in metrics:
        category = _metric_priority(metric)
        if category < 6 and category not in covered:
            selected.append(metric)
            covered.add(category)
    selected.extend(metric for metric in metrics if metric not in selected)
    surprises = sorted(financial["surprises"], key=lambda item: (
        -_timestamp(item.get("actual_published_at")), str(item.get("event_key")),
        str(item.get("metric")),
    ))
    context = {
        "as_of": financial["as_of"],
        "selection_version": "material-context-v2",
        "financial": {
            "metrics": selected[:20],
            "surprises": surprises[:8],
            "gaps": financial["gaps"],
        },
        "market": {key: market.get(key) for key in (
            "status", "blockers", "quote", "horizons", "benchmark",
            "annualized_volatility_20_pct", "liquidity", "pricing_note",
        )},
        "x_review_records": [
            {key: claim.get(key) for key in (
                "claim_text", "claim_kind", "author", "post_url", "published_at",
                "status", "note", "source_truncated", "verification_needs", "evidence",
            )}
            for claim in claims if claim["status"] != "expired"
        ][:8],
    }
    context["material_hash"] = material_research_hash(context)
    return context
