from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.models.strategy_risk import (
    StrategyRiskConfig,
    StrategyRiskModel,
    build_features,
    forward_drawdown_label,
)


def _regime_returns(seed: int = 0, blocks: int = 26, block: int = 60) -> pd.Series:
    """Alternating calm/stormy blocks: storms carry high vol and negative drift (drawdowns follow
    a run-up in volatility), so trailing vol is genuinely predictive of forward drawdown."""
    rng = np.random.RandomState(seed)
    out: list[float] = []
    for b in range(blocks):
        if b % 2 == 0:
            out += list(rng.normal(0.0008, 0.004, block))   # calm
        else:
            out += list(rng.normal(-0.0015, 0.020, block))  # stormy
    idx = pd.date_range("2020-01-01", periods=len(out), freq="B", tz="UTC")
    return pd.Series(out, index=idx)


def test_forward_label_flags_a_coming_drawdown() -> None:
    r = pd.Series([0.0] * 5 + [-0.03] * 5 + [0.0] * 20)   # a -14% run starting at index 5
    lab = forward_drawdown_label(r, horizon=10, dd_threshold=0.05)
    assert lab.iloc[4] == 1.0    # drawdown lies just ahead
    assert lab.iloc[20] == 0.0   # flat ahead -> no event


def test_features_are_causal_no_lookahead() -> None:
    r = _regime_returns(seed=1)
    full = build_features(r)
    for t in (300, 600, 900):
        truncated = build_features(r.iloc[: t + 1]).iloc[-1]
        pd.testing.assert_series_equal(full.iloc[t], truncated, check_names=False)


def test_scalar_stands_down_in_risky_periods() -> None:
    r = _regime_returns(seed=2)
    res = StrategyRiskModel(StrategyRiskConfig(step=21)).walk_forward(r)
    f = res.frame.dropna(subset=["scalar", "label"])
    risky = f[f["label"] == 1]["scalar"].mean()
    calm = f[f["label"] == 0]["scalar"].mean()
    assert risky < calm            # exposure is cut when a drawdown is actually coming
    assert f["scalar"].between(0.25, 1.0).all()


def test_walk_forward_has_out_of_sample_skill() -> None:
    r = _regime_returns(seed=3)
    res = StrategyRiskModel().walk_forward(r)
    assert res.n_events > 0
    assert res.oos_auc > 0.55      # beats a coin flip on a learnable signal
    assert not np.isnan(res.baseline_auc)


def test_fit_latest_returns_a_live_scalar() -> None:
    r = _regime_returns(seed=4)
    _, scalar = StrategyRiskModel().fit_latest(r)
    assert 0.25 <= scalar <= 1.0


def test_auc_matches_a_known_ranking() -> None:
    from trading_live_claude.models.strategy_risk import _auc
    # perfectly separable: all positives score above all negatives -> AUC 1.0
    y = np.array([0, 0, 1, 1])
    s = np.array([0.1, 0.2, 0.8, 0.9])
    assert abs(_auc(y, s) - 1.0) < 1e-9
    assert abs(_auc(y, -s) - 0.0) < 1e-9   # reversed -> 0.0


def _osint(scalar: float, halt: bool = False):
    from trading_live_claude.intel.overlay import OverlayDecision
    return OverlayDecision(asset_class="equity", scalar=scalar, halt_new_entries=halt,
                           reasons=["elevated global news alerts"], components={})


def test_combine_multiplies_both_layers_and_cites_each() -> None:
    from trading_live_claude.models.risk_mitigation import combine
    m = combine(0.5, _osint(0.5))
    assert abs(m.scalar - 0.25) < 1e-9                   # the product of the two layers
    assert m.strategy_scalar == 0.5 and m.osint_scalar == 0.5
    assert len(m.reasons) == 2                           # both layers cited
    # halt_below is 0.20, so a combined 0.25 trims hard but does NOT stand the book down.
    assert not m.halt


def test_combine_halts_only_at_the_floor_now() -> None:
    """With halt_below lowered to 0.20, halting needs the product at the combine() floor."""
    from trading_live_claude.models.risk_mitigation import combine
    assert combine(0.25, _osint(0.25)).halt              # 0.0625 -> clipped to floor 0.20 -> halts
    assert not combine(0.6, _osint(0.5)).halt            # 0.30 -> trims only


def test_combine_without_osint_is_just_the_ai_scalar() -> None:
    from trading_live_claude.models.risk_mitigation import combine
    m = combine(0.8, None)
    assert m.scalar == 0.8 and not m.halt and m.osint_scalar == 1.0


def test_combine_halts_when_osint_class_halts() -> None:
    from trading_live_claude.models.risk_mitigation import combine
    m = combine(1.0, _osint(0.6, halt=True))   # AI calm, but OSINT halts the class
    assert m.halt


def test_vol_risk_scalar_cuts_exposure_in_high_vol() -> None:
    from trading_live_claude.models.strategy_risk import vol_risk_scalar
    r = _regime_returns(seed=5)
    sc = vol_risk_scalar(r)
    assert sc.between(0.25, 1.0).all()
    # stormy blocks (odd 60-bar blocks) carry higher vol -> lower scalar than calm blocks
    block = 60
    calm = pd.concat([sc.iloc[b * block:(b + 1) * block] for b in range(2, 26, 2)]).mean()
    storm = pd.concat([sc.iloc[b * block:(b + 1) * block] for b in range(3, 26, 2)]).mean()
    assert storm < calm


def test_strategy_risk_gate_cannot_halt_on_its_own() -> None:
    """Documented invariant: the vol rule only trims; halting is the OSINT layer's job.

    Guards against a future retune of STRATEGY_RISK_FLOOR silently giving the volatility rule
    halt authority it was never validated for.
    """
    from trading_live_claude.models.risk_mitigation import combine
    from trading_live_claude.models.strategy_risk import STRATEGY_RISK_FLOOR

    assert STRATEGY_RISK_FLOOR > 0.4, "floor must stay above combine()'s halt_below"
    assert not combine(STRATEGY_RISK_FLOOR, None).halt


def test_vol_risk_scalar_respects_the_configured_floor() -> None:
    """Pins the scalar to the real floor (0.75), not a loose bound that passes trivially."""
    from trading_live_claude.models.strategy_risk import STRATEGY_RISK_FLOOR, vol_risk_scalar
    sc = vol_risk_scalar(_regime_returns(seed=7))
    assert sc.min() >= STRATEGY_RISK_FLOOR - 1e-12
    assert sc.max() <= 1.0
    assert sc.min() < 0.95, "a regime-switching series should actually engage the gate"
