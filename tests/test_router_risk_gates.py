"""Router risk-architecture gates — 2026-09-08.

Coverage for the four router-side items in the risk-architecture follow-up:

* Item 1 (kill-switch tighten): default dropped 0.10 → 0.03; verified in
  ``test_settings.py`` implicitly. Auto-halt wiring on PaperBroker._journal_equity is
  in ``test_kill_switch_auto_halt`` here.
* Item 2 (per-symbol notional cap): rejects an intent that would take too much of equity.
* Item 3 (intra-day forced exit): ``Router.check_forced_exits`` emits exit intents when
  unrealized loss exceeds N × ATR since entry.
* Item 5 (portfolio gross-leverage gate): rejects an intent that would push open notional
  above ``max_gross_leverage × equity``.

Item 4 (strategy-level opt-in stops) is data-blocked pending WF and has no code changes
this round.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_live_claude.brokers.models import OrderAction
from trading_live_claude.execution.router import OrderIntent, Router


@pytest.fixture
def tmp_state(tmp_path: Path) -> Path:
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    return d


@pytest.fixture
def broker() -> MagicMock:
    b = MagicMock()
    b.name = "test-broker"
    b.venue = "test"
    return b


def _mk_router(tmp_state: Path, broker: MagicMock, **overrides) -> Router:
    kwargs = dict(
        mode="paper",
        broker=broker,
        state_dir=tmp_state,
        max_gross_leverage=1.0,
        max_position_notional_pct=0.50,
        force_exit_atr_mult=3.0,
        on_size_cap_breach="reject",           # default flipped to 'trim' 2026-09-09; tests
                                                # that assert reject behavior keep old mode
    )
    kwargs.update(overrides)
    return Router.build_default(**kwargs)


def _mk_intent(symbol: str = "SPY", shares: int = 10, entry: float = 100.0,
               stop: float = 95.0, action: OrderAction = OrderAction.BUY) -> OrderIntent:
    return OrderIntent(
        symbol=symbol, action=action, shares=shares, entry=entry, stop=stop, target=None,
        strategy="test", risk_dollars=abs(entry - stop) * shares, account_number="TEST-001",
        symbolId=None,
    )


# ---- item 2: per-symbol notional cap ---------------------------------------------------

def test_per_symbol_notional_cap_rejects_above_50pct(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, max_position_notional_pct=0.50)
    # 600 sh × $100 = $60,000 = 60% of $100k equity — above the 50% cap.
    intent = _mk_intent(shares=600, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=0, current_open_notional=0.0)
    assert not decision.accepted
    assert any("single-name notional" in r for r in decision.rejected_reasons)


def test_per_symbol_notional_cap_admits_below_cap(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, max_position_notional_pct=0.50)
    # 400 sh × $100 = $40k = 40% of equity — under the 50% cap.
    intent = _mk_intent(shares=400, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=0, current_open_notional=0.0)
    assert decision.accepted


def test_per_symbol_cap_only_applies_to_entries(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, max_position_notional_pct=0.50)
    intent = _mk_intent(shares=600, entry=100.0, stop=105.0, action=OrderAction.SELL)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=0.0)
    # SELL side isn't gated by per-symbol notional cap — exits always allowed
    reasons = " ".join(decision.rejected_reasons)
    assert "single-name notional" not in reasons


# ---- item 5: portfolio gross-leverage cap ----------------------------------------------

def test_portfolio_gross_leverage_rejects_over_cap(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, max_gross_leverage=1.0,
                        max_position_notional_pct=1.0)  # widen sym cap so leverage is the binding one
    # Already $90k in positions; new intent $20k → gross 1.10x → over cap 1.0x
    intent = _mk_intent(shares=200, entry=100.0, stop=95.0)  # $20k notional
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=90_000)
    assert not decision.accepted
    assert any("gross leverage" in r for r in decision.rejected_reasons)


def test_portfolio_gross_leverage_admits_under_cap(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, max_gross_leverage=1.0,
                        max_position_notional_pct=1.0)
    # $50k open + $30k intent = $80k on $100k equity = 0.80x — under 1.0x cap
    intent = _mk_intent(shares=300, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=50_000)
    assert decision.accepted


def test_portfolio_gross_leverage_configurable(tmp_state, broker) -> None:
    # A margin-account router at 2x cap accepts what a paper 1x router rejects
    router = _mk_router(tmp_state, broker, max_gross_leverage=2.0,
                        max_position_notional_pct=1.0)
    intent = _mk_intent(shares=200, entry=100.0, stop=95.0)  # $20k
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=90_000)
    assert decision.accepted  # gross 1.10x under 2x cap


# ---- item 3: intra-day forced-exit gate ------------------------------------------------

def test_forced_exit_triggers_above_atr_mult(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, force_exit_atr_mult=3.0)
    positions = [{"symbol": "SPY", "entry_price": 100.0, "quantity": 10.0, "side": "long"}]
    # Loss of $4/share vs 3 × $1 ATR = $3/share threshold → force exit
    exits = router.check_forced_exits(
        positions, current_prices={"SPY": 96.0}, atr_at_entry={"SPY": 1.0}
    )
    assert len(exits) == 1
    assert exits[0]["symbol"] == "SPY"
    assert exits[0]["action"] == "SELL"
    assert exits[0]["shares"] == 10.0
    assert "3.0x ATR" in exits[0]["reason"]


def test_forced_exit_does_not_trigger_at_or_above_entry(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, force_exit_atr_mult=3.0)
    positions = [{"symbol": "SPY", "entry_price": 100.0, "quantity": 10.0, "side": "long"}]
    # Position at $101 = profit, no exit
    exits = router.check_forced_exits(
        positions, current_prices={"SPY": 101.0}, atr_at_entry={"SPY": 1.0}
    )
    assert exits == []


def test_forced_exit_does_not_trigger_within_atr_band(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, force_exit_atr_mult=3.0)
    positions = [{"symbol": "SPY", "entry_price": 100.0, "quantity": 10.0, "side": "long"}]
    # Loss $2/share vs 3 × $1 ATR = $3/share threshold → no exit (within noise band)
    exits = router.check_forced_exits(
        positions, current_prices={"SPY": 98.0}, atr_at_entry={"SPY": 1.0}
    )
    assert exits == []


def test_forced_exit_disabled_when_mult_zero(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, force_exit_atr_mult=0.0)
    positions = [{"symbol": "SPY", "entry_price": 100.0, "quantity": 10.0, "side": "long"}]
    # Catastrophic loss but the feature is off — no exit
    exits = router.check_forced_exits(
        positions, current_prices={"SPY": 50.0}, atr_at_entry={"SPY": 1.0}
    )
    assert exits == []


def test_forced_exit_fails_open_on_missing_data(tmp_state, broker) -> None:
    router = _mk_router(tmp_state, broker, force_exit_atr_mult=3.0)
    positions = [
        {"symbol": "SPY", "entry_price": 100.0, "quantity": 10.0, "side": "long"},
        {"symbol": "QQQ", "entry_price": 200.0, "quantity": 5.0, "side": "long"},
    ]
    # Missing SPY quote, missing QQQ ATR — both should be skipped, not crash
    exits = router.check_forced_exits(
        positions, current_prices={"QQQ": 100.0}, atr_at_entry={"SPY": 1.0}
    )
    assert exits == []


# ---- 2026-09-09: trim mode on the size caps ------------------------------------------

def test_trim_mode_per_symbol_cap_resizes_intent_and_accepts(tmp_state, broker) -> None:
    """The exact VDY.TO failure mode from 2026-09-09: 100%-of-equity intent gets trimmed
    to the 50% cap instead of rejected 39x in a row."""
    router = _mk_router(tmp_state, broker,
                        max_position_notional_pct=0.50,
                        on_size_cap_breach="trim")
    intent = _mk_intent(shares=1000, entry=100.0, stop=95.0)  # $100k intent = 100% of equity
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=0, current_open_notional=0.0)
    assert decision.accepted                                    # trim accepts, doesn't reject
    assert intent.shares == 500                                 # trimmed to fit 50% cap ($50k)


def test_trim_mode_gross_leverage_cap_trims_to_headroom(tmp_state, broker) -> None:
    """Leverage cap trim: intent above headroom is resized to the remaining slack."""
    router = _mk_router(tmp_state, broker,
                        max_gross_leverage=1.0,
                        max_position_notional_pct=1.0,        # widen sym cap so leverage binds
                        on_size_cap_breach="trim")
    # $90k already open + $20k intent → gross 1.10x; headroom = $10k, so trim to 100 sh
    intent = _mk_intent(shares=200, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=90_000)
    assert decision.accepted
    assert intent.shares == 100                                 # $10k of headroom / $100 entry


def test_trim_mode_takes_tighter_of_two_caps(tmp_state, broker) -> None:
    """Both caps binding — trim to the tighter one."""
    router = _mk_router(tmp_state, broker,
                        max_gross_leverage=1.0,
                        max_position_notional_pct=0.30,       # sym cap tighter than leverage
                        on_size_cap_breach="trim")
    intent = _mk_intent(shares=1000, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=0, current_open_notional=0.0)
    assert decision.accepted
    assert intent.shares == 300                                 # 30% of equity


def test_trim_mode_rejects_when_no_headroom_remains(tmp_state, broker) -> None:
    """When existing open notional already equals equity, no room for even a trimmed
    position — trim mode falls through to reject rather than trim-to-zero."""
    router = _mk_router(tmp_state, broker,
                        max_gross_leverage=1.0,
                        max_position_notional_pct=1.0,
                        on_size_cap_breach="trim")
    intent = _mk_intent(shares=10, entry=100.0, stop=95.0)      # $1k intent
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=100_000)
    assert not decision.accepted
    assert any("no room" in r for r in decision.rejected_reasons)


def test_trim_mode_rejects_when_trim_would_go_below_min_ticket(tmp_state, broker) -> None:
    """If the trimmed notional falls below min_ticket_usd, reject."""
    router = _mk_router(tmp_state, broker,
                        max_gross_leverage=1.0,
                        max_position_notional_pct=1.0,
                        min_ticket_usd=500.0,
                        on_size_cap_breach="trim")
    # $99.5k already open; intent $1k → headroom $500, trim to 5 sh × $100 = $500. Right at min.
    # Nudge min-ticket above so trim under-fits.
    intent = _mk_intent(shares=10, entry=100.0, stop=95.0)
    decision = router._gate(intent, equity=100_000, existing_risk=0.0,
                            open_positions=1, current_open_notional=99_600)
    assert not decision.accepted
    assert any("below min" in r for r in decision.rejected_reasons)


# ---- item 1: kill-switch auto-halt via PaperBroker ------------------------------------

def test_kill_switch_auto_halt_on_drawdown_breach(tmp_path: Path) -> None:
    """PaperBroker._journal_equity trips the KillSwitch file sentinel when a session
    drawdown exceeds settings.max_drawdown_kill_switch (default 0.03 as of 2026-09-08)."""
    from trading_live_claude.brokers.paper import PaperBroker
    from trading_live_claude.brokers.models import Quote
    from trading_live_claude.risk.kill_switch import KillSwitch
    from trading_live_claude.config import get_settings

    feed = MagicMock()
    feed.name = "test-feed"
    feed.venue = "test"
    feed.quote = MagicMock(return_value=Quote(symbol="SPY", symbolId=1, bidPrice=100.0,
                                                askPrice=100.1, lastTradePrice=100.05))

    journal_dir = tmp_path / "state"
    pb = PaperBroker(feed=feed, starting_equity=100_000.0, journal_dir=journal_dir)

    # First journal write — establishes baseline; sentinel absent
    pb._journal_equity()
    ks = KillSwitch(journal_dir)
    assert not ks.state().halted

    # Simulate a drawdown that beats whatever the current setting is by 2x, guaranteeing
    # a trip regardless of local overrides.
    _s = get_settings()
    dd_fraction = max(0.05, 2 * _s.max_drawdown_kill_switch)
    pb._peak_equity = 100_000.0
    pb._cash = 100_000.0 * (1.0 - dd_fraction)
    pb._journal_equity()

    assert ks.state().halted
    assert "max-drawdown" in ks.state().reason or "daily-loss" in ks.state().reason
