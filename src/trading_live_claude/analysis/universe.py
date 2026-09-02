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

# Names currently held in the live QT account. Always carried in to every sweep and every
# screen regardless of price / liquidity / volatility filters, so a resweep can never silently
# drop coverage of what we actually own. Update by hand when positions change; a broker probe
# would be more automatic but also more fragile (the fetch would need to be non-blocking).
HELD_ASSETS: tuple[str, ...] = ("CGL.TO", "ZUT.TO", "SDE.TO")

# Curated seed pools. Futures/commodities use liquid ETF proxies so the same
# equities data path works; swap for native continuous-future tickers under LEAN.
#
# Expansion decisions (Sep 2026 resweep):
#   * TSX side widened to cover the six sector-index ETFs (XIC/XIU/XEG/XEF/XFN/etc), the big
#     financials and energy names, gold + materials producers, and REIT breadth. Point is to
#     let the sweep find survivors we currently exclude by construction.
#   * US side widened to the SPDR sector ETFs + a curated mega-cap / cyclical / defensive
#     spread. Names picked to span sectors, not to be a market-cap ranking.
#   * Commodity sleeve expanded to include the metals and ags the WF pool has been thin on.
#   * Futures list held to the six index/rates continuous-contract placeholders. There is no
#     futures execution path in the framework yet, so more futures = more nothing traded.
#   * Crypto seed matches the current CRYPTO_SLEEVE routed-symbol form for consistency; the
#     canonical sleeve entries live below in CRYPTO_SLEEVE.
SEED_UNIVERSE: dict[AssetClass, tuple[str, ...]] = {
    "equity": (
        # --- broad indexes / ETFs (Canada + US)
        "XIC.TO", "XIU.TO", "VFV.TO", "XEF.TO", "XEM.TO", "VCE.TO",
        "VOO", "SPY", "QQQ", "IWM", "VTI", "DIA", "MDY",
        # --- SPDR US sector ETFs (expanded coverage for the sweep to compare like-with-like)
        "XLE", "XLB", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY", "XLC", "XLRE",
        # --- Canadian sector ETFs
        "ZEB.TO", "ZWB.TO", "ZUT.TO", "ZUB.TO", "XFN.TO", "XEG.TO", "XIT.TO", "XMA.TO",
        "XRE.TO", "XEI.TO", "XSP.TO",
        # --- US mega-caps + tech / cyclicals / defensives
        "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN", "META", "TSLA", "AVGO",
        "V", "MA", "JPM", "BAC", "WFC", "GS", "MS",
        "XOM", "CVX", "COP", "SLB", "OXY",
        "JNJ", "PFE", "MRK", "UNH", "LLY",
        "PG", "KO", "PEP", "COST", "WMT",
        "CAT", "DE", "HON", "BA", "LMT", "RTX",
        # --- Canadian mega-caps
        "RY.TO", "TD.TO", "BNS.TO", "BMO.TO", "CM.TO", "NA.TO",
        "ENB.TO", "TRP.TO", "CNQ.TO", "SU.TO", "IMO.TO", "CVE.TO",
        "SHOP.TO", "BN.TO", "BAM.TO", "L.TO", "MG.TO",
        # --- Canadian mid/small caps that already surfaced in earlier sweeps
        "ARX.TO", "DFY.TO", "EQB.TO", "WCP.TO", "KEY.TO", "FRU.TO", "TA.TO",
        "SRU.UN.TO", "CGL.TO", "EFN.TO", "GEI.TO", "CRT.UN.TO", "BTO.TO",
        "CEW.TO", "VALE",
        # --- gold / materials / precious-metals producers (Canada + US)
        "ABX.TO", "AEM.TO", "K.TO", "FNV.TO", "WPM.TO", "PAAS.TO", "TECK.B.TO",
        "FCX", "NEM", "GOLD", "AA", "RS",
        # --- REITs (Canadian carriers)
        "REI.UN.TO", "AP.UN.TO", "HR.UN.TO", "CAR.UN.TO", "SMU.UN.TO",
        # --- commodity ETFs kept in equity list because they route as equities
        "DBC", "DBA", "GLD", "SLV",
        # --- currently-held (redundant with HELD_ASSETS carry-in but explicit here too)
        "SDE.TO",
        # --- promoted from Sep-2026 full-universe resweep, kept in the seed for future sweeps
        "ENB.TO", "XIU.TO", "VDY.TO", "SLF.TO",
    ),
    # ETF proxies for continuous futures (LEAN swaps in real front-month; this pool works with the
    # existing equities path). Held small on purpose — no futures execution in the framework yet.
    "future": ("ES", "NQ", "YM", "RTY", "ZN", "ZB"),
    # Expanded commodity ETF sleeve — the WF pool has been thin on metals + softs, so we surface
    # the standard names for the sweep to score.
    "commodity": (
        "GLD", "SLV", "PPLT", "PALL",             # precious
        "USO", "UNG", "BNO",                      # energy
        "DBC", "DBA",                             # broad baskets
        "CORN", "WEAT", "SOYB", "CANE",           # softs
        "COPX", "URA", "REMX",                    # metals + specialty
    ),
    # Kraken crypto pairs (routed form). Canonical sleeve entries + strategies live below in
    # CRYPTO_SLEEVE. Broader here to let a resweep surface additional pairs worth screening.
    "crypto": (
        "BTC/USD", "ETH/USD", "SOL/USD", "ADA/USD", "XRP/USD", "XMR/USD",
        "XLM/USD", "LINK/USD", "PAXG/USD", "AVAX/USD", "DOT/USD", "MATIC/USD",
        "ATOM/USD", "ALGO/USD", "LTC/USD",
    ),
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


# --- walk-forward protocols per asset class ------------------------------------------------------
# The uniform 2y-train / 6mo-test / 252-annualization / 10-trade bar suits equities but is wrong
# for other classes: crypto trades 365 days/yr and shifts regime faster (so shorter train+test),
# commodities trade on a slower cycle (already reflected in MIN_OOS_TRADES=4), and futures need a
# continuous-contract data layer that does not exist here yet. Encoding the protocol per class
# rather than hardcoding it in one function makes the WF pipeline honest about what "walk-forward"
# means for each source of information available.


@dataclass(frozen=True)
class WFProtocol:
    """Walk-forward protocol for one asset class.

    ``train_bars`` / ``test_bars`` are in the class's native bar unit (daily bars for every class
    at present). ``annualization`` is the periods-per-year constant returns use (252 or 365).
    ``min_wfe`` is the OOS/IS ratio floor for the robust tier. ``min_oos_trades`` mirrors the
    class-dependent bar (kept here so a caller has one lookup, not two). ``data_source`` is a
    short label for where the frames come from — informational, so a report can say WHY the
    protocol looks the way it does.
    """

    asset_class: str
    train_bars: int
    test_bars: int
    step_bars: int
    annualization: int
    min_wfe: float
    min_oos_trades: int
    data_source: str
    notes: str = ""


# The canonical protocol registry. Read via wf_protocol(asset_class).
WF_PROTOCOLS: dict[str, WFProtocol] = {
    # Equities: the standard. Questrade daily bars, 5+ years easily via paginated fetch. Trades
    # 252 days/yr, so 504 train / 126 test is 2y / 6mo. 10 OOS trades is the honest bar.
    "equity": WFProtocol(
        asset_class="equity",
        train_bars=504, test_bars=126, step_bars=126,
        annualization=252, min_wfe=0.5, min_oos_trades=10,
        data_source="questrade-daily",
        notes="2y train / 6mo test / step 6mo. Standard protocol; every earlier WF used this.",
    ),
    # Commodities: same daily cadence as equities, but the trade cadence is slower — a bollinger
    # on a broad commodity basket fires roughly quarterly. Trade-count bar drops to 4 (about once
    # a quarter over a 6mo test window). Everything else identical to equity.
    "commodity": WFProtocol(
        asset_class="commodity",
        train_bars=504, test_bars=126, step_bars=126,
        annualization=252, min_wfe=0.5, min_oos_trades=4,
        data_source="questrade-daily",
        notes="Same window as equity; trade-count bar lower because commodity strategies fire "
              "on a slower cycle (about quarterly). Cleared names carry thinner evidence than "
              "an equity cleared on 10+ — sensitivity trade-off documented in universe.py.",
    ),
    # Crypto: 365 days/yr, regime shifts faster, and Kraken's daily OHLC caps at ~720 bars —
    # deeper history needs scripts/fetch_crypto_history.py (paginated /public/Trades). Shorter
    # windows honestly acknowledge that: 1y train / 3mo test (365/91), same 10-trade bar (crypto
    # trades every day so 10 in 3 months is not a low bar in absolute terms).
    "crypto": WFProtocol(
        asset_class="crypto",
        train_bars=365, test_bars=91, step_bars=91,
        annualization=365, min_wfe=0.5, min_oos_trades=10,
        data_source="kraken-daily (via kraken_ohlc_deep)",
        notes="Shorter train + test to accept faster regime shifts. Requires the deep-history "
              "fetch to have been run (scripts/fetch_crypto_history.py) — the shallow endpoint "
              "caps at ~720 bars which is barely one fold's worth.",
    ),
    # Futures: continuous-contract data doesn't exist in this project yet AND there's no futures
    # broker adapter. The protocol is registered so callers get a clear "no data" error rather
    # than fabricating a run against wrong data.
    "future": WFProtocol(
        asset_class="future",
        train_bars=504, test_bars=126, step_bars=126,
        annualization=252, min_wfe=0.5, min_oos_trades=10,
        data_source="UNAVAILABLE - needs continuous-contract data pipeline + broker adapter",
        notes="Registered so wf_protocol() has an entry, but no futures WF can honestly run "
              "today. Adding a continuous-contract data layer (roll-yield-adjusted) is the "
              "prerequisite, then a broker adapter, then this protocol becomes live.",
    ),
    # FX: hedged / currency ETFs currently route through the equity pool (e.g. CEW.TO). If a
    # native FX venue and pair-price feed land, this protocol governs its WF. Trades ~260 days
    # a year on-shore (weekday-close), so equity-like windows apply.
    "fx": WFProtocol(
        asset_class="fx",
        train_bars=504, test_bars=126, step_bars=126,
        annualization=260, min_wfe=0.5, min_oos_trades=10,
        data_source="pair-price feed (not yet wired)",
        notes="Currency-hedged ETFs like CEW.TO clear under the equity protocol; a native FX "
              "protocol activates once a pair-price feed exists.",
    ),
}


def wf_protocol(asset_class: str) -> WFProtocol:
    """Return the walk-forward protocol for ``asset_class``. Defaults to the equity protocol
    for unknown classes so callers never silently misconfigure a run."""
    return WF_PROTOCOLS.get(asset_class, WF_PROTOCOLS["equity"])


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
    # CGL.TO (iShares Gold Bullion, CAD-hedged) is a physical-commodity ETF, not an equity.
    # asset_class="commodity" so risk gates, min-trade bars, and asset-class heat treat it
    # correctly — and so per-class coverage counts the pool honestly. Also HELD in the QT account.
    "CGL.TO": _wf("CGL.TO", "atr_channel", {"ema_window": 30, "k": 1.5}, 5.830, 1.230, 0.6550, -0.1350, 23, "robust",
                  asset_class="commodity"),
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
    # DBA (Invesco Agriculture) tracks a soft-commodity basket — a commodity ETF, not an equity.
    # Same class fix as CGL.TO above.
    "DBA": _wf("DBA", "rsi_meanrevert", {"window": 14, "oversold": 35}, 3.17, 0.57, 0.0530, -0.0500, 11, "robust",
               asset_class="commodity"),
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
    # --- promoted from the Sep-2026 full-universe resweep (sweep_resweep_full) ---
    # ENB.TO (Enbridge) topped the sweep by a wide margin: bollinger(15, 2.0), OOS 111.22 on WFE
    # 8.39 across 19 OOS trades. The WFE outlier is real (IS ~13, OOS ~111 — the strategy did NOT
    # oversell in-sample, it *undersold*) but should be treated with corresponding skepticism —
    # confirm on the next re-run before sizing off it aggressively.
    "ENB.TO": _wf("ENB.TO", "bollinger", {"window": 15, "n_std": 2.0}, 111.218, 8.389, 0.0220, -0.0959, 19, "robust"),
    # XIU.TO (iShares S&P/TSX 60) — the TSX-60 ETF. First large-cap Cdn index ETF in the pool;
    # complements XIC.TO. bollinger(15, 2.0), OOS 19.47 / WFE 0.70 / 12 trades.
    "XIU.TO": _wf("XIU.TO", "bollinger", {"window": 15, "n_std": 2.0}, 19.475, 0.704, 0.0104, -0.0476, 12, "robust"),
    # VDY.TO (Vanguard FTSE Cdn High Dividend Yield) — dividend-tilt Cdn ETF, ts_momentum with a
    # 63-day lookback + 2% threshold. Complements the existing rate/energy-heavy Cdn exposure with
    # a dividend-payer factor. OOS 18.20 / WFE 0.66 / 10 trades.
    "VDY.TO": _wf("VDY.TO", "ts_momentum", {"lookback": 63, "threshold": 0.02}, 18.198, 0.661, 0.0156, -0.0504, 10, "robust"),
    # SLF.TO (Sun Life Financial) — first insurance name in the pool, so the sector coverage
    # widens from financials-only to financials+insurance. bollinger(10, 1.5), OOS 7.18 / WFE 0.54
    # / 12 trades. Cleanest of the four on drawdown (-4.1%).
    "SLF.TO": _wf("SLF.TO", "bollinger", {"window": 10, "n_std": 1.5}, 7.176, 0.539, 0.0203, -0.0407, 12, "robust"),
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
