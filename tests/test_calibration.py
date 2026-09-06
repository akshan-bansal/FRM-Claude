"""Asset-class calibration — smoke tests.

Not tuning tests (the calibration numbers are heuristics, not identities to freeze). These
tests pin the *directional* invariants the calibrator promises: high-vol assets get wider
Bollinger bands than low-vol assets, mean-reverting classes get shorter windows than
trending ones, 24/7 markets have gap-dependent patterns filtered out, and unknown symbols
never raise. If a future re-calibration flips one of these, the calibrator is no longer
doing what the strategies expect and the test surfaces that clearly.
"""
from __future__ import annotations

from trading_live_claude.analysis.calibration import (
    CalibrationProfile,
    calibrate_for,
    calibrated_kwargs,
    confirm_patterns_for,
    profile_for,
)
from trading_live_claude.strategies.examples.bollinger import BollingerMeanRevert


def test_profile_defaults_are_class_appropriate() -> None:
    equity = profile_for("SPY")
    crypto = profile_for("XBT/USD")
    fx = profile_for("EURUSD")
    bond = profile_for("TLT")  # long duration
    short_bond = profile_for("SHY")

    # 24/7 markets carry the flag
    assert crypto.session == "twentyfourseven"
    assert fx.session == "twentyfourseven"
    assert equity.session == "regular"

    # Crypto vol dominates every other class
    assert crypto.typical_annual_vol > equity.typical_annual_vol > fx.typical_annual_vol

    # Long-duration bonds have higher vol + longer half-life than short-duration bonds
    assert bond.typical_annual_vol > short_bond.typical_annual_vol
    assert bond.typical_half_life_bars > short_bond.typical_half_life_bars


def test_bollinger_calibration_widens_bands_for_high_vol_assets() -> None:
    equity_kw = calibrated_kwargs("bollinger", "SPY")
    crypto_kw = calibrated_kwargs("bollinger", "XBT/USD")
    fx_kw = calibrated_kwargs("bollinger", "EURUSD")

    # Crypto (75% vol, mixed drift) should carry the widest bands
    assert crypto_kw["n_std"] > equity_kw["n_std"]
    # FX (9% vol, mean-reverting) should be tighter than equity (20% vol, trending)
    assert fx_kw["n_std"] < equity_kw["n_std"]
    # Windows scale with half-life: FX (6d) short, equity (15d) medium, crypto (8d) short
    assert fx_kw["window"] < equity_kw["window"]


def test_futures_venue_rewrites_drift_regime() -> None:
    # Energy futures flip to mean-reverting; equity index stays trending
    cl = profile_for("CL")
    es = profile_for("ES")
    zn = profile_for("ZN")

    assert cl.drift_regime == "mean_reverting"
    assert es.drift_regime == "trending"
    assert zn.drift_regime == "mean_reverting"
    # Rates carry very low vol vs energy
    assert zn.typical_annual_vol < cl.typical_annual_vol


def test_confirm_patterns_drops_gap_patterns_for_247_markets() -> None:
    equity_patterns = confirm_patterns_for("SPY")
    crypto_patterns = confirm_patterns_for("XBT/USD")
    fx_patterns = confirm_patterns_for("EURUSD")

    # 24/7 markets lose the gap-dependent piercing_line by construction
    assert "piercing_line" in equity_patterns
    assert "piercing_line" not in crypto_patterns
    assert "piercing_line" not in fx_patterns
    # But structural patterns (hammer, bullish_engulfing) survive on every class
    for tup in (equity_patterns, crypto_patterns, fx_patterns):
        assert "hammer" in tup
        assert "bullish_engulfing" in tup


def test_calibrate_for_returns_live_instance_with_calibrated_kwargs() -> None:
    inst = calibrate_for("bollinger", "XBT/USD")
    assert isinstance(inst, BollingerMeanRevert)
    # Kwargs actually reached the constructor
    expected = calibrated_kwargs("bollinger", "XBT/USD")
    assert inst.window == expected["window"]
    assert inst.n_std == expected["n_std"]


def test_unknown_symbol_falls_through_to_equity_default() -> None:
    unknown = profile_for("ZZZZZ-NOT-A-REAL-TICKER")
    reference = profile_for("SPY")
    # Same class defaults, no exception
    assert isinstance(unknown, CalibrationProfile)
    assert unknown.session == reference.session
    assert unknown.supports_gaps == reference.supports_gaps


def test_strategy_without_calibrator_returns_empty_kwargs() -> None:
    # Kalman pairs / arima_garch aren't in the calibrator table — empty dict is the contract
    assert calibrated_kwargs("kalman_pairs", "SPY") == {}
    assert calibrated_kwargs("arima_garch", "SPY") == {}


def test_confirm_overlay_filters_patterns_when_given_a_symbol() -> None:
    # ConfirmOverlay wrapped around a base strategy filters its pattern set for the
    # symbol's asset class. On 24/7 markets gap-dependent patterns are dropped.
    from trading_live_claude.strategies import STRATEGIES

    equity_confirm = STRATEGIES["confirm_bollinger"](symbol="SPY")
    crypto_confirm = STRATEGIES["confirm_bollinger"](symbol="XBT/USD")

    assert "piercing_line" in equity_confirm.patterns
    assert "piercing_line" not in crypto_confirm.patterns
    # Structural patterns always survive
    assert "hammer" in equity_confirm.patterns
    assert "hammer" in crypto_confirm.patterns


def test_calibrate_for_candle_wrapper_flows_calibrated_kwargs() -> None:
    # candle_<pattern> wrappers route through the candlestick calibrator, so exit_ma
    # and atr_window pick up the asset's half-life + bar-scale.
    hammer_crypto = calibrate_for("candle_hammer", "XBT/USD")
    hammer_bond = calibrate_for("candle_hammer", "TLT")
    # Long-duration bond has a much longer half-life → longer exit_ma
    assert hammer_bond.exit_ma > hammer_crypto.exit_ma


def test_calibrate_for_confirm_wrapper_flows_symbol_and_filters_patterns() -> None:
    # confirm_<base> wrappers accept a symbol; the wrapper filters its patterns
    equity_confirm = calibrate_for("confirm_bollinger", "SPY")
    fx_confirm = calibrate_for("confirm_bollinger", "EURUSD")

    assert "piercing_line" in equity_confirm.patterns
    assert "piercing_line" not in fx_confirm.patterns


def test_tune_uses_calibrated_strategy_instance() -> None:
    # Smoke: tune._one path should build a calibrated strategy without erroring for
    # any known strategy/symbol combo. Guards against a calibrator returning kwargs
    # a strategy constructor rejects.
    from trading_live_claude.strategies import STRATEGIES

    for strategy_name in ("bollinger", "rsi_meanrevert", "ema_crossover",
                          "momentum_breakout", "candle_hammer", "confirm_bollinger"):
        for symbol in ("SPY", "TLT", "XBT/USD", "EURUSD", "CL"):
            inst = calibrate_for(strategy_name, symbol)
            assert isinstance(inst, STRATEGIES[strategy_name])
