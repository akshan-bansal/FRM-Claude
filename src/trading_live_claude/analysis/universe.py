"""Universe selection — build the tradable symbol set per asset class.

Two parts:
  * **Seed lists** (`SEED_UNIVERSE`) — curated candidate symbols per asset class
    (equity/ETF, future, commodity, crypto), the starting pool.
  * **Screen** (`screen_universe`) — a pure filter over OHLCV frames that keeps only
    liquid, sanely-priced, sanely-volatile names, ranked by dollar volume.

Like ``analysis.matrix`` this is a pure function of the frames handed in, so it runs
and tests on synthetic data with no broker. The screened universe feeds the scoring
layer (`scoring.selection`) so ranking only ever sees tradable names.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

AssetClass = Literal["equity", "future", "commodity", "crypto"]

# Curated seed pools. Futures/commodities use liquid ETF proxies so the same
# equities data path works; swap for native continuous-future tickers under LEAN.
SEED_UNIVERSE: dict[AssetClass, tuple[str, ...]] = {
    "equity": (
        "XIC.TO", "VFV.TO", "XEF.TO", "VOO", "SPY", "QQQ", "IWM", "VTI",
        "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN", "RY.TO", "TD.TO",
        # Walk-forward-validated names (see WALK_FORWARD_VALIDATED below).
        "XLE", "SMH", "ARX.TO", "DFY.TO", "EQB.TO", "WCP.TO", "KEY.TO",
    ),
    "future": ("ES", "NQ", "YM", "RTY", "ZN", "ZB"),
    "commodity": ("GLD", "SLV", "USO", "UNG", "DBC", "CORN", "WEAT"),
    "crypto": ("BTCUSD", "ETHUSD", "SOLUSD", "ADAUSD", "XRPUSD"),
}

# Crypto trades ~365 days/yr; the rest use trading days for annualization.
_PERIODS_PER_YEAR: dict[AssetClass, int] = {
    "equity": 252, "future": 252, "commodity": 252, "crypto": 365,
}


def seed_symbols(asset_class: AssetClass) -> tuple[str, ...]:
    return SEED_UNIVERSE[asset_class]


@dataclass(frozen=True)
class UniverseFilter:
    """Screen thresholds. Defaults suit liquid equities; loosen for crypto/futures."""

    min_price: float = 5.0
    min_dollar_volume: float = 1_000_000.0
    min_annual_vol: float = 0.05
    max_annual_vol: float = 1.50


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    asset_class: str
    last_price: float
    avg_dollar_volume: float
    annual_vol: float


def _metrics(df: pd.DataFrame, periods_per_year: int, lookback: int) -> tuple[float, float, float] | None:
    """(last_price, avg_dollar_volume, annualized_vol) over the last ``lookback`` bars."""
    if df is None or df.empty or "close" not in df.columns:
        return None
    tail = df.tail(lookback)
    if len(tail) < 2:
        return None
    last_price = float(tail["close"].iloc[-1])
    adv = float((tail["close"] * tail["volume"]).mean()) if "volume" in tail.columns else 0.0
    rets = tail["close"].pct_change().dropna()
    annual_vol = float(rets.std(ddof=0) * np.sqrt(periods_per_year)) if not rets.empty else 0.0
    return last_price, adv, annual_vol


def screen_universe(
    frames: Mapping[str, pd.DataFrame],
    *,
    asset_class: AssetClass = "equity",
    filt: UniverseFilter | None = None,
    lookback: int = 60,
) -> list[UniverseMember]:
    """Keep only names passing price/liquidity/volatility filters, ranked by ADV desc."""
    f = filt or UniverseFilter()
    ppy = _PERIODS_PER_YEAR[asset_class]
    members: list[UniverseMember] = []
    for symbol, df in frames.items():
        m = _metrics(df, ppy, lookback)
        if m is None:
            continue
        last_price, adv, annual_vol = m
        if last_price < f.min_price:
            continue
        if adv < f.min_dollar_volume:
            continue
        if not (f.min_annual_vol <= annual_vol <= f.max_annual_vol):
            continue
        members.append(
            UniverseMember(
                symbol=symbol,
                asset_class=asset_class,
                last_price=last_price,
                avg_dollar_volume=adv,
                annual_vol=annual_vol,
            )
        )
    members.sort(key=lambda mem: mem.avg_dollar_volume, reverse=True)
    return members


def select_universe(
    frames: Mapping[str, pd.DataFrame],
    *,
    asset_class: AssetClass = "equity",
    top_n: int = 10,
    filt: UniverseFilter | None = None,
    lookback: int = 60,
) -> list[str]:
    """Screen then return the top ``top_n`` most-liquid symbols (just the tickers)."""
    screened = screen_universe(frames, asset_class=asset_class, filt=filt, lookback=lookback)
    return [m.symbol for m in screened[:top_n]]


# --------------------------------------------------------------------------- #
# Walk-forward-validated (strategy, params) recommendations
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WFValidated:
    """A walk-forward-validated (strategy, params) recommendation for a symbol.

    Produced by a rolling walk-forward (2-year train / 6-month test, parameters
    re-optimized on each fold, scored only out-of-sample) over the universe-search
    survivors. ``params`` are the full-history in-sample optimum kept as the recommended
    config; the ``oos_*`` fields and ``wfe`` (walk-forward efficiency = OOS score /
    in-sample score) summarize how the family held up on unseen data.

    ``tier`` is ``"robust"`` when the name cleared all three bars — WFE >= 0.5, positive
    out-of-sample score, and >= 10 out-of-sample trades (enough sample to trust the
    number). ``"watch"`` names are carried by explicit request despite thinner evidence
    (few OOS trades, or WFE below 0.5) and should be treated as candidates to watch, not
    as deploy-ready signals.
    """

    symbol: str
    asset_class: str
    strategy: str
    params: Mapping[str, float]
    oos_score: float
    wfe: float
    oos_return: float
    oos_max_drawdown: float
    oos_trades: int
    tier: str  # "robust" | "watch"


def _wf(
    symbol: str, strategy: str, params: Mapping[str, float], oos_score: float, wfe: float,
    oos_return: float, oos_max_drawdown: float, oos_trades: int, tier: str,
    asset_class: str = "equity",
) -> WFValidated:
    return WFValidated(
        symbol=symbol, asset_class=asset_class, strategy=strategy, params=params,
        oos_score=oos_score, wfe=wfe, oos_return=oos_return,
        oos_max_drawdown=oos_max_drawdown, oos_trades=oos_trades, tier=tier,
    )


# Symbol -> validated recommendation, ordered by out-of-sample score and truncated at
# KEY.TO (OOS 6.11) — every name scores at least as high. The 6 "robust" names cleared
# WFE>=0.5, OOS>0, and >=10 OOS trades; the 3 "watch" names are carried on thinner
# evidence (VFV.TO on a 2-trade sample; WCP.TO and KEY.TO on 5 OOS trades each).
WALK_FORWARD_VALIDATED: dict[str, WFValidated] = {
    "XLE": _wf("XLE", "rsi_meanrevert", {"window": 21, "oversold": 35}, 24.19, 2.53, 0.4189, -0.0487, 16, "robust"),
    "XIC.TO": _wf("XIC.TO", "rsi_meanrevert", {"window": 7, "oversold": 35}, 19.60, 1.21, 0.2946, -0.0505, 17, "robust"),
    "VFV.TO": _wf("VFV.TO", "high_52w_breakout", {"high_window": 126, "exit_window": 63}, 18.90, 1.07, 0.1590, -0.0660, 2, "watch"),
    "ARX.TO": _wf("ARX.TO", "bollinger", {"window": 20, "n_std": 3.0}, 8.82, 0.72, 0.5757, -0.1131, 15, "robust"),
    "EQB.TO": _wf("EQB.TO", "ts_momentum", {"lookback": 126, "threshold": 0.0}, 8.17, 1.36, 0.4588, -0.1605, 11, "robust"),
    "SMH": _wf("SMH", "ts_momentum", {"lookback": 189, "threshold": 0.0}, 7.10, 1.22, 2.3069, -0.2547, 14, "robust"),
    "WCP.TO": _wf("WCP.TO", "confirm_bollinger", {"window": 30, "n_std": 3.0}, 6.62, 0.17, 0.3289, -0.0882, 5, "watch"),
    "DFY.TO": _wf("DFY.TO", "rsi_meanrevert", {"window": 14, "oversold": 35}, 6.62, 0.65, 0.2544, -0.0974, 14, "robust"),
    "KEY.TO": _wf("KEY.TO", "rsi_meanrevert", {"window": 14, "oversold": 35}, 6.11, 0.63, 0.1700, -0.0710, 5, "watch"),
}


def validated_symbols(tier: str | None = None) -> tuple[str, ...]:
    """Walk-forward-validated tickers, optionally filtered to one ``tier``."""
    return tuple(
        v.symbol for v in WALK_FORWARD_VALIDATED.values() if tier is None or v.tier == tier
    )


def validated_for(symbol: str) -> WFValidated | None:
    """The validated recommendation for ``symbol``, or ``None`` if not validated."""
    return WALK_FORWARD_VALIDATED.get(symbol)
