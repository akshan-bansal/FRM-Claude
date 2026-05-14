from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from trading_live_claude.risk import KillSwitch, PortfolioHeat, PositionSizer, historical_var


def test_position_sizer_floors_shares() -> None:
    sizer = PositionSizer(risk_pct=0.01, atr_multiple=2.0)
    result = sizer.size(equity=100_000, entry=100.0, atr_value=2.0, side="long")
    # dollar_risk = 1000; stop_distance = 4; shares = 250 floored
    assert result.shares == 250
    assert result.stop == pytest.approx(96.0)
    assert result.target == pytest.approx(108.0)


def test_position_sizer_rejects_high_risk_pct() -> None:
    with pytest.raises(ValueError):
        PositionSizer(risk_pct=0.2)


def test_portfolio_heat_breach() -> None:
    heat = PortfolioHeat(cap_pct=0.05)
    snap = heat.snapshot(equity=100_000, open_risk_dollars=6_000)
    assert snap.breached is True
    snap2 = heat.snapshot(equity=100_000, open_risk_dollars=4_999)
    assert snap2.breached is False


def test_historical_var() -> None:
    returns = pd.Series([-0.01, -0.02, -0.03, -0.04, -0.05, 0.01, 0.02, 0.03, 0.04, 0.05])
    var_95 = historical_var(returns, 0.95)
    assert var_95 > 0


def test_kill_switch_trip_and_clear(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path)
    assert ks.state().halted is False
    ks.trip("test")
    assert ks.state().halted is True
    with pytest.raises(PermissionError):
        ks.clear("not the magic phrase")
    ks.clear("I HAVE INVESTIGATED")
    assert ks.state().halted is False


def test_kill_switch_evaluates_drawdown(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path, max_drawdown_pct=0.10)
    state = ks.evaluate(equity=85_000, peak_equity=100_000, day_open_equity=100_000)
    assert state.halted is True
    assert "max-drawdown" in state.reason


def test_kill_switch_evaluates_daily_loss(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path, daily_loss_limit_pct=0.03)
    state = ks.evaluate(equity=96_000, peak_equity=100_000, day_open_equity=100_000)
    assert state.halted is True
    assert "daily-loss" in state.reason
