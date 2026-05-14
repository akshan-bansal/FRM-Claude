from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ExecutionMode = Literal["paper", "dry-run", "live"]
QuestradeEnv = Literal["practice", "live"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Questrade
    questrade_refresh_token: str = ""
    questrade_env: QuestradeEnv = "practice"
    questrade_account_number: str = ""

    # Execution
    execution_mode: ExecutionMode = "paper"

    # Risk
    risk_pct_per_trade: float = Field(default=0.01, ge=0.0001, le=0.05)
    portfolio_heat_cap: float = Field(default=0.05, ge=0.001, le=0.5)
    daily_loss_limit_pct: float = Field(default=0.03, ge=0.001, le=0.2)
    max_drawdown_kill_switch: float = Field(default=0.10, ge=0.01, le=0.5)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    min_ticket_usd: float = Field(default=100.0, ge=0.0)

    # Strategy / Universe
    default_strategy: str = "ema_crossover"
    default_symbols: str = "AAPL,MSFT,SHOP.TO,XIC.TO"
    timezone: str = "America/Toronto"

    # Storage
    log_level: str = "INFO"
    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    data_cache_dir: Path = Path("data/cache")

    # Encryption
    token_encryption_key: str = ""

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email_to: str = ""

    @field_validator("default_symbols")
    @classmethod
    def _strip_symbols(cls, v: str) -> str:
        return ",".join(s.strip().upper() for s in v.split(",") if s.strip())

    @property
    def symbols_list(self) -> list[str]:
        return [s for s in self.default_symbols.split(",") if s]

    @property
    def is_live_capable(self) -> bool:
        """True only if all live preconditions are present (does not mean it's live)."""
        return bool(
            self.questrade_refresh_token
            and self.token_encryption_key
            and self.execution_mode == "live"
            and self.questrade_env == "live"
        )

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.log_dir, self.data_cache_dir, Path("reports")):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
