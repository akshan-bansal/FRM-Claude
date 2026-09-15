from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_live_claude.brokers.kraken import lot_rule_from_asset_pair
from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.monitor.live_loop import LiveMonitor, MonitorEvent
from trading_live_claude.risk.quantity import WHOLE_UNITS, QuantityRule
from trading_live_claude.risk.sizing import PositionSizer
from trading_live_claude.risk.sizing_policy import SizingPolicy, calendar_days, trading_days
from trading_live_claude.strategies.base import Strategy, StrategyContext

# ---- quantity rules ------------------------------------------------------------------------------


def test_whole_units_match_the_old_floor_and_stay_ints() -> None:
    assert WHOLE_UNITS.floor(7.99) == 7 and isinstance(WHOLE_UNITS.floor(7.99), int)
    assert WHOLE_UNITS.floor(0.36) == 0


def test_crypto_lot_rule_keeps_fractions_and_applies_minimums() -> None:
    paxg = QuantityRule(step=1e-8, min_qty=0.001, min_notional=0.5)
    assert paxg.floor(0.3612345678, price=4300.0) == pytest.approx(0.36123456)
    assert paxg.floor(0.0009, price=4300.0) == 0
    assert "below minimum order" in paxg.why_zero(0.0009, price=4300.0)
    tiny_value = QuantityRule(step=1e-8, min_qty=0.0, min_notional=0.5)
    assert tiny_value.floor(0.001, price=100.0) == 0
    assert "value 0.10 below minimum" in tiny_value.why_zero(0.001, price=100.0)


def test_kraken_asset_pair_row_parses_to_a_rule() -> None:
    rule = lot_rule_from_asset_pair({"lot_decimals": 5, "ordermin": "50", "costmin": "0.5"})
    assert rule == QuantityRule(step=1e-5, min_qty=50.0, min_notional=0.5)


def test_sizer_reports_raw_and_rounded_quantity() -> None:
    sized = PositionSizer().size(equity=100_000, entry=4300.0, atr_value=40.0, annual_vol=0.15, conviction=0.022,
                                 qty_rule=QuantityRule(step=1e-8, min_qty=0.001))
    assert sized.raw_shares == pytest.approx(100_000 * 0.022 / 4300.0)
    assert sized.shares == pytest.approx(0.51162790, abs=1e-8)
    legacy = PositionSizer().size(equity=100_000, entry=4300.0, atr_value=40.0, annual_vol=0.15, conviction=0.022)
    assert legacy.shares == 0       # the bug: whole units swallow a $2,200 position


def test_annualization_is_explicit_per_venue() -> None:
    assert SizingPolicy().periods_per_year_for("XIC.TO") == 252.0      # default: exchange sessions
    assert calendar_days("BTC/USD") == 365.0 and trading_days("/MCL") == 252.0


# ---- live loop -----------------------------------------------------------------------------------


@dataclass
class _Quote:
    mid: float
    lastTradePrice: float

    @property
    def askPrice(self) -> float:
        return self.mid

    @property
    def bidPrice(self) -> float:
        return self.mid


@dataclass
class _Position:
    symbol: str
    openQuantity: float
    averageEntryPrice: float = 0.0


class _Broker:
    def __init__(self, price: float, positions: list[_Position] | None = None) -> None:
        self.price = price
        self._positions = positions or []

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        return 100_000.0

    def positions(self, account_number: str) -> list[_Position]:
        return self._positions

    def quote(self, symbol: str) -> _Quote:
        return _Quote(self.price, self.price)


class _Market:
    def recent(self, symbol: str, bars: int, interval: str = "1d") -> pd.DataFrame:
        rng = np.random.default_rng(7)
        close = 4300.0 * np.cumprod(1 + rng.normal(0, 0.01, bars + 30))
        return pd.DataFrame({"close": close, "high": close * 1.01, "low": close * 0.99})


class _Router:
    def __init__(self) -> None:
        self.intents: list = []

    def submit(self, intent, **kw) -> None:
        self.intents.append(intent)


class _Signal(Strategy):
    name = "sig"

    def __init__(self, entry: int, exit_: int, strength: float = 1.0) -> None:
        super().__init__()
        self.entry, self.exit_, self.strength = entry, exit_, strength

    def required_history_bars(self) -> int:
        return 25

    def generate_signals(self, df: pd.DataFrame, ctx: StrategyContext) -> pd.DataFrame:
        out = df.copy()
        out["entry"], out["exit"], out["atr"], out["signal_strength"] = self.entry, self.exit_, 40.0, self.strength
        return out


def _monitor(broker: _Broker, strat: Strategy, router: _Router, events: list[MonitorEvent],
             policy: SizingPolicy | None) -> LiveMonitor:
    return LiveMonitor(broker=broker, market=_Market(), strategy=strat, sizer=PositionSizer(),  # type: ignore[arg-type]
                       router=router, account_number="X", symbols=["PAXG/USD"],  # type: ignore[arg-type]
                       on_event=events.append, emit_on_change_only=False, risk_model="atr",
                       heat_aggregation="sum", sizing_policy=policy)


def test_exit_closes_the_exact_fractional_position_without_a_policy() -> None:
    router, events = _Router(), []
    broker = _Broker(4300.0, [_Position("PAXG/USD", 0.36, 4200.0)])
    _monitor(broker, _Signal(0, 1), router, events, None).step()
    assert router.intents[0].action == OrderAction.SELL
    assert router.intents[0].shares == pytest.approx(0.36)


def test_policy_routes_a_fractional_entry_and_journals_the_decision(tmp_path: Path) -> None:
    router, events = _Router(), []
    journal = tmp_path / "sizing.jsonl"
    policy = SizingPolicy(quantity_rule_for=lambda _s: QuantityRule(step=1e-8, min_qty=0.001, min_notional=0.5),
                          periods_per_year_for=calendar_days, journal_path=journal, session_id="s1")
    _monitor(_Broker(4300.0), _Signal(1, 0, strength=0.05), router, events, policy).step()
    shares = router.intents[0].shares
    assert shares > 0 and shares != int(shares)      # a fractional lot, not floored to whole coins
    row = json.loads(journal.read_text().splitlines()[0])
    assert row["periods"] == 365.0 and row["routed"] is True and row["session_id"] == "s1"
    assert row["qty"] == pytest.approx(router.intents[0].shares)


def test_policy_journals_why_a_size_rounded_to_zero(tmp_path: Path) -> None:
    router, events = _Router(), []
    journal = tmp_path / "sizing.jsonl"
    policy = SizingPolicy(quantity_rule_for=lambda _s: QuantityRule(step=1e-8, min_qty=5.0), journal_path=journal)
    _monitor(_Broker(4300.0), _Signal(1, 0, strength=0.05), router, events, policy).step()
    assert not router.intents
    assert "below minimum order" in json.loads(journal.read_text().splitlines()[0])["reason"]


def test_stop_is_enforced_from_the_entry_stop_and_rebuilt_after_restart() -> None:
    router, events = _Router(), []
    # Restart case: no recorded stop, so it is rebuilt from avg entry 4300 - 2 x ATR 40 = 4220.
    broker = _Broker(4210.0, [_Position("PAXG/USD", 0.5, 4300.0)])
    policy = SizingPolicy()
    _monitor(broker, _Signal(0, 0), router, events, policy).step()
    assert router.intents and router.intents[0].action == OrderAction.SELL
    assert events[-1].detail["reason"] == "stop" and events[-1].detail["stop"] == pytest.approx(4220.0)


def test_price_above_the_stop_holds_and_no_policy_never_stops() -> None:
    held = [_Position("PAXG/USD", 0.5, 4300.0)]
    router, events = _Router(), []
    _monitor(_Broker(4250.0, held), _Signal(0, 0), router, events, SizingPolicy()).step()
    assert not router.intents and events[-1].kind == "hold"
    router2: _Router = _Router()
    _monitor(_Broker(4000.0, held), _Signal(0, 0), router2, [], None).step()
    assert not router2.intents


def test_router_trim_rounds_to_the_symbol_lot_not_whole_units(tmp_path: Path) -> None:
    from trading_live_claude.brokers.paper import PaperBroker
    from trading_live_claude.execution.router import OrderIntent, Router

    broker = PaperBroker(feed=_Broker(60_000.0), starting_equity=100_000.0, journal_dir=tmp_path)  # type: ignore[arg-type]
    router = Router.build_default(mode="paper", broker=broker, state_dir=tmp_path, cap_pct=0.5,
                                  max_drawdown_pct=0.2, daily_loss_limit_pct=0.05, max_open_positions=5,
                                  min_ticket_usd=10.0)
    router.max_position_notional_pct = 0.10          # $10k cap on a $30k BTC intent -> trim
    router.quantity_rule_for = lambda _s: QuantityRule(step=1e-8, min_qty=5e-5)
    intent = OrderIntent(symbol="BTC/USD", action=OrderAction.BUY, shares=0.5, entry=60_000.0, stop=58_000.0,
                         target=None, strategy="t", risk_dollars=1_000.0, account_number=broker.accounts()[0].number)
    router.submit(intent, equity=100_000.0, existing_risk=0.0, open_positions=0)
    assert 0 < intent.shares <= 10_000.0 / 60_000.0 + 1e-8
