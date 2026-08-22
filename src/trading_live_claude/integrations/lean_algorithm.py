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
