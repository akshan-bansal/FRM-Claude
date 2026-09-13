from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.brokers.base import StaleQuote
from trading_live_claude.brokers.models import Order, OrderAction, OrderType, Quote
from trading_live_claude.brokers.paper import PaperBroker
from trading_live_claude.brokers.routed import VenueRoutedFeed
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.execution.scheduler import MicrostructureConfig, SessionRouter, spread_bps
from trading_live_claude.monitor.live_loop import LiveMonitor
from trading_live_claude.risk.risk_model import lead_lag_corr, portfolio_risk
from trading_live_claude.strategies.base import Strategy, StrategyContext
from trading_live_claude.venues import VENUES

MON = datetime(2026, 9, 14, tzinfo=UTC)          # Monday


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


class _Feed:
    name = "fake"
    venue = "fake"

    def __init__(self) -> None:
        self.prices: dict[str, tuple[float, float]] = {}
        self.stale: set[str] = set()

    def quote(self, symbol: str) -> Quote:
        if symbol in self.stale:
            raise StaleQuote(symbol, ("halted",))
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


def _setup(tmp_path: Path, now: datetime, **cfg):
    clock, feed = _Clock(now), _Feed()
    paper = PaperBroker(feed=feed, journal_dir=tmp_path, slippage_bps=0.0)
    inner = Router.build_default(mode="paper", broker=paper, state_dir=tmp_path)
    router = SessionRouter(inner, paper, account_number="PAPER-001",
                           config=MicrostructureConfig(**cfg),
                           journal_path=tmp_path / "scheduled_intents.jsonl", clock=clock)
    return clock, feed, paper, router


def _buy(symbol: str, shares: int = 250, entry: float = 20.0, stop: float = 19.0) -> OrderIntent:
    return OrderIntent(symbol=symbol, action=OrderAction.BUY, shares=shares, entry=entry, stop=stop,
                       target=entry * 1.1, strategy="t", risk_dollars=shares * (entry - stop),
                       account_number="PAPER-001", symbolId=1)


GATES = dict(equity=100_000.0, existing_risk=0.0, open_positions=0)


def _events(tmp_path: Path) -> list[str]:
    rows = (tmp_path / "scheduled_intents.jsonl").read_text().splitlines()
    return [json.loads(r)["event"] for r in rows]


# ----- venue windows ---------------------------------------------------------------------------

def test_open_and_close_buffers_shrink_the_tradeable_window() -> None:
    us = VENUES["US"]
    assert us.is_open(MON.replace(hour=13, minute=32))
    assert not us.tradeable(MON.replace(hour=13, minute=32), open_buffer_min=5)
    assert us.tradeable(MON.replace(hour=13, minute=35), open_buffer_min=5)
    assert not us.tradeable(MON.replace(hour=19, minute=52), close_buffer_min=10)


def test_next_tradeable_start_skips_weekends_and_lunch() -> None:
    friday_after_close = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)
    assert VENUES["US"].next_tradeable_start(friday_after_close, open_buffer_min=5) == \
        datetime(2026, 9, 21, 13, 35, tzinfo=UTC)
    tokyo_lunch = MON.replace(hour=2, minute=45)      # 11:45 JST
    assert VENUES["TSEJ"].next_tradeable_start(tokyo_lunch, open_buffer_min=5) == \
        MON.replace(hour=3, minute=35)                # 12:35 JST
    assert VENUES["CRYPTO"].next_tradeable_start(tokyo_lunch) == tokyo_lunch


# ----- queue + release -------------------------------------------------------------------------

def test_closed_venue_intent_is_queued_then_released_repriced_and_lot_rounded(tmp_path: Path) -> None:
    clock, feed, paper, router = _setup(tmp_path, MON.replace(hour=20))
    assert router.submit(_buy("7203.T"), **GATES) is None
    assert router.submit(_buy("7203.T"), **GATES) is None
    (item,) = router.queue.values()
    assert item.release_at == datetime(2026, 9, 15, 0, 5, tzinfo=UTC)   # 09:05 JST

    feed.prices["7203.T"] = (20.49, 20.51)
    clock.set(datetime(2026, 9, 15, 0, 6, tzinfo=UTC))
    (order,) = router.release_due(**GATES)
    assert order.totalQuantity == 200                                 # 250 -> whole 100-share lots
    pos = paper.positions("PAPER-001")[0]
    assert pos.symbol == "7203.T" and pos.openQuantity == 200
    assert router.queue == {}
    assert _events(tmp_path) == ["queued", "replaced", "released"]


def test_release_shifts_stop_with_the_open_and_expires_on_a_gap_through_stop(tmp_path: Path) -> None:
    clock, feed, _paper, router = _setup(tmp_path, MON.replace(hour=20))
    router.submit(_buy("7203.T", shares=100, entry=20.0, stop=19.0), **GATES)
    feed.prices["7203.T"] = (18.9, 18.95)
    clock.set(datetime(2026, 9, 15, 0, 6, tzinfo=UTC))
    assert router.release_due(**GATES) == []
    assert _events(tmp_path)[-1] == "expired"


def test_queued_intent_expires_after_ttl(tmp_path: Path) -> None:
    clock, feed, _paper, router = _setup(tmp_path, MON.replace(hour=20), intent_ttl_min=30)
    router.submit(_buy("AAPL"), **GATES)
    clock.set(datetime(2026, 9, 15, 14, 10, tzinfo=UTC))            # release 13:35, expiry 14:05
    feed.prices["AAPL"] = (20.0, 20.01)
    assert router.release_due(**GATES) == []
    assert _events(tmp_path)[-1] == "expired"


def test_stale_quote_at_release_keeps_the_intent_queued(tmp_path: Path) -> None:
    clock, feed, _paper, router = _setup(tmp_path, MON.replace(hour=20))
    router.submit(_buy("AAPL"), **GATES)
    feed.stale.add("AAPL")
    clock.set(datetime(2026, 9, 15, 13, 36, tzinfo=UTC))
    assert router.release_due(**GATES) == []
    assert len(router.queue) == 1
    feed.stale.clear()
    feed.prices["AAPL"] = (20.0, 20.01)
    assert len(router.release_due(**GATES)) == 1


def test_open_auction_buffer_queues_instead_of_trading(tmp_path: Path) -> None:
    _clock, feed, _paper, router = _setup(tmp_path, MON.replace(hour=13, minute=31))
    feed.prices["AAPL"] = (20.0, 20.01)
    assert router.submit(_buy("AAPL"), **GATES) is None
    assert next(iter(router.queue.values())).release_at == MON.replace(hour=13, minute=35)


# ----- microstructure controls -----------------------------------------------------------------

def test_wide_spread_blocks_entries_but_never_exits(tmp_path: Path) -> None:
    _clock, feed, paper, router = _setup(tmp_path, MON.replace(hour=15), max_spread_bps_equity=50)
    feed.prices["AAPL"] = (20.0, 20.4)                               # ~198 bps
    assert router.submit(_buy("AAPL"), **GATES) is None
    assert "spread" in (tmp_path / "rejected.jsonl").read_text()

    feed.prices["AAPL"] = (20.0, 20.01)
    paper.place_order(Order(symbol="AAPL", symbolId=1, action=OrderAction.BUY,
                            orderType=OrderType.MARKET, totalQuantity=10))
    feed.prices["AAPL"] = (20.0, 20.4)
    exit_intent = OrderIntent(symbol="AAPL", action=OrderAction.SELL, shares=10, entry=20.2,
                              stop=22.2, target=None, strategy="t", risk_dollars=20.0,
                              account_number="PAPER-001", symbolId=1)
    assert router.submit(exit_intent, **GATES | {"open_positions": 1}) is not None
    assert paper.positions("PAPER-001") == []


def test_hong_kong_needs_a_configured_board_lot(tmp_path: Path) -> None:
    at = MON.replace(hour=2)                                          # 10:00 HKT
    _clock, feed, _paper, router = _setup(tmp_path, at)
    feed.prices["0700.HK"] = (20.0, 20.01)
    assert router.submit(_buy("0700.HK"), **GATES) is None
    assert "board lot unknown" in (tmp_path / "rejected.jsonl").read_text()

    _clock, feed, _paper, router = _setup(tmp_path / "lots", at, board_lots={"0700.HK": 100})
    feed.prices["0700.HK"] = (20.0, 20.01)
    assert router.submit(_buy("0700.HK"), **GATES).totalQuantity == 200


def test_spread_bps() -> None:
    assert spread_bps(Quote(symbol="X", symbolId=1, bidPrice=99.5, askPrice=100.5)) == pytest.approx(100.0)
    assert spread_bps(Quote(symbol="X", symbolId=1, lastTradePrice=100.0)) is None


def test_wake_time_tracks_next_open_and_queued_releases(tmp_path: Path) -> None:
    _clock, _feed, _paper, router = _setup(tmp_path, MON.replace(hour=20))
    assert router.seconds_until_next_wake(["7203.T"]) == pytest.approx(4 * 3600 + 5 * 60)
    assert router.seconds_until_next_wake(["7203.T", "BTC/USD"]) is None
    router.submit(_buy("7203.T"), **GATES)
    assert router.seconds_until_next_wake(["7203.T", "BTC/USD"]) == pytest.approx(4 * 3600 + 5 * 60)


# ----- fills, routing, correlation -------------------------------------------------------------

def test_touch_fill_model_crosses_the_spread(tmp_path: Path) -> None:
    feed = _Feed()
    feed.prices["AAPL"] = (100.0, 101.0)
    order = Order(symbol="AAPL", symbolId=1, action=OrderAction.BUY, orderType=OrderType.MARKET,
                  totalQuantity=1)
    mid = PaperBroker(feed=feed, slippage_bps=0.0, journal_dir=tmp_path / "m")
    touch = PaperBroker(feed=feed, slippage_bps=0.0, journal_dir=tmp_path / "t", fill_model="touch")
    mid.place_order(order.model_copy())
    touch.place_order(order.model_copy())
    assert mid.positions("PAPER-001")[0].averageEntryPrice == pytest.approx(100.5)
    assert touch.positions("PAPER-001")[0].averageEntryPrice == pytest.approx(101.0)


def test_routed_feed_dispatches_by_venue_and_keeps_order() -> None:
    stocks, crypto = _Feed(), _Feed()
    stocks.prices.update({"AAPL": (1.0, 1.1), "XIC.TO": (2.0, 2.1)})
    crypto.prices["BTC/USD"] = (3.0, 3.1)
    routed = VenueRoutedFeed({"CRYPTO": crypto}, default=stocks)
    assert [q.bidPrice for q in routed.quotes(["XIC.TO", "BTC/USD", "AAPL"])] == [2.0, 3.0, 1.0]
    assert routed.feed_for("BTC/USD") is crypto and routed.feed_for("7203.T") is stocks


def test_lead_lag_correlation_recovers_a_one_day_asynchronous_link() -> None:
    rng = np.random.default_rng(7)
    x = rng.normal(size=400)
    y = np.roll(x, 1) + rng.normal(scale=0.2, size=400)             # y reacts a session later
    frame = pd.DataFrame({"x": x[1:], "y": y[1:]})
    assert abs(frame.corr().iloc[0, 1]) < 0.2
    assert lead_lag_corr(frame, 1)[0, 1] > 0.8


def test_lead_lag_correlation_is_a_valid_correlation_matrix() -> None:
    frame = pd.DataFrame(np.random.default_rng(1).normal(size=(60, 6)))
    rho = lead_lag_corr(frame, 2)
    assert np.allclose(np.diag(rho), 1.0)
    assert np.allclose(rho, rho.T)
    assert np.linalg.eigvalsh(rho).min() > -1e-9


def test_portfolio_risk_aligns_returns_by_date_across_calendars() -> None:
    days = pd.date_range("2026-06-01", periods=80, freq="B", tz="UTC")
    rng = np.random.default_rng(3)
    base = pd.Series(rng.normal(size=80), index=days)
    tokyo = base.drop(days[10])                                      # a Tokyo-only holiday
    risks = {"US": 1000.0, "JP": 1000.0}
    aligned = portfolio_risk(risks, {"US": base, "JP": tokyo}, method="corr")
    assert aligned == pytest.approx(2000.0, rel=0.01)                # same series -> no diversification


# ----- monitor -------------------------------------------------------------------------------

class _Enter(Strategy):
    name = "enter"

    def required_history_bars(self) -> int:
        return 3

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"], out["exit"], out["atr"] = 1, 0, 0.5
        return out


class _QueueingRouter:
    queues_closed_venues = True

    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.released = 0

    def submit(self, intent, **_kw):
        self.submitted.append(intent.symbol)

    def release_due(self, **_kw):
        self.released += 1
        return []

    def seconds_until_next_wake(self, symbols):
        return 120.0


class _MonitorBroker:
    name = "fake"

    def __init__(self) -> None:
        self.quoted: list[str] = []

    def equity(self, *_a, **_k): return 100_000.0
    def positions(self, _acct): return []

    def quote(self, symbol):
        self.quoted.append(symbol)
        return Quote(symbol=symbol, symbolId=1, bidPrice=10.0, askPrice=10.01)


class _Market:
    def recent(self, symbol, bars, interval="1d"):
        n = bars + 2
        return pd.DataFrame({"close": [10.0] * n, "high": [10.1] * n, "low": [9.9] * n})


def test_monitor_evaluates_closed_venues_for_a_queueing_router() -> None:
    from trading_live_claude.risk.sizing import PositionSizer

    broker, router = _MonitorBroker(), _QueueingRouter()
    monitor = LiveMonitor(broker=broker, market=_Market(), strategy=_Enter(),
                          sizer=PositionSizer(risk_pct=0.01), router=router,  # type: ignore[arg-type]
                          account_number="A", symbols=["7203.T", "BTC/USD"], risk_model="atr",
                          heat_aggregation="sum", market_open_for=lambda s: s == "BTC/USD",
                          interval_seconds=300)
    monitor.step()
    assert router.released == 1
    assert router.submitted == ["7203.T", "BTC/USD"]
    assert broker.quoted == ["BTC/USD"]                  # closed venue priced off its last close
    assert monitor._sleep_seconds() == 120.0
