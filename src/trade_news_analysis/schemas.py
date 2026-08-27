"""Validated API and two-stage LLM data contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Direction = Literal["bullish", "neutral", "bearish"]
Market = Literal["A", "HK", "US"]


class HorizonImpact(BaseModel):
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)


class CandidateCompany(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    symbol: str | None = Field(default=None, max_length=24)
    market: Market | None = None
    supply_chain_role: str = Field(min_length=1, max_length=300)
    chain_level: int = Field(default=1, ge=1, le=3)
    themes: list[str] = Field(min_length=1, max_length=3)


class EventPayload(BaseModel):
    canonical_title: str = Field(min_length=1, max_length=500)
    event_type: str = Field(min_length=1, max_length=80)
    observed_demand: str = Field(min_length=1, max_length=2000)
    themes: list[str] = Field(min_length=1, max_length=8)
    candidates: list[CandidateCompany] = Field(max_length=30)
    evidence: list[str] = Field(max_length=12)

    @model_validator(mode="after")
    def candidate_themes_must_belong_to_event(self) -> Self:
        event_themes = set(self.themes)
        for candidate in self.candidates:
            unknown = set(candidate.themes) - event_themes
            if unknown:
                raise ValueError(
                    f"候选 {candidate.name} 引用了事件外主题：{sorted(unknown)}"
                )
        return self


class ImpactPayload(BaseModel):
    impacts: dict[Literal["1", "5", "20"], HorizonImpact]
    demand_certainty: float = Field(ge=0, le=5)
    transmission_clarity: float = Field(ge=0, le=5)
    business_purity: float = Field(ge=0, le=5)
    scale_elasticity: float = Field(ge=0, le=5)
    market_neglect: float = Field(ge=0, le=5)
    novelty_unpriced: float = Field(ge=0, le=5)
    verification_speed: float = Field(ge=0, le=5)
    risk_penalty: float = Field(ge=0, le=20)
    financial_channels: list[str] = Field(max_length=8)
    thesis: str = Field(min_length=1, max_length=3000)
    catalysts: list[str] = Field(max_length=8)
    risks: list[str] = Field(max_length=8)
    falsifiers: list[str] = Field(max_length=8)
    evidence: list[str] = Field(max_length=8)

    @field_validator("impacts")
    @classmethod
    def require_all_horizons(
        cls, value: dict[Literal["1", "5", "20"], HorizonImpact]
    ) -> dict[Literal["1", "5", "20"], HorizonImpact]:
        if set(value) != {"1", "5", "20"}:
            raise ValueError("impacts must contain exactly 1, 5, and 20")
        return value


class SecurityInput(BaseModel):
    market: Market
    exchange: str = Field(min_length=1, max_length=20)
    symbol: str = Field(pattern=r"^[A-Za-z0-9.^-]{1,24}$")
    name: str = Field(min_length=1, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    industry: str = Field(default="", max_length=160)
    business_summary: str = Field(default="", max_length=10000)

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, value: str) -> str:
        return value.upper()


class WatchlistInput(BaseModel):
    security_id: int | None = Field(default=None, gt=0)
    query: str | None = Field(default=None, max_length=160)
    market: Market | None = None
    active: bool = True

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        value = value.strip() if value else None
        return value or None

    @model_validator(mode="after")
    def require_security_reference(self) -> Self:
        if self.security_id is None and self.query is None:
            raise ValueError("security_id 和 query 至少提供一个")
        return self


class WatchlistReplace(BaseModel):
    items: list[WatchlistInput] = Field(max_length=500)


class PEForecastAssumption(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    year_offset: int = Field(ge=1, le=4)
    revenue_growth: float | None = Field(default=None, gt=-1)
    net_income_growth: float | None = Field(default=None, gt=-1)
    pe_low: float = Field(gt=0)
    pe_high: float = Field(gt=0)

    @model_validator(mode="after")
    def validate_pe_range(self) -> Self:
        if self.pe_low > self.pe_high:
            raise ValueError("PE Low 不能高于 PE High")
        return self


class PEAnalysisOverrides(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    fiscal_year: int | None = Field(default=None, ge=1900, le=2200)
    price: float | None = Field(default=None, gt=0)
    shares_outstanding: float | None = Field(default=None, gt=0)
    revenue: float | None = Field(default=None, gt=0)
    net_income: float | None = None


class PEAnalysisUpdate(BaseModel):
    overrides: PEAnalysisOverrides = Field(default_factory=PEAnalysisOverrides)
    assumptions: list[PEForecastAssumption] = Field(min_length=4, max_length=4)

    @field_validator("assumptions")
    @classmethod
    def require_four_ordered_years(
        cls, value: list[PEForecastAssumption]
    ) -> list[PEForecastAssumption]:
        if [item.year_offset for item in value] != [1, 2, 3, 4]:
            raise ValueError("assumptions 必须按顺序包含 1、2、3、4 年")
        return value


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunResponse(ORMModel):
    id: int
    trigger: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    articles_seen: int
    articles_new: int
    analyses_created: int
    errors: list[str]
