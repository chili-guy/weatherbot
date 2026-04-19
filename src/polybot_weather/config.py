"""Runtime configuration loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="POLYBOT_",
        extra="ignore",
    )

    user_agent: str = Field(
        default="polybot-weather (unset@example.com)",
        description="User-Agent string for NWS — must be set to a real contact.",
    )

    bankroll_usd: float = Field(default=1000.0, ge=0)

    min_edge: float = Field(default=0.05, ge=0, le=1)
    min_ev: float = Field(default=0.10, ge=0)
    min_liquidity_usd: float = Field(default=50.0, ge=0)
    max_hours_to_resolution: int = Field(default=72, gt=0)
    # Polymarket weather markets have a 5% taker fee on entry (see feeSchedule
    # on any weather sub-market). We model it as a multiplier on the ask when
    # scoring edge, so the bot only recommends trades whose model probability
    # clears the post-fee price.
    fee_rate: float = Field(default=0.05, ge=0, le=0.5)

    kelly_fraction: float = Field(default=0.25, ge=0, le=1)
    max_bet_fraction: float = Field(default=0.05, ge=0, le=1)

    db_url: str = Field(default="sqlite:///./polybot.db")
    cache_dir: Path = Field(default=Path("./.cache"))

    forecast_ttl_seconds: int = Field(default=1800, ge=0)
    climatology_ttl_seconds: int = Field(default=86400, ge=0)

    execution_enabled: bool = Field(default=False)
    private_key: str | None = Field(default=None, repr=False)
    funder_address: str | None = Field(default=None)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cache and return the singleton Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.cache_dir.mkdir(parents=True, exist_ok=True)
    return _settings
