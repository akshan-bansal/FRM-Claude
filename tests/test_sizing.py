from __future__ import annotations

import pytest

from trading_live_claude.risk.sizing import PositionSizer


def test_conviction_scales_the_risk_budget() -> None:
    s = PositionSizer(risk_pct=0.01, atr_multiple=2.0)
    full = s.size(equity=100_000, entry=100, atr_value=2.0).shares
    half = s.size(equity=100_000, entry=100, atr_value=2.0, conviction=0.5).shares
    assert full == 250          # 1000 risk / 4 stop distance
    assert half == 125          # conviction halves the risk budget


def test_conviction_clipped_to_allocator_boost_ceiling() -> None:
    # Ceiling is 3.0 (matches the weight_bias cap in monitor/live_loop.py) so an
    # allocator boost of 2.0x or 3.0x actually amplifies size; earlier the [0, 1]
    # clip silently discarded every allocator boost above unity.
    s = PositionSizer()
    base = s.size(equity=100_000, entry=100, atr_value=2.0).shares
    boosted = s.size(equity=100_000, entry=100, atr_value=2.0, conviction=2.0).shares
    ceiling = s.size(equity=100_000, entry=100, atr_value=2.0, conviction=5.0).shares
    assert boosted == base * 2
    assert ceiling == base * 3               # clipped at ceiling, not silently discarded
    assert s.size(equity=100_000, entry=100, atr_value=2.0, conviction=-1.0).shares == 0


def test_vol_target_leverage_cap_still_binds_over_conviction_boost() -> None:
    # Even with conviction=3.0 the vol-target path is capped at max_leverage, so runaway
    # boost never escapes the leverage ceiling — the safety guard we kept when raising
    # the conviction clip.
    s = PositionSizer()
    r = s.size_vol_target(equity=100_000, price=100, annual_vol=0.30, target_vol=0.15,
                          conviction=3.0, max_leverage=1.0)
    assert r.vol_scale == 1.0                # 0.5 base scale × 3.0 conviction = 1.5 → capped at 1.0


def test_vol_target_sizes_inverse_to_volatility() -> None:
    s = PositionSizer()
    lo = s.size_vol_target(equity=100_000, price=100, annual_vol=0.10, target_vol=0.15)
    hi = s.size_vol_target(equity=100_000, price=100, annual_vol=0.60, target_vol=0.15)
    assert lo.shares > hi.shares          # a calmer name gets a bigger position
    assert hi.shares == 250               # 0.15/0.60 = 0.25 scale → $25k → 250 sh
    assert lo.vol_scale == 1.0            # 0.15/0.10 = 1.5 capped at max_leverage 1.0


def test_vol_target_leverage_cap_and_conviction() -> None:
    s = PositionSizer()
    assert s.size_vol_target(equity=100_000, price=100, annual_vol=0.05).vol_scale == 1.0
    conv = s.size_vol_target(equity=100_000, price=100, annual_vol=0.30, target_vol=0.15, conviction=0.5)
    assert conv.shares == 250             # 0.5 scale x 0.5 conviction = 0.25 → 250 sh


def test_size_uses_vol_targeting_when_annual_vol_given() -> None:
    s = PositionSizer()
    # annual_vol path: notional = equity x (0.15/0.30) x conviction, shares = notional/entry.
    r = s.size(equity=100_000, entry=100, atr_value=2.0, annual_vol=0.30)
    assert r.shares == 500                       # 0.5 scale → $50k → 500 sh
    assert r.stop == 96.0 and r.target == 108.0  # ATR stop/target still defined for the gate
    # Conviction scales the vol-targeted count too.
    assert s.size(equity=100_000, entry=100, atr_value=2.0, annual_vol=0.30, conviction=0.5).shares == 250
    # Omitting annual_vol falls back to fixed-fractional (unchanged).
    assert s.size(equity=100_000, entry=100, atr_value=2.0).shares == 250


def test_vol_target_rejects_bad_inputs() -> None:
    s = PositionSizer()
    for bad in (dict(equity=0, price=100, annual_vol=0.2), dict(equity=1e5, price=100, annual_vol=0.0)):
        with pytest.raises(ValueError):
            s.size_vol_target(**bad)  # type: ignore[arg-type]
