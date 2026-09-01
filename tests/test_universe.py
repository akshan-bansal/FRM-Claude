from __future__ import annotations

import numpy as np
import pandas as pd

from trading_live_claude.analysis.universe import (
    SEED_UNIVERSE,
    UniverseFilter,
    screen_universe,
    seed_symbols,
    select_universe,
)


def _frame(price: float, volume: float, daily_vol: float, n: int = 80, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, daily_vol, n)
    close = price * np.exp(np.cumsum(rets))
    close = close * (price / close[-1])  # pin last price to `price`
    return pd.DataFrame(
        {
            "time": pd.date_range("2023-01-01", periods=n, freq="B", tz="UTC"),
            "open": close, "high": close * 1.01, "low": close * 0.99,
            "close": close, "volume": np.full(n, volume),
        }
    )


def _pool() -> dict[str, pd.DataFrame]:
    return {
        "GOOD_A": _frame(price=100.0, volume=1_000_000, daily_vol=0.015, seed=1),  # passes
        "GOOD_B": _frame(price=50.0, volume=5_000_000, daily_vol=0.02, seed=2),    # passes, most liquid
        "PENNY": _frame(price=2.0, volume=1_000_000, daily_vol=0.02, seed=3),      # fails min_price
        "ILLIQUID": _frame(price=100.0, volume=100, daily_vol=0.02, seed=4),       # fails dollar-volume
        "DEAD": _frame(price=100.0, volume=1_000_000, daily_vol=0.0005, seed=5),   # fails min vol
        "INSANE": _frame(price=100.0, volume=1_000_000, daily_vol=0.20, seed=6),   # fails max vol
    }


def test_screen_keeps_only_passing_names() -> None:
    members = screen_universe(_pool(), asset_class="equity")
    symbols = {m.symbol for m in members}
    assert symbols == {"GOOD_A", "GOOD_B"}


def test_ranked_by_dollar_volume_desc() -> None:
    members = screen_universe(_pool(), asset_class="equity")
    assert members[0].symbol == "GOOD_B"  # 50 * 5M > 100 * 1M
    assert members[0].avg_dollar_volume >= members[-1].avg_dollar_volume


def test_select_universe_returns_top_n_tickers() -> None:
    picks = select_universe(_pool(), asset_class="equity", top_n=1)
    assert picks == ["GOOD_B"]


def test_filter_thresholds_are_tunable() -> None:
    # Loosen price + volume so PENNY and ILLIQUID both pass.
    loose = UniverseFilter(min_price=1.0, min_dollar_volume=100.0, min_annual_vol=0.05, max_annual_vol=1.5)
    symbols = {m.symbol for m in screen_universe(_pool(), asset_class="equity", filt=loose)}
    assert {"PENNY", "ILLIQUID"}.issubset(symbols)


def test_seed_lists_cover_all_asset_classes() -> None:
    for ac in ("equity", "future", "commodity", "crypto"):
        assert len(seed_symbols(ac)) > 0  # type: ignore[arg-type]
    # Crypto seed now uses the routed slash-form (BTC/USD) to match CRYPTO_SLEEVE and the
    # KrakenBroker pair-mapping table. Kraken's wire code (XBTUSD) is derived at fetch time.
    assert "BTC/USD" in SEED_UNIVERSE["crypto"]


def test_crypto_annualization_differs() -> None:
    # Same daily vol annualizes higher for crypto (365) than equity (252); ensure a
    # borderline name is judged under the right calendar (no crash, sane values).
    pool = {"X": _frame(price=100.0, volume=1_000_000, daily_vol=0.03, seed=9)}
    eq = screen_universe(pool, asset_class="equity")
    cr = screen_universe(pool, asset_class="crypto")
    if eq and cr:
        assert cr[0].annual_vol > eq[0].annual_vol
