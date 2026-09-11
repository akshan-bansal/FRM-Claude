"""CompositeStrategy — the recall lever of the pipeline.

Unions the entry candidates of several member detectors (logical OR), so the
composite catches every move any member catches: its recall is >= the recall of
its best member, by construction. The trade-off is lower precision — which the
scoring stage is responsible for recovering.

The fraction of members that agree on a bar is emitted as ``signal_strength`` in
[0, 1]. This graded agreement is exactly the kind of feature the scoring/precision
stage consumes: a bar where 4/4 detectors fire is a stronger candidate than one
where 1/4 does.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, StrategyContext
from .examples.bollinger import BollingerMeanRevert
from .examples.ema_crossover import EmaCrossover
from .examples.momentum_breakout import DonchianBreakout
from .examples.rsi_meanrevert import RsiMeanRevert


class CompositeStrategy(Strategy):
    """Union an ensemble of member strategies to maximize recall.

    ``entry`` / ``exit`` are the logical OR of the members'. ``signal_strength`` is
    the fraction of members that fired an entry on that bar (agreement in [0, 1]),
    and ``n_agree`` is the raw count. ``atr`` is carried through from the first
    member that provides it, so the position sizer keeps working unchanged.
    """

    name = "composite"
    description = "OR-ensemble of detectors (recall stage)"

    def __init__(self, members: list[Strategy] | None = None) -> None:
        super().__init__()
        if members is not None and not members:
            raise ValueError("CompositeStrategy needs at least one member")
        self.members: list[Strategy] = members if members is not None else _default_members()
        # Composite declares level-trigger support iff any member does (2026-09-10).
        # Set on the instance so LiveMonitor's getattr check picks it up regardless of
        # the class-level default. Composites of pure event-triggered members stay
        # event-only, keeping backward compatibility.
        self.supports_level_trigger = any(
            getattr(m, "supports_level_trigger", False) for m in self.members
        )

    def required_history_bars(self) -> int:
        return max(m.required_history_bars() for m in self.members)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        entries: list[pd.Series] = []
        exits: list[pd.Series] = []
        levels: list[pd.Series] = []
        atr_col: pd.Series | None = None
        any_level_support = False

        for member in self.members:
            sig = member.generate_signals(df, ctx)
            entries.append(sig["entry"].fillna(0).astype(int))
            exits.append(sig["exit"].fillna(0).astype(int))
            # Level channel (2026-09-10) — OR across members that emit it so composite
            # preserves any single member's level-eligibility. Members without
            # entry_level contribute a zero column and don't affect the OR.
            if "entry_level" in sig.columns and getattr(member, "supports_level_trigger", False):
                levels.append(sig["entry_level"].fillna(0).astype(int))
                any_level_support = True
            else:
                levels.append(pd.Series(0, index=sig.index))
            if atr_col is None and "atr" in sig.columns:
                atr_col = sig["atr"]

        entry_mat = pd.concat(entries, axis=1)
        exit_mat = pd.concat(exits, axis=1)
        level_mat = pd.concat(levels, axis=1)

        out["entry"] = entry_mat.max(axis=1).astype(int)   # OR: any member enters
        out["exit"] = exit_mat.max(axis=1).astype(int)      # OR: any member exits
        # Only emit entry_level when at least one member declared level support —
        # composite of pure event-triggered strategies shouldn't fabricate a level
        # column of all zeros (would trip the LiveMonitor's has-column check).
        if any_level_support:
            out["entry_level"] = level_mat.max(axis=1).astype(int)
        out["n_agree"] = entry_mat.sum(axis=1).astype(int)
        out["signal_strength"] = entry_mat.mean(axis=1).astype(float)  # agreement [0,1]
        if atr_col is not None:
            out["atr"] = atr_col
        return out


def _default_members() -> list[Strategy]:
    """Long-biased detector set for the default composite.

    Pairs is excluded (it needs two cointegrated symbols, not a single OHLCV frame).
    """
    return [
        BollingerMeanRevert(),
        RsiMeanRevert(),
        EmaCrossover(),
        DonchianBreakout(),
    ]


class DefaultComposite(CompositeStrategy):
    """Zero-arg composite so it can be registered and tuned like any strategy."""

    name = "composite"

    def __init__(self) -> None:
        super().__init__(members=_default_members())
