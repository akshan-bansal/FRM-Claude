"""Settings: secrets from .env, trading knobs from config/trading.yaml.

Why split?
  .env is gitignored and Claude's settings.json explicitly denies `Read(.env)`.
  Trading knobs (strategy, symbols, risk caps, daily budgets) are not secret;
  putting them in a YAML file Claude *can* read and rewrite lets the
  `trading tune` command (and Claude during a session) pick configuration
  automatically without ever touching credentials.

Precedence:
  1. config/trading.yaml      (Claude-managed; trading knobs)
  2. environment variables    (escape hatch; useful in CI and one-off shells)
  3. .env                     (secrets only)
  4. dataclass defaults
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ExecutionMode = Literal["paper", "dry-run", "live", "autonomous"]
QuestradeEnv = Literal["practice", "live"]
AutonomousAccount = Literal["practice", "live"]

DEFAULT_TRADING_YAML = Path("config") / "trading.yaml"
TRADING_YAML_ENV_VAR = "TRADING_YAML_PATH"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Secrets (env only) ===
    questrade_refresh_token: str = ""
    token_encryption_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_email_to: str = ""
    quantconnect_user_id: str = ""
    quantconnect_api_token: str = ""
    # Kraken private REST API (crypto sleeve execution). Public market data needs none of this;
    # only the future KrakenBroker's private endpoints (balances, orders) do.
    kraken_api_key: str = ""
    kraken_api_secret: str = ""
    # WorldMonitor OSINT/news MCP (Pro). Sent as the X-WorldMonitor-Key header to the data tools;
    # only get_sources works without it. Not required for any trading path.
    worldmonitor_api_key: str = ""

    # === Trading knobs (yaml-managed) ===
    execution_mode: ExecutionMode = "paper"
    questrade_env: QuestradeEnv = "practice"
    questrade_account_number: str = ""

    risk_pct_per_trade: float = Field(default=0.01, ge=0.0001, le=0.05)
    portfolio_heat_cap: float = Field(default=0.05, ge=0.001, le=1.0)
    # Risk estimate the heat gate budgets on. risk_model: "cvar" (Expected Shortfall tail,
    # default) | "var" | "atr" (ATR-stop loss). heat_aggregation: "corr" (covariance-aware,
    # credits diversification, default) | "sum" (correlation-blind total).
    risk_model: str = Field(default="cvar", pattern="^(atr|var|cvar)$")
    heat_aggregation: str = Field(default="corr", pattern="^(sum|corr)$")
    daily_loss_limit_pct: float = Field(default=0.03, ge=0.001, le=0.2)
    max_drawdown_kill_switch: float = Field(default=0.10, ge=0.01, le=0.5)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    min_ticket_usd: float = Field(default=100.0, ge=0.0)

    default_strategy: str = "ema_crossover"
    default_symbols: str = "AAPL,MSFT,SHOP.TO,XIC.TO"
    timezone: str = "America/Toronto"
    account_currency: Literal["CAD", "USD"] = "CAD"

    autonomous_enabled: bool = False
    autonomous_account: AutonomousAccount = "practice"
    autonomous_interval_seconds: int = Field(default=1200, ge=60, le=3600)
    autonomous_strategy: str = "ema_crossover"
    autonomous_symbols: str = "AAPL,MSFT,XIC.TO,VFV.TO"
    autonomous_daily_max_trades: int = Field(default=10, ge=1, le=200)
    autonomous_daily_max_notional_usd: float = Field(default=10_000.0, ge=100.0)
    autonomous_auto_start_on_session: bool = False

    log_level: str = "INFO"
    state_dir: Path = Path("state")
    log_dir: Path = Path("logs")
    data_cache_dir: Path = Path("data/cache")

    @field_validator("default_symbols", "autonomous_symbols")
    @classmethod
    def _strip_symbols(cls, v: str) -> str:
        return ",".join(s.strip().upper() for s in v.split(",") if s.strip())

    @property
    def symbols_list(self) -> list[str]:
        return [s for s in self.default_symbols.split(",") if s]

    @property
    def autonomous_symbols_list(self) -> list[str]:
        return [s.strip().upper() for s in self.autonomous_symbols.split(",") if s.strip()]

    @property
    def is_live_capable(self) -> bool:
        return bool(
            self.questrade_refresh_token
            and self.token_encryption_key
            and self.execution_mode == "live"
            and self.questrade_env == "live"
        )

    def ensure_dirs(self) -> None:
        for d in (self.state_dir, self.log_dir, self.data_cache_dir, Path("reports"), Path("config")):
            d.mkdir(parents=True, exist_ok=True)


_TRADING_KNOB_FIELDS: tuple[str, ...] = (
    "execution_mode",
    "questrade_env",
    "questrade_account_number",
    "risk_pct_per_trade",
    "portfolio_heat_cap",
    "risk_model",
    "heat_aggregation",
    "daily_loss_limit_pct",
    "max_drawdown_kill_switch",
    "max_open_positions",
    "min_ticket_usd",
    "default_strategy",
    "default_symbols",
    "timezone",
    "account_currency",
    "autonomous_enabled",
    "autonomous_account",
    "autonomous_interval_seconds",
    "autonomous_strategy",
    "autonomous_symbols",
    "autonomous_daily_max_trades",
    "autonomous_daily_max_notional_usd",
    "autonomous_auto_start_on_session",
    "log_level",
    "state_dir",
    "log_dir",
    "data_cache_dir",
)


def _resolve_yaml_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    override = os.environ.get(TRADING_YAML_ENV_VAR, "").strip()
    return Path(override) if override else DEFAULT_TRADING_YAML


def _load_trading_yaml(path: Path | None = None) -> dict[str, Any]:
    resolved = _resolve_yaml_path(path)
    if not resolved.exists():
        return {}
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return {k: v for k, v in data.items() if k in _TRADING_KNOB_FIELDS}


def write_trading_yaml(updates: dict[str, Any], path: Path | None = None) -> None:
    """Merge `updates` into the existing trading.yaml and write atomically."""
    resolved = _resolve_yaml_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if resolved.exists():
        try:
            current = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            current = {}
    merged = {**current, **updates}
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False, default_flow_style=False), encoding="utf-8")
    tmp.replace(resolved)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    yaml_data = _load_trading_yaml()
    s = Settings(**yaml_data)
    s.ensure_dirs()
    return s
