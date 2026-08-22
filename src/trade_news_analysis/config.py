"""Application configuration loaded exclusively from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_WATCHLIST = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA"
DEFAULT_COMPANIES: dict[str, tuple[str, list[str]]] = {
    "AAPL": ("Apple Inc.", ["Apple"]),
    "MSFT": ("Microsoft Corporation", ["Microsoft"]),
    "NVDA": ("NVIDIA Corporation", ["NVIDIA"]),
    "AMZN": ("Amazon.com, Inc.", ["Amazon"]),
    "GOOGL": ("Alphabet Inc.", ["Alphabet", "Google"]),
    "META": ("Meta Platforms, Inc.", ["Meta Platforms", "Facebook"]),
    "TSLA": ("Tesla, Inc.", ["Tesla"]),
}

DEFAULT_SECURITY_META: dict[str, tuple[str, str, str, str, str]] = {
    symbol: ("US", "NASDAQ", "USD", "America/New_York", "US")
    for symbol in DEFAULT_COMPANIES
}


class Settings(BaseSettings):
    """Runtime settings. Secrets are represented as ``SecretStr`` to avoid log leaks."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_name: str = "金融新闻影响分析平台"
    database_url: str = "sqlite:///./data/trade_news.db"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4o-mini"
    llm_thinking: Literal["enabled", "disabled"] | None = None
    tushare_token: SecretStr | None = None
    tushare_news_enabled: bool = False
    akshare_enabled: bool = True
    scheduler_enabled: bool = True
    ingest_interval_minutes: int = Field(default=30, ge=5, le=1440)
    request_timeout_seconds: float = Field(default=20.0, ge=1, le=120)
    auto_analyze: bool = True
    app_timezone: str = "America/New_York"
    seed_watchlist: str = DEFAULT_WATCHLIST
    http_user_agent: str = "tradeNewsAnalysis/0.1 local-research"

    @property
    def tushare_configured(self) -> bool:
        return bool(self.tushare_token and self.tushare_token.get_secret_value().strip())

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_key.get_secret_value().strip())

    @property
    def seed_symbols(self) -> list[str]:
        return [
            symbol.strip().upper() for symbol in self.seed_watchlist.split(",") if symbol.strip()
        ]

    def ensure_local_directories(self) -> None:
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            database_path = Path(self.database_url.removeprefix(prefix))
            database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
