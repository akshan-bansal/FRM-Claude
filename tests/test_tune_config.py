from __future__ import annotations

from pathlib import Path

import pytest

from trading_live_claude.config.settings import (
    DEFAULT_TRADING_YAML,
    Settings,
    _load_trading_yaml,
    write_trading_yaml,
)
from trading_live_claude.tune import TuneResult, pick_config


def test_write_then_load_yaml_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_path = tmp_path / "trading.yaml"
    write_trading_yaml(
        {
            "default_strategy": "bollinger",
            "default_symbols": "XIC.TO,VOO",
            "autonomous_interval_seconds": 600,
        },
        path=yaml_path,
    )
    loaded = _load_trading_yaml(yaml_path)
    assert loaded["default_strategy"] == "bollinger"
    assert loaded["default_symbols"] == "XIC.TO,VOO"
    assert loaded["autonomous_interval_seconds"] == 600


def test_yaml_unknown_keys_filtered(tmp_path: Path) -> None:
    yaml_path = tmp_path / "trading.yaml"
    yaml_path.write_text(
        "default_strategy: bollinger\nunknown_field: dangerous\n", encoding="utf-8"
    )
    loaded = _load_trading_yaml(yaml_path)
    assert "default_strategy" in loaded
    assert "unknown_field" not in loaded


def test_write_yaml_merges_with_existing(tmp_path: Path) -> None:
    yaml_path = tmp_path / "trading.yaml"
    write_trading_yaml({"default_strategy": "macd"}, path=yaml_path)
    write_trading_yaml({"default_symbols": "AAPL"}, path=yaml_path)
    loaded = _load_trading_yaml(yaml_path)
    assert loaded["default_strategy"] == "macd"
    assert loaded["default_symbols"] == "AAPL"


def _r(strategy: str, symbol: str, sharpe: float, dd: float, trades: int = 20) -> TuneResult:
    return TuneResult(
        strategy=strategy,
        symbol=symbol,
        sharpe=sharpe,
        max_drawdown=dd,
        cagr=0.05,
        win_rate=0.6,
        num_trades=trades,
        score=sharpe / abs(dd) if dd else sharpe,
    )


def test_pick_config_filters_min_trades() -> None:
    results = [_r("bollinger", "X", 1.5, -0.05, trades=3)]
    assert pick_config(results, min_trades=15) is None


def test_pick_config_filters_deep_drawdown() -> None:
    results = [_r("bollinger", "X", 1.5, -0.5, trades=30)]
    assert pick_config(results, max_drawdown_cap=-0.20) is None


def test_pick_config_picks_top_3_symbols_same_strategy() -> None:
    results = [
        _r("bollinger", "A", 1.5, -0.05),
        _r("bollinger", "B", 1.2, -0.07),
        _r("bollinger", "C", 1.1, -0.08),
        _r("bollinger", "D", 0.9, -0.10),
        _r("rsi_meanrevert", "Z", 1.4, -0.06),
    ]
    update = pick_config(results)
    assert update is not None
    assert update["default_strategy"] == "bollinger"
    assert update["default_symbols"] == "A,B,C"
    assert update["autonomous_strategy"] == "bollinger"
    assert "last_tune" in update
