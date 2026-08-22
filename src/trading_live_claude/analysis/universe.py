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
