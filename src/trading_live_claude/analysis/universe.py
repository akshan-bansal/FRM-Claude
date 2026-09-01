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
from dataclasses import dataclass, field
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
        "XLE", "ARX.TO", "DFY.TO", "EQB.TO", "WCP.TO", "KEY.TO",
        "XLB", "QQQ", "IWM", "RS", "DBA", "CEW.TO", "BTO.TO", "ZEB.TO",
        "FRU.TO", "TA.TO", "SRU.UN.TO", "CGL.TO", "ZUT.TO", "EFN.TO",
        "ZWB.TO", "GEI.TO", "XEI.TO", "CRT.UN.TO", "VALE", "DBC",
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
    out-of-sample score, and at least :func:`min_oos_trades` out-of-sample trades for its
    asset class (10 by default; 4 for commodities, which trade on a slower, roughly
    quarterly cycle). ``"watch"`` names are carried by explicit request despite thinner evidence
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


# Minimum out-of-sample trades a name must post to qualify as "robust". Commodities trade on a
# slower cycle than equities — a mean-reversion band on gold bullion or a broad commodity basket
# fires roughly quarterly, where the same rule on a bank stock fires monthly — so a flat 10-trade
# bar systematically excluded the entire class regardless of how well it held up out-of-sample.
# Commodities are therefore held to ~4 trades (about once a quarter over a one-year OOS span).
#
# This is a deliberate sensitivity trade-off, not a free pass: 4 trades is a thin sample, and a
# commodity name cleared on it carries materially weaker evidence than an equity cleared on 10+.
# The WFE and positive-OOS bars are unchanged for every class.
MIN_OOS_TRADES: dict[str, int] = {"commodity": 4}
DEFAULT_MIN_OOS_TRADES = 10


def min_oos_trades(asset_class: str) -> int:
    """Trade-count bar for the ``robust`` tier, by asset class."""
    return MIN_OOS_TRADES.get(asset_class, DEFAULT_MIN_OOS_TRADES)


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


# Symbol -> validated recommendation, ordered by out-of-sample score. "robust" names
# cleared WFE>=0.5, OOS>0, and >=10 OOS trades; "watch" names are carried on thinner
# evidence (VFV.TO on a 2-trade sample; WCP.TO and KEY.TO on 5 OOS trades each). The
# cyclical-sector, broad-index and seasonal names (XLE, XLB, QQQ, IWM, RS, DBA) come from
# the widened ~180-asset search and its walk-forward — the two standouts, XLE (energy) and
# XLB (materials), score higher out-of-sample than in-sample.
WALK_FORWARD_VALIDATED: dict[str, WFValidated] = {
    "XLE": _wf("XLE", "rsi_meanrevert", {"window": 7, "oversold": 35}, 28.39, 2.60, 0.4620, -0.0480, 24, "robust"),
    "XLB": _wf("XLB", "bollinger", {"window": 20, "n_std": 2.0}, 20.95, 1.22, 0.3580, -0.0550, 16, "robust"),
    "XIC.TO": _wf("XIC.TO", "rsi_meanrevert", {"window": 7, "oversold": 35}, 19.60, 1.21, 0.2946, -0.0505, 17, "robust"),
    "VFV.TO": _wf("VFV.TO", "high_52w_breakout", {"high_window": 126, "exit_window": 63}, 18.90, 1.07, 0.1590, -0.0660, 2, "watch"),
    # ZEB.TO (BMO equal-weight Canadian banks) cleared walk-forward on an ATR-channel breakout —
    # the first atr_channel name in the pool; OOS beat in-sample (WFE > 1), like XLE/XLB.
    "ZEB.TO": _wf("ZEB.TO", "atr_channel", {"ema_window": 30, "k": 1.5}, 16.749, 1.123, 0.6086, -0.0924, 27, "robust"),
    # FRU.TO (Freehold Royalties) cleared walk-forward from the $15-25 sweep on a tight Bollinger
    # band; low-drawdown energy-royalty name, OOS ~= in-sample (WFE ~1), the cleanest of that sweep.
    "FRU.TO": _wf("FRU.TO", "bollinger", {"window": 15, "n_std": 1.5}, 16.510, 1.020, 0.5420, -0.0590, 22, "robust"),
    # SRU.UN.TO (SmartCentres REIT) cleared walk-forward from the $25-35 sweep — the first REIT in
    # the pool; RSI mean-reversion, OOS beat in-sample (WFE ~1) at the tightest drawdown of that batch.
    "SRU.UN.TO": _wf("SRU.UN.TO", "rsi_meanrevert", {"window": 7, "oversold": 25}, 13.330, 1.030, 0.3110, -0.0490, 15, "robust"),
    # ZWB.TO (BMO covered-call Canadian banks) cleared walk-forward from the $25-40 sweep — OOS beat
    # in-sample (WFE 1.57), the covered-call sibling of ZEB.TO (equal-weight banks); correlated but a
    # different payoff. Second atr_channel banks-ETF in the pool.
    "ZWB.TO": _wf("ZWB.TO", "atr_channel", {"ema_window": 10, "k": 1.5}, 11.900, 1.570, 0.2770, -0.0710, 29, "robust"),
    # CEW.TO is a CAD currency-hedged basket (not a pure FX pair); it cleared walk-forward.
    "CEW.TO": _wf("CEW.TO", "bollinger", {"window": 20, "n_std": 2.0}, 10.93, 0.76, 0.2000, -0.0620, 12, "robust"),
    # ARX.TO (ARC Resources) UPGRADED by the $1-45 resweep: re-optimizing across all families
    # picked rsi_meanrevert over the earlier bollinger config and beat it out-of-sample on every
    # axis (OOS 12.38 vs 8.82, WFE 1.30 vs 0.72, drawdown -6.3% vs -11.3%) on a comparable trade
    # count. Prior config kept here for provenance: bollinger {window:20, n_std:3.0}, OOS 8.82.
    "ARX.TO": _wf("ARX.TO", "rsi_meanrevert", {"window": 21, "oversold": 30}, 12.375, 1.301, 0.0467, -0.0625, 12, "robust"),
    # BTO.TO (B2Gold, ~$8) cleared walk-forward from the $1-$10 small-cap sleeve; low-priced,
    # so commission drag is material at small position sizes.
    "BTO.TO": _wf("BTO.TO", "rsi_meanrevert", {"window": 7, "oversold": 25}, 8.78, 2.69, 1.2100, -0.1580, 14, "robust"),
    "EQB.TO": _wf("EQB.TO", "ts_momentum", {"lookback": 126, "threshold": 0.0}, 8.17, 1.36, 0.4588, -0.1605, 11, "robust"),
    "QQQ": _wf("QQQ", "ts_momentum", {"lookback": 189, "threshold": 0.02}, 7.70, 2.72, 0.5030, -0.1420, 16, "robust"),
    "WCP.TO": _wf("WCP.TO", "confirm_bollinger", {"window": 30, "n_std": 3.0}, 6.62, 0.17, 0.3289, -0.0882, 5, "watch"),
    "DFY.TO": _wf("DFY.TO", "rsi_meanrevert", {"window": 14, "oversold": 35}, 6.62, 0.65, 0.2544, -0.0974, 14, "robust"),
    "KEY.TO": _wf("KEY.TO", "rsi_meanrevert", {"window": 14, "oversold": 35}, 6.11, 0.63, 0.1700, -0.0710, 5, "watch"),
    # CGL.TO (iShares gold bullion) cleared walk-forward from the $25-35 sweep — the first
    # commodity/bullion name; ATR-channel breakout, WFE > 1, the third atr_channel name after ZEB/ZWB.
    "CGL.TO": _wf("CGL.TO", "atr_channel", {"ema_window": 30, "k": 1.5}, 5.830, 1.230, 0.6550, -0.1350, 23, "robust"),
    "IWM": _wf("IWM", "bollinger", {"window": 30, "n_std": 2.0}, 5.82, 0.93, 0.1600, -0.0810, 14, "robust"),
    "RS": _wf("RS", "rsi_meanrevert", {"window": 14, "oversold": 35}, 5.75, 0.88, 0.0780, -0.0610, 12, "robust"),
    # GEI.TO (Gibson Energy) cleared walk-forward from the $25-40 sweep — OOS beat in-sample
    # (WFE 0.65), +28.6% at -7.4% DD; robust but on exactly 10 OOS trades (a thinner sample).
    "GEI.TO": _wf("GEI.TO", "rsi_meanrevert", {"window": 14, "oversold": 35}, 5.490, 0.650, 0.2860, -0.0740, 10, "robust"),
    # ZUT.TO (BMO utilities) has a huge WFE (4.35) but only 8 out-of-sample trades — too thin a
    # sample to call robust, so it is carried at watch (same reason as VFV.TO).
    "ZUT.TO": _wf("ZUT.TO", "ts_momentum", {"lookback": 189, "threshold": 0.0}, 5.190, 4.350, 0.2040, -0.1050, 8, "watch"),
    # XEI.TO (iShares Cdn equal-weight income ETF) topped the $25-40 in-sample sweep (8.4) but the
    # walk-forward re-opt picked atr_channel and the score decayed hard (IS 12.8 -> OOS 3.8) with a
    # WFE right on the 0.5 gate — clears robust on paper but carried at watch on that thin margin.
    "XEI.TO": _wf("XEI.TO", "atr_channel", {"ema_window": 20, "k": 1.5}, 3.830, 0.530, 0.0570, -0.0680, 20, "watch"),
    # EFN.TO (Element Fleet) cleared the robust gate (WFE 0.92, 24 trades) but is carried at watch
    # on a drawdown call: its -19.3% OOS drawdown echoes TA.TO. Flip to robust if that's acceptable.
    "EFN.TO": _wf("EFN.TO", "ts_momentum", {"lookback": 126, "threshold": 0.0}, 3.210, 0.920, 0.3560, -0.1930, 24, "watch"),
    # DBC (broad commodity index fund) cleared walk-forward in the $1-45 resweep under the
    # commodity trade-count bar of 4 (see min_oos_trades): OOS 5.57 on WFE 0.62 across 7 trades.
    # It would NOT have cleared the flat 10-trade bar, which is precisely the class-wide exclusion
    # the lower commodity bar was introduced to fix. Thinner evidence than a 10-trade equity name.
    "DBC": _wf("DBC", "bollinger", {"window": 20, "n_std": 2.0}, 5.565, 0.622, 0.0020, -0.1320, 7, "robust",
               asset_class="commodity"),
    "DBA": _wf("DBA", "rsi_meanrevert", {"window": 14, "oversold": 35}, 3.17, 0.57, 0.0530, -0.0500, 11, "robust"),
    # TA.TO (TransAlta) technically cleared the robust gate (WFE 0.79, 32 trades) but is carried
    # at "watch" on a deliberate risk call: its OOS MACD run posts a big return through a -26.5%
    # drawdown, deeper than anything else in the pool. Watch until the drawdown profile improves.
    "TA.TO": _wf("TA.TO", "macd", {"fast": 16, "slow": 34}, 2.68, 0.79, 0.7610, -0.2650, 32, "watch"),
    # CRT.UN.TO (CT REIT) cleared walk-forward in the $1-45 resweep - the second REIT in the pool
    # after SRU.UN.TO, on the same rsi_meanrevert family but a tighter oversold band. WFE 0.59 is
    # modest and it clears the trade-count gate exactly (10), so it is robust on a thin margin.
    "CRT.UN.TO": _wf("CRT.UN.TO", "rsi_meanrevert", {"window": 7, "oversold": 20}, 9.180, 0.590, 0.0301, -0.0519, 10, "robust"),
    # VALE cleared walk-forward in the $1-45 resweep - the first non-North-American-listed name
    # (Brazilian iron ore) and the first materials producer in the pool, diversifying away from
    # the Canadian financials/energy concentration. OOS beat in-sample (WFE 1.52) on 16 trades.
    # Numbers are the RE-RUN after fixing a sweep bug that priced VALE at ETF spreads (a first-letter
    # heuristic caught the leading V); at correct equity spreads it still clears robust.
    "VALE": _wf("VALE", "bollinger", {"window": 15, "n_std": 2.5}, 6.339, 1.520, 0.0312, -0.0945, 16, "robust"),
}


def validated_symbols(tier: str | None = None) -> tuple[str, ...]:
    """Walk-forward-validated tickers, optionally filtered to one ``tier``."""
    return tuple(
        v.symbol for v in WALK_FORWARD_VALIDATED.values() if tier is None or v.tier == tier
    )


def validated_for(symbol: str) -> WFValidated | None:
    """The validated recommendation for ``symbol``, or ``None`` if not validated."""
    return WALK_FORWARD_VALIDATED.get(symbol)


@dataclass(frozen=True)
class CryptoSleeveEntry:
    """One Kraken currency in the second (crypto) sleeve.

    ``symbol`` is the routed/live form (e.g. ``BTC/USD``); ``pair`` is the Kraken REST OHLC code
    (e.g. ``XBTUSD``). ``screen_score`` is the in-sample panel score from the currency sweep.
    """

    symbol: str
    pair: str
    strategy: str
    screen_score: float
    params: Mapping[str, float] = field(default_factory=dict)
    asset_class: str = "crypto"
    tier: str = "screened"


# Second sleeve — Kraken currencies. IMPORTANT: unlike WALK_FORWARD_VALIDATED, these are only
# *screened* (best in-sample strategy on ~2 years of Kraken daily candles — the endpoint caps at
# ~720 bars, too short to walk-forward). Scores are weak vs the equity pool (BTC macd 2.59 leads;
# the rest are under 1.7), so this sleeve is PROVISIONAL and PAPER-ONLY: it routes through the
# Router's risk gates and, via AssetRouter, to Kraken as the crypto venue, but nothing here is
# validated or cleared for live capital until deeper history lets it clear the walk-forward gate.
CRYPTO_SLEEVE: dict[str, CryptoSleeveEntry] = {
    "BTC/USD": CryptoSleeveEntry("BTC/USD", "XBTUSD", "macd", 2.59, {"fast": 16, "slow": 34}),
    # PAXG (Pax Gold) is a tokenized-gold token — it tracks physical gold, so it has the lowest
    # volatility (0.25) in the crypto universe and diversifies the sleeve away from crypto beta
    # (the on-Kraken analog of the equity pool's CGL.TO). Surfaced by the widened 638-pair search.
    "PAXG/USD": CryptoSleeveEntry("PAXG/USD", "PAXGUSD", "ts_momentum", 2.55, {"lookback": 90, "threshold": 0.0}),
    "XMR/USD": CryptoSleeveEntry("XMR/USD", "XMRUSD", "macd", 1.66, {"fast": 16, "slow": 34}),
    "XRP/USD": CryptoSleeveEntry("XRP/USD", "XRPUSD", "bollinger", 1.43, {}),
    "XLM/USD": CryptoSleeveEntry("XLM/USD", "XLMUSD", "bollinger", 1.01, {}),
    "LINK/USD": CryptoSleeveEntry("LINK/USD", "LINKUSD", "atr_channel", 0.93, {"ema_window": 30, "k": 1.5}),
    "ETH/USD": CryptoSleeveEntry("ETH/USD", "ETHUSD", "macd", 0.62, {"fast": 16, "slow": 34}),
}


def crypto_sleeve_symbols() -> tuple[str, ...]:
    """Routed symbols in the crypto sleeve (e.g. ``BTC/USD``)."""
    return tuple(CRYPTO_SLEEVE)


def crypto_sleeve_for(symbol: str) -> CryptoSleeveEntry | None:
    """The crypto-sleeve entry for ``symbol``, or ``None`` if it is not in the sleeve."""
    return CRYPTO_SLEEVE.get(symbol)
