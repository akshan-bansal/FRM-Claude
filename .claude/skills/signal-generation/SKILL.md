---
name: signal-generation
description: Use when translating a plain-English strategy spec, Pine Script, or research note into a vectorized Python Strategy subclass with entry/exit signals. Enforces no-lookahead bias and the framework's Strategy contract. Outputs a DataFrame the backtester, paper broker, and live monitor can all consume identically.
---

# signal-generation

You translate strategy rules into a runnable `Strategy` subclass.

## Recipe

1. **Parse the strategy rules** into explicit conditions: entry condition, exit condition, optional filters (time-of-day, regime).
2. **Map each condition to pandas/numpy operations.** Use the prebuilt indicators in `signals.indicators`: `sma`, `ema`, `rsi`, `macd`, `atr`, `bollinger`, `donchian`, `zscore`. Never use a row loop.
3. **Compute indicators vectorized.** No `for i in range(len(df))` patterns. Anything you can't vectorize is suspect.
4. **Build entry and exit Series separately** as dtype int (0/1). They are independent signals. The framework's `SignalSet.to_positions()` materializes the position track.
5. **Apply session filters if requested** (e.g., "only between 10:00 and 15:30 ET"). Use the timezone-aware `time` column.
6. **Output DataFrame** must contain at minimum `entry`, `exit`, and an `atr` column for the position sizer. `size_hint` in [0, 1] is optional.
7. **Verify no lookahead bias** — every indicator comparison must reference `.shift(1)` of the prior bar, not the current/future bar. Add a unit test using `signals.generator.no_lookahead_check(...)`.

## Scaffold for a new strategy

```python
# src/trading_live_claude/strategies/examples/my_strategy.py
from __future__ import annotations
import pandas as pd
from ...signals.indicators import atr, ema
from ..base import Strategy, StrategyContext

class MyStrategy(Strategy):
    name = "my_strategy"
    description = "<one-line description>"

    def __init__(self, fast: int = 10, slow: int = 30) -> None:
        super().__init__(fast=fast, slow=slow)
        self.fast, self.slow = fast, slow

    def required_history_bars(self) -> int:
        return max(self.slow * 4, 120)

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = ema(out["close"], self.fast)
        out["ema_slow"] = ema(out["close"], self.slow)
        out["atr"] = atr(out, 14)
        out["entry"] = ((out["ema_fast"] > out["ema_slow"]) &
                        (out["ema_fast"].shift(1) <= out["ema_slow"].shift(1))).astype(int)
        out["exit"] = ((out["ema_fast"] < out["ema_slow"]) &
                       (out["ema_fast"].shift(1) >= out["ema_slow"].shift(1))).astype(int)
        out["size_hint"] = 1.0
        return out
```

Then register it in `src/trading_live_claude/strategies/__init__.py:STRATEGIES`.

## Always

- Write the lookahead-bias unit test alongside the strategy.
- Use `prev = series.shift(1)` to compare against the previous bar.
- Make hyperparameters constructor arguments so they're easy to grid-search.
- Hand the resulting strategy to the `strategy-reviewer` agent before merging.

## Never

- Use `.shift(-N)`. Anywhere. Ever.
- Use `.rolling(..., center=True)`.
- Use future close in the entry condition (e.g., `df["close"] > df["close"].rolling(20).max()` is fine, but `df["close"].rolling(20, center=True).max()` would peek).
- Call any broker method from inside `generate_signals`.

## Trigger phrases

- "Code this Pine Script in our framework"
- "Convert this paper's strategy rule into Python"
- "I want to add a strategy that goes long when ..."

## Libraries

`pandas`, `numpy`. The framework's `signals.indicators` and `strategies.base.Strategy`.

## Article alignment

Skill #3 in [Top 5 Claude Code Skills for Algorithmic Trading](https://medium.datadriveninvestor.com/top-5-claude-code-skills-for-algorithmic-trading-49620fa2b02c) (upstream: `ScientiaCapital/skills`/active/signal-generation).
