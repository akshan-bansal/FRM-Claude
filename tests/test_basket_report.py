"""Basket analysis: what evidence exists per symbol, and honest gaps where it doesn't."""
from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.analysis.basket_report import build_report
from trading_live_claude.analysis.universe import CRYPTO_SLEEVE, WALK_FORWARD_VALIDATED
from trading_live_claude.intel.overlay import IntelSnapshot


def _close(seed: int, n: int = 200) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n)))


def test_evidence_kinds_are_kept_apart() -> None:
    wf = next(iter(WALK_FORWARD_VALIDATED))
    rep = build_report("qt", [wf, "ZZZZ"])
    kinds = {r["symbol"]: r["evidence"]["kind"] for r in rep["rows"]}
    assert kinds[wf] == "walk_forward" and kinds["ZZZZ"] == "none"
    assert rep["scope"]["walk_forward"] == 1 and rep["scope"]["no_evidence"] == 1


def test_crypto_screen_is_labelled_in_sample_not_walk_forward() -> None:
    sym = next(iter(CRYPTO_SLEEVE))
    ev = build_report("kraken", [sym])["rows"][0]["evidence"]
    assert ev["kind"] == "screened_in_sample" and "oos_score" not in ev and "screen_score" in ev


def test_ib_refuses_equities_with_a_reason() -> None:
    rep = build_report("ib", ["AAPL", "GC"])
    by = {r["symbol"]: r for r in rep["rows"]}
    assert by["AAPL"]["policy_refusal"] and by["GC"]["policy_refusal"] is None
    assert rep["scope"]["refused"] == 1


def test_no_closes_means_no_statistics_and_the_row_says_why() -> None:
    row = build_report("kraken", ["BTC/USD"])["rows"][0]
    assert row["stats"] is None and "no daily closes" in row["stats_missing"]


def test_short_history_gets_no_statistics() -> None:
    row = build_report("kraken", ["BTC/USD"], closes={"BTC/USD": _close(1, 30)})["rows"][0]
    assert row["stats"] is None and "fewer than" in row["stats_missing"]


def test_correlation_and_weights_cover_only_symbols_with_stats() -> None:
    rep = build_report("kraken", ["BTC/USD", "ETH/USD", "XRP/USD"],
                       closes={"BTC/USD": _close(1), "ETH/USD": _close(2)})
    assert rep["scope"]["with_stats"] == 2
    assert rep["correlation"] is not None and set(rep["correlation"]["max_pair"]) == {"BTC/USD", "ETH/USD"}
    assert set(rep["allocator_weights"]) <= {"BTC/USD", "ETH/USD"}


def test_refused_symbols_stay_out_of_the_aggregates() -> None:
    rep = build_report("ib", ["AAPL", "GC", "SI"],
                       closes={"AAPL": _close(1), "GC": _close(2), "SI": _close(3)})
    assert rep["scope"]["with_stats"] == 2 and "AAPL" not in rep["allocator_weights"]


def test_stale_energy_source_withholds_energy_theses() -> None:
    snap = IntelSnapshot(energy_stress=0.9, strategic_risk=80.0, source_age_hours={"energy": 135.0})
    rep = build_report("kraken", ["BTC/USD"], snapshot=snap)
    assert all("energy" not in k or k == "theses:energy" for k in rep["withheld"])


def test_launch_map_strategy_is_labelled_not_presented_as_evidence() -> None:
    row = build_report("qt", ["ZZZZ"], launch_map={"ZZZZ": "bollinger"})["rows"][0]
    assert row["strategy"] == "bollinger" and row["strategy_source"] == "launch_map"
    assert row["evidence"]["kind"] == "none"
    fb = build_report("ib", ["GC"], fallback_strategy="bollinger")["rows"][0]
    assert fb["strategy_source"] == "fallback"
