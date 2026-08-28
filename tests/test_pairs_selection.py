from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.analysis.pairs import (
    enumerate_pairs,
    pair_frame,
    select_cointegrated_pairs,
)


def _basket() -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(11)
    n = 500
    t = pd.date_range("2022-01-01", periods=n, freq="D", tz="UTC")
    base = np.cumsum(rng.normal(0, 1, n)) + 100.0
    ou = np.zeros(n)
    for i in range(1, n):
        ou[i] = 0.85 * ou[i - 1] + rng.normal(0, 0.5)
    a = base                       # leg A
    b = 5.0 + 1.4 * base + ou      # leg B cointegrated with A
    c = np.cumsum(rng.normal(0, 1, n)) + 50.0  # independent walk

    def frame(px: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame({"time": t, "open": px, "high": px, "low": px, "close": px, "volume": 1e6})

    return {"AAA": frame(a), "BBB": frame(b), "CCC": frame(c)}


def test_enumerate_covers_all_pairs() -> None:
    cands = enumerate_pairs(_basket(), min_obs=200)
    assert len(cands) == 3  # C(3,2)
    keys = {frozenset({c.sym_y, c.sym_x}) for c in cands}
    assert keys == {frozenset({"AAA", "BBB"}), frozenset({"AAA", "CCC"}), frozenset({"BBB", "CCC"})}


def test_cointegrated_pair_ranks_first_and_is_selected() -> None:
    picks = select_cointegrated_pairs(_basket(), min_obs=200)
    assert len(picks) >= 1
    top = picks[0]
    assert {top.sym_y, top.sym_x} == {"AAA", "BBB"}
    assert top.tradeable and top.pvalue < 0.05
    # The independent leg CCC should not form a tradeable pair with anyone.
    assert all("CCC" not in {p.sym_y, p.sym_x} for p in picks)


def test_pair_frame_builds_close_and_close_b() -> None:
    dfs = _basket()
    frame = pair_frame(dfs, "BBB", "AAA")
    assert {"close", "close_b"}.issubset(frame.columns)
    assert len(frame) == len(dfs["BBB"])
    assert frame["close_b"].notna().all()
