"""Asset-class calibration — translate a symbol's spec into strategy parameters.

The strategies library ships with hard-coded defaults tuned on the TSX equity basket
(``BollingerMeanRevert(window=20, n_std=2.0)`` and friends). Those numbers are wrong for
crypto (needs much wider bands and shorter windows), wrong for FX (spreads are 5x tighter,
half-life is a fraction of an equity's), and wrong for long-duration treasuries (vol is
low but auto-correlation runs weeks long). This module carries the per-class knobs those
overrides need and turns a symbol into a calibrated strategy instance without touching the
strategy classes themselves.

The design is deliberately data-driven — one :class:`CalibrationProfile` per asset class,
refined by the concrete :class:`~trading_live_claude.analysis.asset_spec.AssetSpec` subclass
fields (a treasury's duration, a future's exchange group, an FX pair's leg majors) — and
one dispatch table :data:`_CALIBRATORS` keyed by strategy name. Adding a strategy is one
table entry; adding an asset class is one row in :data:`_CLASS_DEFAULTS` plus an optional
refinement in :func:`profile_for`. Nothing in the strategies package needs to change.

Contract:

* :func:`profile_for(symbol)` returns the calibrated :class:`CalibrationProfile` for that
  symbol. Never raises; unknown symbols fall through to the equity default.
* :func:`calibrate_for(strategy_name, symbol)` returns a live :class:`Strategy` instance
  with parameters translated from the profile. Unknown strategy names raise KeyError;
  strategies without a calibration entry return a stock default instance (safe fallback).
* :func:`confirm_patterns_for(symbol)` returns the bullish-reversal pattern tuple appropriate
  for the symbol's asset class — gap-dependent patterns are filtered out for 24/7 markets
  (FX, crypto) where daily-bar gaps are near-zero and the pattern would practically never
  fire.

Calibration knobs cover: drift regime (trending vs mean-reverting), typical annualized
volatility, mean-reversion half-life on the native bar, typical round-trip spread cost,
liquidity tier, trading session (regular / extended / 24-7), whether the asset shows
meaningful bar-to-bar gaps, and a ``bar_scale`` multiplier for indicator windows (fast
assets like crypto/FX get shorter windows; slow assets like long-duration bonds get
longer). Every calibrator uses only a subset — the profile is the full menu, individual
strategies pick what they need.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trading_live_claude.analysis.asset_spec import (
    AssetSpec,
    CryptoSpec,
    FixedIncomeSpec,
    FutureSpec,
    FXSpec,
    spec_for,
)
from trading_live_claude.intel.overlay import OverlayClass

DriftRegime = Literal["trending", "mean_reverting", "mixed", "flat"]
LiquidityTier = Literal["tier1", "tier2", "tier3", "illiquid"]
Session = Literal["regular", "extended", "twentyfourseven"]


@dataclass(frozen=True, kw_only=True)
class CalibrationProfile:
    """Per-symbol calibration knobs that translate an asset's microstructure into strategy
    parameters. Every field is expressed on the native bar scale (daily by default).

    * ``typical_annual_vol`` — annualized realized volatility, e.g. ~0.20 for SPY,
      ~0.75 for BTC, ~0.08 for a G10 FX major, ~0.06 for SHY. Drives band widths.
    * ``drift_regime`` — informs which strategy families are a-priori suitable and how
      aggressive their entry thresholds should be.
    * ``typical_half_life_bars`` — OU half-life of price/spread deviations on the native
      bar. Drives rolling-window sizes; short (crypto/FX) means faster indicators,
      long (long-duration bonds) means slower.
    * ``typical_spread_bps`` — round-trip cost proxy; the ratio of spread to typical
      excursion sets the minimum z-score at which a mean-reversion entry survives cost.
    * ``liquidity_tier`` — coarse tiering used to gate size and skip low-tier names.
    * ``session`` — regular exchange hours vs 24/7 vs futures-extended; changes whether
      candlestick gap patterns are meaningful.
    * ``supports_gaps`` — True for exchange-traded (equities, futures) where daily bars
      routinely gap; False for 24/7 markets where the "gap" is a rounding artefact.
    * ``bar_scale`` — global window multiplier. 1.0 is baseline equity; ~0.6 for crypto
      (faster), ~1.5 for long-duration bonds (slower).
    """
    typical_annual_vol: float
    drift_regime: DriftRegime
    typical_half_life_bars: float
    typical_spread_bps: float
    liquidity_tier: LiquidityTier
    session: Session
    supports_gaps: bool
    bar_scale: float


# ---- per-class base defaults ------------------------------------------------
# Numbers below are pre-refinement — the base profile for a "typical" member of each
# overlay class. Subclass fields (bond duration, futures venue, crypto majors) refine
# these in profile_for(). Sourced from realized-vol on the FRM-Claude walk-forward
# universe (Sep 2026) rounded to two decimals, and from the sweep_ib probe for spreads.

_CLASS_DEFAULTS: dict[OverlayClass, CalibrationProfile] = {
    "equity": CalibrationProfile(
        typical_annual_vol=0.20, drift_regime="trending",
        typical_half_life_bars=15.0, typical_spread_bps=5.0,
        liquidity_tier="tier1", session="regular",
        supports_gaps=True, bar_scale=1.0,
    ),
    "fixed_income": CalibrationProfile(
        typical_annual_vol=0.08, drift_regime="mean_reverting",
        typical_half_life_bars=45.0, typical_spread_bps=3.0,
        liquidity_tier="tier1", session="regular",
        supports_gaps=True, bar_scale=1.5,
    ),
    "precious_metals": CalibrationProfile(
        typical_annual_vol=0.16, drift_regime="trending",
        typical_half_life_bars=25.0, typical_spread_bps=6.0,
        liquidity_tier="tier1", session="regular",
        supports_gaps=True, bar_scale=1.2,
    ),
    "commodity": CalibrationProfile(
        typical_annual_vol=0.30, drift_regime="mean_reverting",
        typical_half_life_bars=15.0, typical_spread_bps=8.0,
        liquidity_tier="tier2", session="regular",
        supports_gaps=True, bar_scale=0.9,
    ),
    "future": CalibrationProfile(
        typical_annual_vol=0.20, drift_regime="trending",
        typical_half_life_bars=12.0, typical_spread_bps=2.0,
        liquidity_tier="tier1", session="extended",
        supports_gaps=True, bar_scale=1.0,
    ),
    "crypto": CalibrationProfile(
        typical_annual_vol=0.75, drift_regime="mixed",
        typical_half_life_bars=8.0, typical_spread_bps=20.0,
        liquidity_tier="tier2", session="twentyfourseven",
        supports_gaps=False, bar_scale=0.6,
    ),
    "fx": CalibrationProfile(
        typical_annual_vol=0.09, drift_regime="mean_reverting",
        typical_half_life_bars=6.0, typical_spread_bps=1.0,
        liquidity_tier="tier1", session="twentyfourseven",
        supports_gaps=False, bar_scale=0.8,
    ),
}


# G10 FX majors — the tight-spread, tier-1 legs. Crosses (EURGBP, EURJPY, EURCAD, EURCHF)
# and EM pairs get wider spreads and lower liquidity in the refinement below.
_FX_MAJORS: frozenset[str] = frozenset({
    "EURUSD", "USDJPY", "GBPUSD", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
})

# Crypto majors that behave like tier-1 for our purposes — BTC/ETH have institutional
# flow, tight spreads on Kraken, and lower realized vol than the alts.
_CRYPTO_MAJORS: frozenset[str] = frozenset({
    "XBT", "BTC", "ETH", "XETH", "XXBT",
})

# Energy futures roots — the mean-reverting side of the futures complex (contango/
# backwardation cycles, weather-driven excursions). Rates + equity index stay trending.
_FUT_ENERGY: frozenset[str] = frozenset({"CL", "NG", "HO", "RB", "BZ"})
_FUT_RATES: frozenset[str] = frozenset({"ZN", "ZB", "ZF", "ZT", "GE"})

# Candlestick patterns that require meaningful bar-to-bar gaps. On 24/7 markets these
# patterns fire on rounding noise or effectively never, so ConfirmOverlay excludes them
# when the profile says supports_gaps=False.
_GAP_DEPENDENT_PATTERNS: frozenset[str] = frozenset({
    "morning_star", "evening_star",
    "morning_doji_star", "evening_doji_star",
    "abandoned_baby_bull", "abandoned_baby_bear",
    "tri_star",
    "bullish_kicker", "bearish_kicker",
    "piercing_line", "dark_cloud_cover",  # need prev-bar low/high gap
})


def profile_for(symbol: str) -> CalibrationProfile:
    """Resolve a symbol to its :class:`CalibrationProfile`.

    Starts from the class-default profile, then applies concrete-spec refinements: bond
    duration scales vol and half-life, futures venue rewrites the drift regime, FX/crypto
    majors move to tier-1. Never raises; unknown symbols fall through to the equity default
    matching :func:`~trading_live_claude.analysis.asset_spec.spec_for`'s behaviour.
    """
    spec = spec_for(symbol)
    return _refine(_CLASS_DEFAULTS[spec.asset_class], spec)


def _refine(base: CalibrationProfile, spec: AssetSpec) -> CalibrationProfile:
    """Apply concrete-spec-driven refinements to a base class profile."""
    if isinstance(spec, FixedIncomeSpec) and spec.duration_years > 0:
        # Long-duration treasuries carry 3x the vol of short-duration, and their price
        # auto-correlation stretches with duration (rates shocks propagate over weeks).
        d = spec.duration_years
        vol = max(0.03, min(0.20, 0.03 + 0.008 * d))
        half_life = max(15.0, min(70.0, 45.0 * (d / 6.0)))
        bar_scale = 1.2 + min(0.6, 0.03 * d)
        return CalibrationProfile(
            typical_annual_vol=vol, drift_regime=base.drift_regime,
            typical_half_life_bars=half_life, typical_spread_bps=base.typical_spread_bps,
            liquidity_tier=base.liquidity_tier, session=base.session,
            supports_gaps=base.supports_gaps, bar_scale=bar_scale,
        )
    if isinstance(spec, FutureSpec):
        root = spec.root.upper()
        if root in _FUT_ENERGY:
            return _replace(base, drift_regime="mean_reverting", typical_annual_vol=0.35)
        if root in _FUT_RATES:
            return _replace(base, drift_regime="mean_reverting", typical_annual_vol=0.06,
                            typical_half_life_bars=25.0, bar_scale=1.3)
    if isinstance(spec, CryptoSpec):
        if spec.base.upper() in _CRYPTO_MAJORS:
            return _replace(base, liquidity_tier="tier1", typical_annual_vol=0.60,
                            typical_spread_bps=8.0)
    if isinstance(spec, FXSpec):
        pair = (spec.base + spec.quote).upper()
        if pair in _FX_MAJORS:
            return base  # class default already tuned for majors
        # Crosses — wider vol, wider spread, longer half-life (less liquid mean-reversion).
        return _replace(base, typical_annual_vol=0.11, typical_spread_bps=2.5,
                        typical_half_life_bars=9.0, liquidity_tier="tier2")
    return base


def _replace(p: CalibrationProfile, **kw: object) -> CalibrationProfile:
    """dataclasses.replace without importing it — kw_only frozen dataclass friendly."""
    fields = {
        "typical_annual_vol": p.typical_annual_vol,
        "drift_regime": p.drift_regime,
        "typical_half_life_bars": p.typical_half_life_bars,
        "typical_spread_bps": p.typical_spread_bps,
        "liquidity_tier": p.liquidity_tier,
        "session": p.session,
        "supports_gaps": p.supports_gaps,
        "bar_scale": p.bar_scale,
    }
    fields.update(kw)
    return CalibrationProfile(**fields)  # type: ignore[arg-type]


# ---- per-strategy calibrators ----------------------------------------------
# Each returns the kwargs to pass to that strategy's __init__, given a profile. Strategies
# not in the table get called with no args (safe stock default).

def _calibrate_bollinger(p: CalibrationProfile) -> dict[str, object]:
    window = max(10, int(round(2.0 * p.typical_half_life_bars)))
    n_std = 2.0
    if p.drift_regime == "trending":
        n_std = 2.5  # avoid mean-reverting into a trend
    elif p.drift_regime == "mean_reverting":
        n_std = 1.8  # reversion is reliable — enter sooner
    n_std *= min(1.6, max(0.8, p.typical_annual_vol / 0.20))
    return {"window": window, "n_std": round(n_std, 2)}


def _calibrate_rsi_meanrevert(p: CalibrationProfile) -> dict[str, object]:
    window = max(5, int(round(p.typical_half_life_bars)))
    if p.drift_regime == "mean_reverting":
        oversold = 35.0
    elif p.drift_regime == "trending":
        oversold = 25.0
    else:
        oversold = 30.0
    return {"window": window, "oversold": oversold}


def _calibrate_zscore_ou(p: CalibrationProfile) -> dict[str, object]:
    window = max(10, int(round(3.0 * p.typical_half_life_bars)))
    # Ratio of round-trip cost to typical bar excursion (~vol/sqrt(252) in bps of price)
    # sets the minimum z-score that pays after cost. Larger for crypto, smaller for FX.
    bar_bps = max(5.0, p.typical_annual_vol / 0.16 * 100.0)
    entry_z = round(max(0.8, min(3.0, 1.5 + 1.5 * (p.typical_spread_bps / bar_bps))), 2)
    return {"window": window, "entry_z": entry_z}


def _calibrate_ema_crossover(p: CalibrationProfile) -> dict[str, object]:
    fast = max(5, int(round(10 * p.bar_scale)))
    slow = max(fast * 2, int(round(30 * p.bar_scale)))
    return {"fast": fast, "slow": slow}


def _calibrate_donchian(p: CalibrationProfile) -> dict[str, object]:
    scale = 1.3 if p.drift_regime == "trending" else 0.7
    window = max(10, int(round(20 * p.bar_scale * scale)))
    return {"window": window}


def _calibrate_candlestick(p: CalibrationProfile) -> dict[str, object]:
    exit_ma = max(5, int(round(0.7 * p.typical_half_life_bars)))
    atr_window = max(7, int(round(14 * p.bar_scale)))
    return {"exit_ma": exit_ma, "atr_window": atr_window}


_CALIBRATORS: dict[str, Callable[[CalibrationProfile], dict[str, object]]] = {
    "bollinger": _calibrate_bollinger,
    "rsi_meanrevert": _calibrate_rsi_meanrevert,
    "zscore_ou": _calibrate_zscore_ou,
    "ema_crossover": _calibrate_ema_crossover,
    "momentum_breakout": _calibrate_donchian,
    "candlestick": _calibrate_candlestick,
}


def calibrated_kwargs(strategy_name: str, symbol: str) -> dict[str, object]:
    """Return the kwargs a strategy's constructor should receive for this symbol.

    Handles two families that the base table can't key by exact name:

    * ``candle_<pattern>`` — every bullish-pattern wrapper reuses the same base
      candlestick calibration (exit-MA + ATR window), so they all route to the
      ``candlestick`` calibrator regardless of which pattern the wrapper carries.
    * ``confirm_<base>`` — the ConfirmOverlay wrappers accept a ``symbol`` kwarg so
      the overlay can filter its confirmation-pattern set for the asset's session
      (gap-dependent patterns dropped on 24/7 markets). Pass through the symbol; the
      wrapper does the filtering itself.

    Empty dict for strategies without a calibrator (safe: caller uses stock defaults).
    """
    if strategy_name.startswith("candle_"):
        return _CALIBRATORS["candlestick"](profile_for(symbol))
    if strategy_name.startswith("confirm_"):
        return {"symbol": symbol}
    calibrator = _CALIBRATORS.get(strategy_name)
    if calibrator is None:
        return {}
    return calibrator(profile_for(symbol))


def calibrate_for(strategy_name: str, symbol: str):  # -> Strategy
    """Instantiate ``strategy_name`` with parameters calibrated for ``symbol``.

    Imported lazily to avoid a circular import (strategies -> calibration -> strategies).
    Unknown strategy names raise :class:`KeyError`; strategies without a calibrator get a
    stock default instance so the caller always gets something usable.
    """
    from trading_live_claude.strategies import STRATEGIES

    cls = STRATEGIES[strategy_name]
    kwargs = calibrated_kwargs(strategy_name, symbol)
    try:
        return cls(**kwargs)
    except TypeError:
        # Some strategies (candlestick pattern variants) take fewer kwargs than the base
        # calibrator produces — fall back to a stock instance in that case.
        return cls()


def confirm_patterns_for(symbol: str, base: tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Filter a bullish-reversal pattern tuple for a symbol's asset class.

    Removes gap-dependent patterns for 24/7 markets (crypto, FX) where they cannot
    meaningfully fire on daily bars. For exchange-traded assets the base tuple is
    returned unchanged.
    """
    from trading_live_claude.strategies.overlay import REVERSAL_CONFIRM

    patterns = base if base is not None else REVERSAL_CONFIRM
    p = profile_for(symbol)
    if p.supports_gaps:
        return tuple(patterns)
    return tuple(x for x in patterns if x not in _GAP_DEPENDENT_PATTERNS)


__all__ = [
    "CalibrationProfile",
    "DriftRegime", "LiquidityTier", "Session",
    "profile_for",
    "calibrated_kwargs",
    "calibrate_for",
    "confirm_patterns_for",
]
