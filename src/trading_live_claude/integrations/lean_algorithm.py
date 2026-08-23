"""A minimal, valid LEAN algorithm used as the default for ``trading qc-backtest``.

This is written in QuantConnect's LEAN API (``QCAlgorithm``), which is a different
framework than this repo's pandas ``Strategy`` classes — so it is a stand-alone
starter, not a translation of any local strategy. Override it by passing
``--algorithm <path-to-your-lean-file.py>`` to the command.

It mirrors the repo's ``ema_crossover`` idea (fast/slow EMA cross, long-only) so
the default backtest is recognizable.
"""
from __future__ import annotations


def render_lean_algorithm(
    *,
    symbol: str,
    add_method: str = "AddEquity",
    market: str = "",
    start: tuple[int, int, int] = (2021, 1, 1),
    end: tuple[int, int, int] = (2023, 1, 1),
    cash: int = 100_000,
    fast: int = 20,
    slow: int = 50,
) -> str:
    """Render a LEAN EMA-crossover algorithm parameterized by asset class.

    ``add_method`` / ``market`` come from the asset-class → brokerage router's
    ``RouteDecision`` (e.g. AddEquity, AddFuture, or AddCrypto + Market.Coinbase), so
    the same generator emits a valid subscription for equities, futures, commodities,
    or crypto. Long-only 20/50 EMA cross on the primary symbol.
    """
    add_args = f'"{symbol}", Resolution.Daily' + (f", {market}" if market else "")
    sy, sm, sd = start
    ey, em, ed = end
    return f'''\
from AlgorithmImports import *


class GeneratedEmaCross(QCAlgorithm):
    """Long-only {fast}/{slow} EMA crossover on {symbol} (asset-routed via {add_method})."""

    def Initialize(self):
        self.SetStartDate({sy}, {sm}, {sd})
        self.SetEndDate({ey}, {em}, {ed})
        self.SetCash({cash})
        self.sym = self.{add_method}({add_args}).Symbol
        self.fast = self.EMA(self.sym, {fast}, Resolution.Daily)
        self.slow = self.EMA(self.sym, {slow}, Resolution.Daily)

    def OnData(self, data):
        if not (self.fast.IsReady and self.slow.IsReady):
            return
        if self.fast.Current.Value > self.slow.Current.Value and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif self.fast.Current.Value < self.slow.Current.Value and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''


# Back-compat default used by ``qc-backtest`` when no algorithm file is supplied.
DEFAULT_LEAN_ALGORITHM = render_lean_algorithm(symbol="SPY", add_method="AddEquity")


# Our candlestick pattern name -> QuantConnect CandlestickPatterns method (snake_case
# LEAN Python API). Only patterns LEAN ships natively are mappable; this is what makes
# a candlestick strategy "workable for QC" as well as QT.
LEAN_CANDLESTICK_MAP: dict[str, str] = {
    "hammer": "Hammer",
    "inverted_hammer": "InvertedHammer",
    "bullish_engulfing": "Engulfing",
    "bullish_harami": "Harami",
    "piercing_line": "Piercing",
    "morning_star": "MorningStar",
    "morning_doji_star": "MorningDojiStar",
    "three_white_soldiers": "ThreeWhiteSoldiers",
    "three_inside_up": "ThreeInside",
    "three_outside_up": "ThreeOutside",
    "abandoned_baby_bull": "AbandonedBaby",
    "dragonfly_doji": "DragonflyDoji",
    "belt_hold_bull": "BeltHold",
    "matching_low": "MatchingLow",
    "rising_three_methods": "RiseFallThreeMethods",
}


def render_candlestick_lean_algorithm(
    *,
    pattern: str,
    symbol: str,
    add_method: str = "AddEquity",
    market: str = "",
    start: tuple[int, int, int] = (2021, 1, 1),
    end: tuple[int, int, int] = (2023, 1, 1),
    cash: int = 100_000,
    exit_ma: int = 10,
) -> str:
    """Render a LEAN algorithm that trades a candlestick pattern via QC's built-ins.

    Uses ``self.candlestick_patterns.<pattern>`` (LEAN's native CandlestickPatterns),
    entering long when the indicator turns bullish (value > 0) and exiting on a
    short-SMA momentum fade — the same entry/exit logic as the native QT strategy.
    """
    if pattern not in LEAN_CANDLESTICK_MAP:
        raise KeyError(
            f"No LEAN CandlestickPatterns equivalent for {pattern!r}. "
            f"Deployable to QC: {sorted(LEAN_CANDLESTICK_MAP)}"
        )
    lean_method = LEAN_CANDLESTICK_MAP[pattern]
    add_args = f'"{symbol}", Resolution.Daily' + (f", {market}" if market else "")
    sy, sm, sd = start
    ey, em, ed = end
    return f'''\
from AlgorithmImports import *


class Generated{pattern.title().replace("_", "")}(QCAlgorithm):
    """Long on the {pattern} candlestick (QC CandlestickPatterns.{lean_method}); SMA-fade exit."""

    def Initialize(self):
        self.SetStartDate({sy}, {sm}, {sd})
        self.SetEndDate({ey}, {em}, {ed})
        self.SetCash({cash})
        self.sym = self.{add_method}({add_args}).Symbol
        self.pattern = self.CandlestickPatterns.{lean_method}(self.sym)
        self.ma = self.SMA(self.sym, {exit_ma}, Resolution.Daily)

    def OnData(self, data):
        if not (self.pattern.IsReady and self.ma.IsReady):
            return
        price = self.Securities[self.sym].Price
        if self.pattern.Current.Value > 0 and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif price < self.ma.Current.Value and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''
