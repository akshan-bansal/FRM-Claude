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
        # Tunable knobs (QC parameter-optimizable), defaulting to {fast}/{slow}.
        fast = int(self.GetParameter("fast", {fast}))
        slow = int(self.GetParameter("slow", {slow}))
        self.sym = self.{add_method}({add_args}).Symbol
        self.fast = self.EMA(self.sym, fast, Resolution.Daily)
        self.slow = self.EMA(self.sym, slow, Resolution.Daily)

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


_MEAN_REVERSION_LEAN = '''\
from AlgorithmImports import *


class GapMeanReversionRsi(QCAlgorithm):
    """Mean reversion: buy RSI(2) oversold, exit on RSI recovery."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        self.entry_th = float(self.GetParameter("entry_rsi", 10))
        self.exit_th = float(self.GetParameter("exit_rsi", 60))
        period = int(self.GetParameter("rsi_period", 2))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.rsi = self.RSI(self.sym, period, MovingAverageType.Wilders, Resolution.Daily)

    def OnData(self, data):
        if not self.rsi.IsReady:
            return
        if self.rsi.Current.Value < self.entry_th and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif self.rsi.Current.Value > self.exit_th and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''

_VOLATILITY_LEAN = '''\
from AlgorithmImports import *


class GapVolatilityRegime(QCAlgorithm):
    """Volatility regime: risk-on when realized vol (ATR/STD) is calm."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        self.low_vol = float(self.GetParameter("low_vol", 0.15))
        self.high_vol = float(self.GetParameter("high_vol", 0.25))
        atr_period = int(self.GetParameter("atr_period", 14))
        std_period = int(self.GetParameter("std_period", 20))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.atr = self.ATR(self.sym, atr_period, MovingAverageType.Wilders, Resolution.Daily)
        self.std = self.STD(self.sym, std_period, Resolution.Daily)

    def OnData(self, data):
        if not (self.atr.IsReady and self.std.IsReady):
            return
        price = self.Securities[self.sym].Price
        if price <= 0:
            return
        annualized_vol = (self.std.Current.Value / price) * (252 ** 0.5)
        if annualized_vol < self.low_vol and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif annualized_vol > self.high_vol and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''

_SEASONALITY_LEAN = '''\
from AlgorithmImports import *


class GapSeasonalityTurnOfMonth(QCAlgorithm):
    """Seasonality: hold across the turn-of-month calendar window."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        self.enter_day = int(self.GetParameter("enter_day", 27))
        self.exit_day = int(self.GetParameter("exit_day", 5))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol

    def OnData(self, data):
        day = self.Time.day
        if day >= self.enter_day and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif self.exit_day <= day < self.enter_day and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''


_MOMENTUM_MACD_LEAN = '''\
from AlgorithmImports import *


class SeedMomentumMacd(QCAlgorithm):
    """Momentum: MACD signal-line crossover."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        fast = int(self.GetParameter("macd_fast", 12))
        slow = int(self.GetParameter("macd_slow", 26))
        signal = int(self.GetParameter("macd_signal", 9))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.macd = self.MACD(self.sym, fast, slow, signal, MovingAverageType.Exponential, Resolution.Daily)

    def OnData(self, data):
        if not self.macd.IsReady:
            return
        if self.macd.Current.Value > self.macd.Signal.Current.Value and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif self.macd.Current.Value < self.macd.Signal.Current.Value and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''

_MEAN_REVERSION_BB_LEAN = '''\
from AlgorithmImports import *


class SeedMeanReversionBollinger(QCAlgorithm):
    """Mean reversion: buy the lower Bollinger band, exit at the middle band."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        period = int(self.GetParameter("bb_period", 20))
        n_std = float(self.GetParameter("bb_std", 2))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol
        self.bb = self.BB(self.sym, period, n_std, MovingAverageType.Simple, Resolution.Daily)

    def OnData(self, data):
        if not self.bb.IsReady:
            return
        price = self.Securities[self.sym].Price
        if price < self.bb.LowerBand.Current.Value and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif price > self.bb.MiddleBand.Current.Value and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''

_SEASONALITY_DOW_LEAN = '''\
from AlgorithmImports import *


class SeedSeasonalityDayOfWeek(QCAlgorithm):
    """Seasonality: hold from Monday, flat by Friday (day-of-week effect)."""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        self.entry_dow = int(self.GetParameter("entry_dow", 0))
        self.exit_dow = int(self.GetParameter("exit_dow", 4))
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol

    def OnData(self, data):
        weekday = self.Time.weekday()
        if weekday == self.entry_dow and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif weekday == self.exit_dow and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''


def _lean_strategy(
    class_name: str,
    doc: str,
    params: dict[str, int | float],
    indicators: list[tuple[str, str]],
    entry: str,
    exit_: str,
) -> str:
    """Render a standard long-only LEAN algorithm from a compact spec.

    ``params`` become tunable ``self.<name> = int/float(self.GetParameter(...))``;
    ``indicators`` are ``(attr, constructor)`` pairs; ``entry``/``exit_`` are LEAN
    boolean expressions (may use ``price`` and ``self.<attr>...``).
    """
    plines = "\n".join(
        f'        self.{p} = {"int" if isinstance(d, int) else "float"}(self.GetParameter("{p}", {d}))'
        for p, d in params.items()
    )
    ind_lines = "\n".join(f"        self.{a} = {c}" for a, c in indicators)
    ready = " and ".join(f"self.{a}.IsReady" for a, _ in indicators) or "True"
    return f'''\
from AlgorithmImports import *


class {class_name}(QCAlgorithm):
    """{doc}"""

    def Initialize(self):
        self.SetStartDate(2021, 1, 1)
        self.SetEndDate(2023, 1, 1)
        self.SetCash(100000)
        self.sym = self.AddEquity("SPY", Resolution.Daily).Symbol
{plines}
{ind_lines}

    def OnData(self, data):
        if not ({ready}):
            return
        price = self.Securities[self.sym].Price
        if ({entry}) and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif ({exit_}) and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''


# Additional detectable + tunable strategies across momentum / mean-reversion /
# volatility, inspired by QuantConnect's tutorial catalog. (name, family, source).
def _extra_domain_strategies() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []

    def add(
        name: str,
        family: str,
        cls: str,
        doc: str,
        params: dict[str, int | float],
        inds: list[tuple[str, str]],
        entry: str,
        exit_: str,
    ) -> None:
        out.append((name, family, _lean_strategy(cls, doc, params, inds, entry, exit_)))

    # momentum (detected via SMA/MOM, or by name for ADX/Aroon/ROC)
    add("Seed Momentum DualSMA", "momentum", "SeedMomentumDualSma", "Momentum: dual SMA crossover",
        {"fast": 20, "slow": 100},
        [("fast_ma", "self.SMA(self.sym, self.fast, Resolution.Daily)"),
         ("slow_ma", "self.SMA(self.sym, self.slow, Resolution.Daily)")],
        "self.fast_ma.Current.Value > self.slow_ma.Current.Value",
        "self.fast_ma.Current.Value < self.slow_ma.Current.Value")
    add("Seed Momentum MOM", "momentum", "SeedMomentumMom", "Momentum: rate-of-change sign",
        {"period": 90},
        [("mom", "self.MOM(self.sym, self.period, Resolution.Daily)")],
        "self.mom.Current.Value > 0", "self.mom.Current.Value < 0")
    add("Seed Momentum ROC", "momentum", "SeedMomentumRoc", "Momentum: ROC percent",
        {"period": 60},
        [("roc", "self.ROCP(self.sym, self.period, Resolution.Daily)")],
        "self.roc.Current.Value > 0", "self.roc.Current.Value < 0")
    add("Seed Momentum ADX Trend", "momentum", "SeedMomentumAdx", "Momentum: ADX trend-strength gate",
        {"threshold": 25},
        [("adx", "self.ADX(self.sym, 14, Resolution.Daily)")],
        "self.adx.Current.Value > self.threshold", "self.adx.Current.Value < self.threshold")
    add("Seed Momentum Aroon", "momentum", "SeedMomentumAroon", "Momentum: Aroon up/down crossover",
        {"period": 25},
        [("aroon", "self.AROON(self.sym, self.period, Resolution.Daily)")],
        "self.aroon.AroonUp.Current.Value > self.aroon.AroonDown.Current.Value",
        "self.aroon.AroonUp.Current.Value < self.aroon.AroonDown.Current.Value")

    # mean-reversion (detected via RSI/STO/BB, or by name for Williams/CCI)
    add("Seed MeanReversion RSI14", "mean_reversion", "SeedMeanReversionRsi14", "Mean reversion: RSI(14) bands",
        {"period": 14, "entry_rsi": 30, "exit_rsi": 55},
        [("rsi", "self.RSI(self.sym, self.period, MovingAverageType.Wilders, Resolution.Daily)")],
        "self.rsi.Current.Value < self.entry_rsi", "self.rsi.Current.Value > self.exit_rsi")
    add("Seed MeanReversion Stochastic", "mean_reversion", "SeedMeanReversionStoch", "Mean reversion: stochastic %K",
        {"period": 14, "entry_k": 20, "exit_k": 80},
        [("sto", "self.STO(self.sym, self.period, self.period, 3)")],
        "self.sto.StochK.Current.Value < self.entry_k", "self.sto.StochK.Current.Value > self.exit_k")
    add("Seed MeanReversion Williams", "mean_reversion", "SeedMeanReversionWilliams", "Mean reversion: Williams %R",
        {"period": 14, "entry_r": -80, "exit_r": -40},
        [("wilr", "self.WILR(self.sym, self.period, Resolution.Daily)")],
        "self.wilr.Current.Value < self.entry_r", "self.wilr.Current.Value > self.exit_r")
    add("Seed MeanReversion CCI", "mean_reversion", "SeedMeanReversionCci", "Mean reversion: CCI extremes",
        {"period": 20, "entry_cci": -100, "exit_cci": 0},
        [("cci", "self.CCI(self.sym, self.period, MovingAverageType.Simple, Resolution.Daily)")],
        "self.cci.Current.Value < self.entry_cci", "self.cci.Current.Value > self.exit_cci")
    add("Seed MeanReversion PercentB", "mean_reversion", "SeedMeanReversionPercentB", "Mean reversion: Bollinger %B",
        {"period": 20, "n_std": 2},
        [("bb", "self.BB(self.sym, self.period, self.n_std, MovingAverageType.Simple, Resolution.Daily)")],
        "price < self.bb.LowerBand.Current.Value", "price > self.bb.MiddleBand.Current.Value")

    # volatility (detected via ATR/STD)
    add("Seed Volatility StdSqueeze", "volatility", "SeedVolatilityStdSqueeze", "Volatility: enter on low STD",
        {"period": 20, "low": 0.010, "high": 0.020},
        [("std", "self.STD(self.sym, self.period, Resolution.Daily)")],
        "self.std.Current.Value / price < self.low", "self.std.Current.Value / price > self.high")
    add("Seed Volatility ATR Expansion", "volatility", "SeedVolatilityAtrExpansion", "Volatility: ATR expansion breakout",
        {"period": 14, "high": 0.020, "low": 0.010},
        [("atr", "self.ATR(self.sym, self.period, MovingAverageType.Wilders, Resolution.Daily)")],
        "self.atr.Current.Value / price > self.high", "self.atr.Current.Value / price < self.low")
    add("Seed Volatility ATR Regime Fast", "volatility", "SeedVolatilityAtrRegimeFast", "Volatility: fast ATR regime",
        {"period": 7, "low": 0.012, "high": 0.022},
        [("atr", "self.ATR(self.sym, self.period, MovingAverageType.Wilders, Resolution.Daily)")],
        "self.atr.Current.Value / price < self.low", "self.atr.Current.Value / price > self.high")
    add("Seed Volatility VolTarget Slow", "volatility", "SeedVolatilityVolTargetSlow", "Volatility: slow STD target",
        {"period": 60, "low": 0.011, "high": 0.018},
        [("std", "self.STD(self.sym, self.period, Resolution.Daily)")],
        "self.std.Current.Value / price < self.low", "self.std.Current.Value / price > self.high")
    return out


def comprehensive_lean_algorithms() -> dict[str, tuple[str, str]]:
    """A broad, multi-domain set of detectable LEAN strategies for seeding QC.

    Spans every family with valid, categorizable LEAN algorithms (momentum,
    mean-reversion, volatility, seasonality, and one project per QC-mappable
    candlestick pattern). Parameters are sensible defaults — the intent is breadth so
    universe + parameter selection can be run over the pool later.

    Returns ``{project_name: (family, lean_source)}``.
    """
    algos: dict[str, tuple[str, str]] = {
        "Seed Momentum EmaCross": ("momentum", render_lean_algorithm(symbol="SPY")),
        "Seed Momentum MACD": ("momentum", _MOMENTUM_MACD_LEAN),
        "Seed MeanReversion RSI": ("mean_reversion", _MEAN_REVERSION_LEAN),
        "Seed MeanReversion Bollinger": ("mean_reversion", _MEAN_REVERSION_BB_LEAN),
        "Seed Volatility Regime": ("volatility", _VOLATILITY_LEAN),
        "Seed Seasonality TurnOfMonth": ("seasonality", _SEASONALITY_LEAN),
        "Seed Seasonality DayOfWeek": ("seasonality", _SEASONALITY_DOW_LEAN),
    }
    for name, family, source in _extra_domain_strategies():
        algos[name] = (family, source)
    for our_pattern in LEAN_CANDLESTICK_MAP:
        title = "".join(w.capitalize() for w in our_pattern.split("_"))
        algos[f"Seed Candlestick {title}"] = (
            "candlestick",
            render_candlestick_lean_algorithm(pattern=our_pattern, symbol="SPY"),
        )
    return algos


def gap_family_algorithms() -> dict[str, tuple[str, str]]:
    """Map each strategy family to a (project_name, LEAN source) for filling QC gaps.

    Each template is written so the ``qc_library`` categorizer detects the right family
    from the code (RSI→mean_reversion, ATR/STD→volatility, CandlestickPatterns→
    candlestick) or the name (Seasonality). Used to populate a thin QC account before
    live, since QC's base API has no clone-from-library call.
    """
    return {
        "mean_reversion": ("Gap MeanReversion RSI", _MEAN_REVERSION_LEAN),
        "volatility": ("Gap Volatility Regime", _VOLATILITY_LEAN),
        "seasonality": ("Gap Seasonality TurnOfMonth", _SEASONALITY_LEAN),
        "candlestick": ("Gap Candlestick MorningStar", render_candlestick_lean_algorithm(
            pattern="morning_star", symbol="SPY")),
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
        exit_ma = int(self.GetParameter("exit_ma", {exit_ma}))  # tunable
        self.sym = self.{add_method}({add_args}).Symbol
        self.pattern = self.CandlestickPatterns.{lean_method}(self.sym)
        self.ma = self.SMA(self.sym, exit_ma, Resolution.Daily)

    def OnData(self, data):
        if not (self.pattern.IsReady and self.ma.IsReady):
            return
        price = self.Securities[self.sym].Price
        if self.pattern.Current.Value > 0 and not self.Portfolio.Invested:
            self.SetHoldings(self.sym, 1.0)
        elif price < self.ma.Current.Value and self.Portfolio.Invested:
            self.Liquidate(self.sym)
'''
