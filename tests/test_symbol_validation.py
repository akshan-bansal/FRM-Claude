"""Pre-flight symbol validation — unit tests.

Covers the four outcome shapes: ok / warn / fail / mixed-batch, plus the
refuse-launch gate's SystemExit behavior.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from trading_live_claude.analysis.symbol_validation import (
    SymbolValidation,
    format_validation_banner,
    refuse_launch_on_hard_failures,
    validate_sleeve,
)
from trading_live_claude.brokers.base import BrokerError
from trading_live_claude.brokers.models import Candle


def _mk_candle(day: int) -> Candle:
    ts = datetime(2026, 9, day, tzinfo=UTC)
    return Candle(start=ts, end=ts, open=100.0, high=101.0, low=99.5,
                    close=100.5, volume=1000)


def test_ok_symbol_returns_ok_status_with_bar_count() -> None:
    broker = MagicMock()
    broker.candles = MagicMock(return_value=[_mk_candle(5), _mk_candle(6), _mk_candle(7)])
    results = validate_sleeve(broker, ["SPY"])
    assert len(results) == 1
    assert results[0].symbol == "SPY"
    assert results[0].status == "ok"
    assert results[0].bars_fetched == 3
    assert "3 bar" in results[0].reason


def test_empty_candle_list_returns_warn() -> None:
    """Broker returned no exception but zero bars — symbol may be delisted or the
    lookback window fell inside a market-closed window."""
    broker = MagicMock()
    broker.candles = MagicMock(return_value=[])
    results = validate_sleeve(broker, ["MAYBE.DELISTED"])
    assert results[0].status == "warn"
    assert results[0].bars_fetched == 0
    assert "empty" in results[0].reason


def test_broker_exception_returns_fail_with_type_preserved() -> None:
    """The ARX.TO / RIG.TO failure mode: candles endpoint 404s. Fail status carries
    the exception type name so the operator can distinguish 404 from auth from rate
    limit at a glance."""
    broker = MagicMock()
    broker.candles = MagicMock(side_effect=BrokerError(
        "GET markets/candles/6291 -> 404: Symbol not found"
    ))
    results = validate_sleeve(broker, ["ARX.TO"])
    assert results[0].status == "fail"
    assert "BrokerError" in results[0].reason
    assert "404" in results[0].reason
    assert results[0].bars_fetched == 0


def test_mixed_batch_classifies_each_symbol_independently() -> None:
    """One broker.candles call per symbol; failure on one doesn't stop the walk."""
    broker = MagicMock()
    def _side(sym: str, *args, **kwargs) -> list[Candle]:
        if sym == "GOOD":
            return [_mk_candle(5)]
        if sym == "EMPTY":
            return []
        raise BrokerError(f"404 on {sym}")
    broker.candles = MagicMock(side_effect=_side)
    results = validate_sleeve(broker, ["GOOD", "EMPTY", "BAD"])
    statuses = {r.symbol: r.status for r in results}
    assert statuses == {"GOOD": "ok", "EMPTY": "warn", "BAD": "fail"}


def test_refuse_launch_raises_on_any_fail() -> None:
    """Even one 'fail' in the batch triggers SystemExit unless allow_fail is set."""
    results = [
        SymbolValidation(symbol="GOOD", status="ok", reason="", bars_fetched=1),
        SymbolValidation(symbol="BAD", status="fail", reason="404", bars_fetched=0),
    ]
    with pytest.raises(SystemExit) as ex:
        refuse_launch_on_hard_failures(results)
    assert "BAD" in str(ex.value)
    assert "REFUSING to start" in str(ex.value)


def test_refuse_launch_admits_when_only_warn_present() -> None:
    """Warn is not a hard failure — the sleeve can still start with a warn'd name."""
    results = [
        SymbolValidation(symbol="GOOD", status="ok", reason="", bars_fetched=1),
        SymbolValidation(symbol="MAYBE", status="warn", reason="empty", bars_fetched=0),
    ]
    # Should not raise
    refuse_launch_on_hard_failures(results)


def test_refuse_launch_opt_out_bypasses_gate() -> None:
    """allow_fail=True lets a caller run through failures deliberately (diagnostic)."""
    results = [
        SymbolValidation(symbol="BAD", status="fail", reason="404", bars_fetched=0),
    ]
    refuse_launch_on_hard_failures(results, allow_fail=True)


def test_format_banner_summary_lines() -> None:
    results = [
        SymbolValidation(symbol="GOOD", status="ok", reason="1 bar returned", bars_fetched=1),
        SymbolValidation(symbol="EMPTY", status="warn", reason="empty", bars_fetched=0),
        SymbolValidation(symbol="BAD", status="fail", reason="BrokerError: 404", bars_fetched=0),
    ]
    banner = format_validation_banner(results)
    assert "3 symbols checked" in banner
    assert "1 ok, 1 warn, 1 fail" in banner
    assert "FAIL" in banner and "BAD" in banner
    assert "WARN" in banner and "EMPTY" in banner
