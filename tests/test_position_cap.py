from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Position, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.risk.position_cap import (
    RealizedVolatility,
    VolScaledPositionCap,
    annualised_vol,
)


def _closes(daily_vol: float, n: int = 120, seed: int = 0) -> pd.Series:
    rets = np.random.default_rng(seed).normal(scale=daily_vol, size=n)
    return pd.Series(100 * np.cumprod(1 + rets))


def test_annualised_vol_takes_the_higher_window() -> None:
    calm, spike = _closes(0.005, 100, 1), _closes(0.03, 20, 2)
    series = pd.concat([calm, spike * (calm.iloc[-1] / spike.iloc[0])], ignore_index=True)
    vol = annualised_vol(series, periods_per_year=252)
    short = float(series.pct_change().dropna().tail(20).std(ddof=1)) * math.sqrt(252)
    assert vol == pytest.approx(short)
    assert annualised_vol(series.head(10), periods_per_year=252) is None


@pytest.mark.parametrize(("vol", "cap"), [
    (0.20, 0.50), (0.40, 0.25), (0.10, 0.75), (0.05, 0.75), (5.0, 0.05), (None, 0.05), (float("nan"), 0.05),
])
def test_cap_scales_inversely_with_vol_within_floor_and_ceiling(vol, cap) -> None:
    rule = VolScaledPositionCap(lambda _s: vol, base_pct=0.5, ref_vol=0.2, floor_pct=0.05, ceiling_pct=0.75)
    assert rule("X") == pytest.approx(cap)


class _Market:
    def __init__(self) -> None:
        self.calls = 0

    def recent(self, symbol: str, bars: int = 200, interval: str = "1d") -> pd.DataFrame:
        self.calls += 1
        return pd.DataFrame({"close": _closes(0.02, bars)})


def test_realized_vol_is_cached_and_crypto_annualises_over_365_days() -> None:
    clock = [0.0]
    market = _Market()
    vols = RealizedVolatility(market, refresh_s=100, clock=lambda: clock[0])
    stock, coin = vols("AAPL"), vols("BTC/USD")
    assert coin == pytest.approx(stock * math.sqrt(365 / 252))
    vols("AAPL")
    assert market.calls == 2
    clock[0] = 101
    vols("AAPL")
    assert market.calls == 3


class _Feed:
    name = "fake"
    venue = "fake"

    def __init__(self) -> None:
        self.px = {"AAA": 100.0, "BBB": 100.0}

    def quote(self, symbol: str) -> Quote:
        p = self.px[symbol]
        return Quote(symbol=symbol, symbolId=1, bidPrice=p, askPrice=p, lastTradePrice=p)

    def quotes(self, symbols): return [self.quote(s) for s in symbols]
    def accounts(self): return []
    def positions(self, _): return []
    def candles(self, *a, **k): return []
    def equity(self, *a, **k): return 0.0
    def place_order(self, order): return order
    def cancel_order(self, *a, **k): pass


def _buy(symbol: str, shares: int) -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=shares, entry=100.0, stop=99.0,
                       target=None, strategy="t", risk_dollars=shares * 1.0, account_number="PAPER-001",
                       symbolId=1)


def _paper_router(tmp_path: Path, cap_for=None) -> tuple[PaperBroker, Router]:
    paper = PaperBroker(feed=_Feed(), slippage_bps=0.0, commission_per_trade=0.0, journal_dir=tmp_path)
    router = Router.build_default(mode="paper", broker=paper, state_dir=tmp_path, cap_pct=1.0,
                                  max_open_positions=10, position_cap_pct_for=cap_for)
    return paper, router


def test_high_vol_name_is_trimmed_to_its_smaller_dynamic_cap(tmp_path: Path) -> None:
    _paper, router = _paper_router(tmp_path, cap_for=lambda s: 0.10 if s == "AAA" else 0.50)
    risky = _buy("AAA", 300)
    assert router._gate(risky, equity=100_000, existing_risk=0, open_positions=0).accepted
    assert risky.shares == 100                                   # 10% of 100k at $100
    calm = _buy("BBB", 300)
    router._gate(calm, equity=100_000, existing_risk=0, open_positions=0)
    assert calm.shares == 300                                    # 30% fits a 50% cap


def test_cap_counts_what_is_already_held_in_the_name(tmp_path: Path) -> None:
    paper, router = _paper_router(tmp_path, cap_for=lambda _s: 0.30)
    paper.place_order(Order(symbol="AAA", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=250))
    add = _buy("AAA", 200)
    assert router._gate(add, equity=100_000, existing_risk=0, open_positions=1).accepted
    assert add.shares == 50                                      # 25k held + 5k = 30% cap


def test_gross_leverage_is_enforced_when_caller_omits_open_notional(tmp_path: Path) -> None:
    paper, router = _paper_router(tmp_path)
    paper.place_order(Order(symbol="AAA", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=450))
    paper.place_order(Order(symbol="BBB", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=450))
    more = _buy("BBB", 300)                                       # 90k open; only 10k headroom
    assert router._gate(more, equity=100_000, existing_risk=0, open_positions=2).accepted
    assert more.shares == 50                                     # 45k already in BBB caps at 50%


def test_unreadable_positions_refuse_entries_but_not_exits(tmp_path: Path) -> None:
    paper, router = _paper_router(tmp_path)

    def boom(_acct: str) -> list[Position]:
        raise ConnectionError("broker down")

    paper.positions = boom  # type: ignore[method-assign]
    entry = router._gate(_buy("AAA", 10), equity=100_000, existing_risk=0, open_positions=0)
    assert "open notional unavailable" in " ".join(entry.rejected_reasons)
    exit_intent = OrderIntent(symbol="AAA", action=OrderAction.SELL, shares=10, entry=100.0,
                              stop=101.0, target=None, strategy="t", risk_dollars=10.0,
                              account_number="PAPER-001")
    assert router._gate(exit_intent, equity=100_000, existing_risk=0, open_positions=1).accepted
