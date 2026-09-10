"""Back-adjusted continuous-contract series for futures.

Given the per-contract parquets that :mod:`ib_futures_history` produces (one per historical
delivery month), this module builds a single continuous price series per root. Two adjustment
methods are supported; the default is Panama-Add because it preserves absolute price *differences*
(what indicator-based strategies read), while Ratio preserves absolute price *returns* (what
percent-based strategies read).

Splice logic:

* Contracts are walked oldest-first.
* At the roll date of contract N (``spec.roll_out_date``), the continuous series switches from
  contract N's bars to contract N+1's bars. The "gap" at that date — the difference (or ratio)
  between the two contracts' prices — becomes the adjustment applied to ALL bars of contract N
  and everything older.
* Panama-Add: subtract the cumulative gap from historical prices, so the series is "shifted down"
  to remove the roll discontinuity.
* Ratio: multiply historical prices by cumulative ratio, so returns are preserved.

The result is one row per calendar date with columns:
``time, open, high, low, close, volume, active_contract, cumulative_adjustment``.

Not fabricated data — every row has a real underlying contract; ``cumulative_adjustment`` records
what was applied so a caller can invert if they want the raw contract bars.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

from trading_live_claude.data.futures_calendar import ContractSpec, enumerate_contracts
from trading_live_claude.data.ib_futures_history import (
    DEFAULT_CACHE_ROOT,
    contract_cache_path,
)
from trading_live_claude.logging_setup import get_logger

log = get_logger(__name__)

AdjustmentMethod = Literal["panama_add", "ratio"]


@dataclass(frozen=True)
class SpliceResult:
    """One continuous-series build result. ``bars`` is the joined frame; ``contracts_used`` is
    the ordered list of contracts that contributed, oldest-first."""
    root: str
    method: AdjustmentMethod
    bars: pd.DataFrame
    contracts_used: list[str]


def _load_contract_frame(spec: ContractSpec, cache_root: Path) -> pd.DataFrame | None:
    """Load a per-contract parquet; None if the file is missing or empty."""
    p = contract_cache_path(spec, cache_root)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return df if not df.empty else None


def build_continuous(root: str, *, lookback_years: float = 6.0,
                       method: AdjustmentMethod = "panama_add",
                       cache_root: Path = DEFAULT_CACHE_ROOT) -> SpliceResult:
    """Splice per-contract bars into one continuous back-adjusted series for ``root``.

    Bar selection at each date follows the roll rule: while the calendar day is before
    contract N's ``roll_out_date``, take bars from contract N. On and after that date, take
    from contract N+1, and record the (contract N+1 price - contract N price) at the switch
    as an add-adjustment applied to all older bars (Panama) or the ratio (Ratio method).

    Runs in-memory; the concatenated frame for 6y of daily bars per root is tens of KB, well
    under any memory concern.
    """
    contracts = enumerate_contracts(root, lookback_years=lookback_years)
    # Oldest-first for the walk so we accumulate adjustments in chronological order.
    contracts = sorted(contracts, key=lambda c: c.expiration_approx)

    # Load per-contract frames. Skip contracts with no bars on disk.
    per_contract: list[tuple[ContractSpec, pd.DataFrame]] = []
    for spec in contracts:
        df = _load_contract_frame(spec, cache_root)
        if df is None:
            continue
        per_contract.append((spec, df.copy()))

    if not per_contract:
        return SpliceResult(root=root, method=method,
                             bars=pd.DataFrame(columns=["time", "open", "high", "low", "close",
                                                          "volume", "active_contract",
                                                          "cumulative_adjustment"]),
                             contracts_used=[])

    # For each contract, keep only bars up to that contract's roll_out_date (or the whole frame
    # for the last, currently-front contract).
    trimmed: list[tuple[ContractSpec, pd.DataFrame]] = []
    for i, (spec, df) in enumerate(per_contract):
        is_last = i == len(per_contract) - 1
        if is_last:
            trimmed.append((spec, df))
        else:
            df_kept = df[df["time"].dt.date < spec.roll_out_date].copy()
            trimmed.append((spec, df_kept))

    # Walk oldest → newest; at each pair boundary compute the gap and register a cumulative
    # adjustment that flows BACKWARDS through the prior bars. Panama-Add subtracts the gap from
    # every prior close (open/high/low too); Ratio multiplies by the ratio.
    #
    # gap_i = (first-bar close of contract N+1) - (last-bar close of contract N)
    # cum_add_from_i backward: apply gap_i to every bar of contract 0..N (walking backwards).
    #
    # Concretely: build the joined frame with no adjustment first, then apply cumulative
    # adjustments in a second pass.
    joined = pd.concat(
        [df.assign(active_contract=spec.local_symbol) for spec, df in trimmed if not df.empty],
        ignore_index=True,
    ).sort_values("time").reset_index(drop=True)

    if joined.empty:
        return SpliceResult(root=root, method=method, bars=joined,
                             contracts_used=[s.local_symbol for s, _ in trimmed])

    # Compute per-contract last-close and next-contract first-close aligned by contract order.
    contract_order = [s.local_symbol for s, df in trimmed if not df.empty]

    # cumulative_adjustment for each row starts at 0 (Panama) or 1 (Ratio); adjustments are
    # applied bar-by-bar based on which contract that bar came from.
    if method == "panama_add":
        # Compute gaps in newest-to-oldest order; sum for each contract to apply backwards.
        # gaps[i] = close_first_of_next - close_last_of_this for pairs (contract_i, contract_{i+1})
        by_contract: dict[str, pd.DataFrame] = {c: joined[joined["active_contract"] == c] for c in contract_order}
        gaps: list[float] = []
        for a, b in zip(contract_order[:-1], contract_order[1:], strict=False):
            gap = float(by_contract[b]["close"].iloc[0] - by_contract[a]["close"].iloc[-1])
            gaps.append(gap)
        # cum_add[i] for contract i = sum of gaps[j] for j >= i
        cum_add: dict[str, float] = {}
        running = 0.0
        for i in range(len(contract_order) - 1, -1, -1):
            cum_add[contract_order[i]] = running
            if i > 0:
                running += gaps[i - 1]
        joined["cumulative_adjustment"] = joined["active_contract"].map(cum_add)
        for col in ("open", "high", "low", "close"):
            joined[col] = joined[col] + joined["cumulative_adjustment"]
    else:  # ratio
        by_contract = {c: joined[joined["active_contract"] == c] for c in contract_order}
        ratios: list[float] = []
        for a, b in zip(contract_order[:-1], contract_order[1:], strict=False):
            denom = float(by_contract[a]["close"].iloc[-1]) or 1e-9
            r = float(by_contract[b]["close"].iloc[0]) / denom
            ratios.append(r)
        cum_ratio: dict[str, float] = {}
        running_r = 1.0
        for i in range(len(contract_order) - 1, -1, -1):
            cum_ratio[contract_order[i]] = running_r
            if i > 0:
                running_r *= ratios[i - 1]
        joined["cumulative_adjustment"] = joined["active_contract"].map(cum_ratio)
        for col in ("open", "high", "low", "close"):
            joined[col] = joined[col] * joined["cumulative_adjustment"]

    joined = joined.sort_values("time").reset_index(drop=True)
    return SpliceResult(root=root, method=method, bars=joined,
                         contracts_used=contract_order)


def continuous_cache_path(root: str, method: AdjustmentMethod = "panama_add",
                             cache_root: Path = DEFAULT_CACHE_ROOT) -> Path:
    """Standardized on-disk name so downstream (walk-forward, dashboard) can find it."""
    return cache_root / f"{root}_continuous_{method}.parquet"


def build_and_save(root: str, *, lookback_years: float = 6.0,
                     method: AdjustmentMethod = "panama_add",
                     cache_root: Path = DEFAULT_CACHE_ROOT) -> Path:
    """Build the continuous series and persist it. Returns the path written to."""
    result = build_continuous(root, lookback_years=lookback_years, method=method,
                                cache_root=cache_root)
    path = continuous_cache_path(root, method, cache_root)
    cache_root.mkdir(parents=True, exist_ok=True)
    result.bars.to_parquet(path, index=False)
    log.info("futures.continuous.built", root=root, method=method, bars=len(result.bars),
             contracts_used=len(result.contracts_used), path=str(path))
    return path


__all__ = [
    "AdjustmentMethod",
    "SpliceResult",
    "build_continuous",
    "build_and_save",
    "continuous_cache_path",
]
