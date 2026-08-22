"""Validated API and LLM data contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Direction = Literal["bullish", "neutral", "bearish"]


class HorizonImpact(BaseModel):
    direction: Direction
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=1000)


class ImpactPayload(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    novelty: float = Field(ge=0, le=1)
    priced_in: float = Field(ge=0, le=1)
    impacts: dict[Literal["1", "5", "20"], HorizonImpact]
    financial_channels: list[str] = Field(max_length=8)
    observed_demand: str = Field(min_length=1, max_length=2000)
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


class WatchlistInput(BaseModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9.^-]{1,20}$")
    company_name: str = Field(default="", max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    active: bool = True

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, value: str) -> str:
        return value.upper()


class WatchlistReplace(BaseModel):
    items: list[WatchlistInput] = Field(max_length=500)


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
