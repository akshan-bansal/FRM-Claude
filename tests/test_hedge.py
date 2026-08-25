from __future__ import annotations

import pytest

from trading_live_claude.risk.hedge import HedgePolicy, hedge_shares, hedge_weight, rebalance_delta


def test_zero_hedge_near_the_highs() -> None:
    assert hedge_weight(0.0) == 0.0
    assert hedge_weight(-0.03) == 0.0  # shallower than ramp_start (-0.05)


def test_ramps_to_cap_as_drawdown_deepens() -> None:
    # default ramp -0.05 -> -0.20, base 0 -> max 0.30; midpoint (-0.125) => 0.15.
    assert hedge_weight(-0.125) == pytest.approx(0.15)
    assert hedge_weight(-0.20) == pytest.approx(0.30)
    assert hedge_weight(-0.40) == pytest.approx(0.30)  # capped


def test_monotone_in_drawdown() -> None:
    ws = [hedge_weight(-d / 100) for d in range(0, 30, 2)]
    assert ws == sorted(ws)  # deeper drawdown never reduces the hedge


def test_heat_boost_adds_weight_capped() -> None:
    p = HedgePolicy(heat_boost=2.0, heat_ref=0.05, max_weight=0.30)
    # at -0.125 base is 0.15; heat 0.15 adds 2.0*(0.15-0.05)=0.20 -> capped at 0.30.
    assert hedge_weight(-0.125, policy=p, heat=0.15) == pytest.approx(0.30)
    assert hedge_weight(-0.125, policy=p, heat=0.05) == pytest.approx(0.15)  # heat at ref, no boost


def test_policy_validates() -> None:
    with pytest.raises(ValueError):
        HedgePolicy(base_weight=0.4, max_weight=0.3)
    with pytest.raises(ValueError):
        HedgePolicy(ramp_start=-0.2, ramp_full=-0.1)  # ramp_full must be deeper


def test_hedge_shares() -> None:
    assert hedge_shares(equity=100_000, hedge_price=28.0, target_weight=0.20) == 714  # floor(20000/28)
    assert hedge_shares(equity=100_000, hedge_price=28.0, target_weight=0.0) == 0


def test_rebalance_no_trade_band_and_close() -> None:
    assert rebalance_delta(700, 714, band=0.20) == 0        # within 20% band → no churn
    assert rebalance_delta(300, 714, band=0.20) == 414      # big drift → trade to target
    assert rebalance_delta(714, 0) == -714                  # target 0 → close the sleeve
