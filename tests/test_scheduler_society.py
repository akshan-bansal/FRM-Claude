"""SessionRouter x simulated-society influence.

The scheduler gates a COPY of an entry (the "probe") to learn its post-trim size, rounds that to a
board lot, then submits the original. A society scaling recorded only on the copy would be applied a
second time by the real gate pass, and a copy that was merely rejected must not mark the original as
handled. Synthetic throughout.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from trading_live_claude.brokers.base import StaleQuote
from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.execution.scheduler import MicrostructureConfig, SessionRouter

TOKYO_OPEN = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)          # Monday 10:00 JST, inside the session
GATES = dict(equity=100_000.0, existing_risk=0.0, open_positions=0)


class _Feed:
    name = "fake"
    venue = "fake"

    def __init__(self) -> None:
        self.prices: dict[str, tuple[float, float]] = {}

    def quote(self, symbol: str) -> Quote:
        if symbol not in self.prices:
            raise StaleQuote(symbol, ("no price",))
        bid, ask = self.prices[symbol]
        return Quote(symbol=symbol, symbolId=1, bidPrice=bid, askPrice=ask, lastTradePrice=bid)

    def quotes(self, symbols):
        return [self.quote(s) for s in symbols]

    def accounts(self): return []
    def positions(self, _): return []
    def candles(self, *a, **k): return []
    def equity(self, *a, **k): return 0.0
    def place_order(self, order): return order
    def cancel_order(self, *a, **k): pass


def _setup(tmp_path: Path):
    feed = _Feed()
    feed.prices["7203.T"] = (20.49, 20.51)
    paper = PaperBroker(feed=feed, journal_dir=tmp_path, slippage_bps=0.0)
    inner = Router.build_default(mode="paper", broker=paper, state_dir=tmp_path)
    router = SessionRouter(inner, paper, account_number="PAPER-001", config=MicrostructureConfig(),
                           journal_path=tmp_path / "scheduled_intents.jsonl", clock=lambda: TOKYO_OPEN)
    return paper, inner, router


def _buy(shares: int, entry: float = 20.0, stop: float = 19.0) -> OrderIntent:
    return OrderIntent(symbol="7203.T", action=OrderAction.BUY, shares=shares, entry=entry, stop=stop,
                       target=None, strategy="t", risk_dollars=shares * (entry - stop),
                       account_number="PAPER-001", symbolId=1)


def test_a_society_scaling_is_applied_once_through_the_lot_probe(tmp_path: Path) -> None:
    _paper, inner, router = _setup(tmp_path)
    inner.society_view = lambda sym: (0.55, "run-1")
    intent = _buy(1000)
    order = router.submit(intent, **GATES)
    # probe: 1000 x 0.55 = 550 -> whole 100-share lots = 500. A second scaling would give 275.
    assert order is not None and order.totalQuantity == 500
    assert intent.shares == 500 and intent.society_influence == 0.55 and intent.society_applied


def test_society_scaling_that_zeroes_the_probe_still_rejects_the_real_intent(tmp_path: Path) -> None:
    paper, inner, router = _setup(tmp_path)
    inner.society_view = lambda sym: (0.005, "run-1")             # 100 x 0.005 = 0.5 -> 0 shares
    intent = _buy(100)
    assert router.submit(intent, **GATES) is None                 # NOT placed at the full 100
    assert paper.positions("PAPER-001") == []
    rejected = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8")
    assert "trims 100 shares to 0" in rejected


def test_no_view_leaves_the_lot_rounded_size_alone(tmp_path: Path) -> None:
    _paper, inner, router = _setup(tmp_path)
    assert inner.society_view is None
    order = router.submit(_buy(1000), **GATES)
    assert order is not None and order.totalQuantity == 1000
