from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from trading_live_claude.brokers.base import OrderRejected, StaleQuote
from trading_live_claude.brokers.fresh import FreshQuoteBroker
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.monitor.live_loop import LiveMonitor
from trading_live_claude.strategies.base import Strategy, StrategyContext

T0 = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _q(symbol: str = "BTC/USD", bid: float | None = 100.0, ask: float | None = 100.2, **kw) -> Quote:
    return Quote(symbol=symbol, symbolId=1, bidPrice=bid, askPrice=ask,
                 lastTradePrice=kw.pop("last", 100.1), **kw)


class _Feed:
    """Returns scripted quotes per symbol; the last scripted quote repeats."""

    name = "fake"
    venue = "kraken"

    def __init__(self, script: dict[str, list[Quote]]) -> None:
        self.script = script
        self.calls: list[list[str]] = []

    def _next(self, symbol: str) -> Quote:
        seq = self.script[symbol]
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def quote(self, symbol: str) -> Quote:
        self.calls.append([symbol])
        return self._next(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        self.calls.append(list(symbols))
        return [self._next(s) for s in symbols]

    def accounts(self): return []
    def positions(self, _): return []
    def candles(self, *a, **k): return []
    def equity(self, *a, **k): return 0.0
    def place_order(self, order): return order
    def cancel_order(self, *a, **k): pass


def test_fresh_quote_passes_with_a_single_fetch() -> None:
    feed = _Feed({"BTC/USD": [_q()]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    assert guard.quote("BTC/USD").bidPrice == 100.0
    assert len(feed.calls) == 1
    assert guard.venue == "kraken" and guard.name == "fake"


def test_missing_price_is_refetched_and_recovers() -> None:
    feed = _Feed({"BTC/USD": [_q(bid=None, ask=None, last=None), _q()]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    assert guard.quote("BTC/USD").mid is not None
    assert len(feed.calls) == 2
    assert not guard.is_stale("BTC/USD")


def test_halted_raises_and_retries_only_once_per_episode() -> None:
    feed = _Feed({"XIC.TO": [_q("XIC.TO", isHalted=True)]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    with pytest.raises(StaleQuote, match="halted"):
        guard.quote("XIC.TO")
    assert len(feed.calls) == 2
    with pytest.raises(StaleQuote):
        guard.quote("XIC.TO")
    assert len(feed.calls) == 3          # still stale: read through, no extra retry


def test_frozen_quote_goes_stale_at_threshold_and_recovers_on_change() -> None:
    clock = _Clock()
    feed = _Feed({"XLM/USD": [_q("XLM/USD", bid=0.1, ask=0.11, last=0.105)]})
    guard = FreshQuoteBroker(feed, max_frozen_s=900, clock=clock)
    guard.quote("XLM/USD")
    clock.advance(600)
    guard.quote("XLM/USD")
    clock.advance(300)
    with pytest.raises(StaleQuote, match="unchanged for 900s"):
        guard.quote("XLM/USD")
    feed.script["XLM/USD"] = [_q("XLM/USD", bid=0.1, ask=0.111, last=0.105)]
    clock.advance(300)
    assert guard.quote("XLM/USD").askPrice == 0.111
    assert not guard.is_stale("XLM/USD")


@pytest.mark.parametrize(("quote", "reason"), [
    (_q(delay=15), "delayed 15m"),
    (_q(bid=101.0, ask=100.0), "crossed book"),
    (_q(lastTradeTime=T0 - timedelta(hours=2)), "last trade 7200s old"),
])
def test_questrade_style_staleness_signals(quote: Quote, reason: str) -> None:
    guard = FreshQuoteBroker(_Feed({"BTC/USD": [quote]}), max_trade_age_s=3600, clock=_Clock())
    with pytest.raises(StaleQuote, match=reason):
        guard.quote("BTC/USD")


def test_warn_mode_returns_the_stale_quote() -> None:
    guard = FreshQuoteBroker(_Feed({"BTC/USD": [_q(isHalted=True)]}), on_stale="warn", clock=_Clock())
    assert guard.quote("BTC/USD").isHalted
    assert guard.is_stale("BTC/USD")


def test_batch_refetches_only_the_stale_subset() -> None:
    feed = _Feed({"BTC/USD": [_q()], "ETH/USD": [_q("ETH/USD", isHalted=True)]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    with pytest.raises(StaleQuote, match="ETH/USD"):
        guard.quotes(["BTC/USD", "ETH/USD"])
    assert feed.calls == [["BTC/USD", "ETH/USD"], ["ETH/USD"]]
    assert not guard.is_stale("BTC/USD")


def test_paper_fill_on_stale_quote_is_rejected_and_journaled(tmp_path: Path) -> None:
    feed = _Feed({"BTC/USD": [_q(isHalted=True)]})
    paper = PaperBroker(feed=FreshQuoteBroker(feed, clock=_Clock()), journal_dir=tmp_path)
    order = Order(symbol="BTC/USD", symbolId=1, action=OrderAction.BUY,
                  orderType=OrderType.MARKET, totalQuantity=1)
    with pytest.raises(OrderRejected, match="stale quote"):
        paper.place_order(order)
    journal = (tmp_path / "paper_orders.jsonl").read_text()
    assert "stale_quote: halted" in journal


def test_router_journals_stale_quote_rejection(tmp_path: Path) -> None:
    feed = _Feed({"BTC/USD": [_q(isHalted=True)]})
    paper = PaperBroker(feed=FreshQuoteBroker(feed, clock=_Clock()), journal_dir=tmp_path)
    router = Router.build_default(mode="paper", broker=paper, state_dir=tmp_path)
    intent = OrderIntent(symbol="BTC/USD", action=OrderAction.BUY, shares=10, entry=100.0,
                         stop=96.0, target=108.0, strategy="macd", risk_dollars=40.0,
                         account_number="PAPER-001", symbolId=1)
    assert router.submit(intent, equity=100_000, existing_risk=0, open_positions=0) is None
    assert "stale quote" in router.journal.rejected_path.read_text()


def test_mark_to_market_keeps_last_good_price_when_quote_goes_stale(tmp_path: Path) -> None:
    clock = _Clock()
    feed = _Feed({"BTC/USD": [_q(bid=100.0, ask=100.0)]})
    paper = PaperBroker(feed=FreshQuoteBroker(feed, max_frozen_s=900, clock=clock),
                        slippage_bps=0.0, journal_dir=tmp_path)
    paper.place_order(Order(symbol="BTC/USD", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=1))
    feed.script["BTC/USD"] = [_q(bid=90.0, ask=90.0)]
    clock.advance(60)
    paper.mark_to_market()
    clock.advance(1000)
    paper.mark_to_market()                   # frozen now; must not raise or change the mark
    assert paper.positions("PAPER-001")[0].currentPrice == 90.0


class _AlwaysEnter(Strategy):
    name = "always_enter"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"] = 1
        out["exit"] = 0
        out["atr"] = 1.0
        return out


@dataclass
class _Pos:
    symbol: str
    openQuantity: float
    currentPrice: float


class _MonitorBroker:
    name = "fake"

    def __init__(self, guard: FreshQuoteBroker) -> None:
        self.guard = guard

    def equity(self, *_a, **_k) -> float:
        return 100_000.0

    def positions(self, _acct: str) -> list[_Pos]:
        return [_Pos("HELD", 5, 42.0)]

    def quote(self, symbol: str) -> Quote:
        return self.guard.quote(symbol)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        n = bars + 2
        return pd.DataFrame({"close": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n})


class _Router:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    def submit(self, intent, **_kw):
        self.symbols.append(intent.symbol)
        return None


def test_monitor_does_not_poll_closed_venues(monkeypatch) -> None:
    from trading_live_claude.monitor import live_loop
    from trading_live_claude.risk.sizing import PositionSizer

    feed = _Feed({"HELD": [_q("HELD")], "SHUT": [_q("SHUT")], "LIVE": [_q("LIVE", bid=10.0, ask=10.02)]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    seen_px: list[float] = []
    real_risk = live_loop.per_trade_risk
    monkeypatch.setattr(live_loop, "per_trade_risk",
                        lambda qty, px, **kw: seen_px.append(px) or real_risk(qty, px, **kw))
    router = _Router()
    monitor = LiveMonitor(broker=_MonitorBroker(guard), market=_Market(), strategy=_AlwaysEnter(),
                          sizer=PositionSizer(risk_pct=0.01), router=router, account_number="A",
                          symbols=["SHUT", "LIVE"], risk_model="atr", heat_aggregation="sum",
                          market_open_for=lambda s: s == "LIVE")
    monitor.step()
    assert seen_px[0] == 42.0                        # closed held position at its last mark
    assert [c[0] for c in feed.calls] == ["LIVE"]    # no quote call for HELD or SHUT
    assert router.symbols == ["LIVE"]


def test_monitor_skips_only_the_stale_symbol_and_still_counts_held_risk(monkeypatch) -> None:
    from trading_live_claude.monitor import live_loop
    from trading_live_claude.risk.sizing import PositionSizer

    feed = _Feed({"HELD": [_q("HELD", isHalted=True)], "FROZEN": [_q("FROZEN", isHalted=True)],
                  "LIVE": [_q("LIVE", bid=10.0, ask=10.02)]})
    guard = FreshQuoteBroker(feed, clock=_Clock())
    seen_px: list[float] = []
    real_risk = live_loop.per_trade_risk

    def spy(qty, px, **kw):
        seen_px.append(px)
        return real_risk(qty, px, **kw)

    monkeypatch.setattr(live_loop, "per_trade_risk", spy)
    router = _Router()
    monitor = LiveMonitor(broker=_MonitorBroker(guard), market=_Market(), strategy=_AlwaysEnter(),
                          sizer=PositionSizer(risk_pct=0.01), router=router, account_number="A",
                          symbols=["FROZEN", "LIVE"], risk_model="atr", heat_aggregation="sum")
    monitor.step()
    assert seen_px[0] == 42.0                # held position counted at its last mark, not $0
    assert "FROZEN" not in router.symbols
    assert "LIVE" in router.symbols
