from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trading_live_claude.venues import currency_of, market_open, venue_for


@pytest.mark.parametrize(("symbol", "code", "ib_symbol", "ccy"), [
    ("AAPL", "US", "AAPL", "USD"),
    ("BRK.B", "US", "BRK.B", "USD"),
    ("XIC.TO", "TSX", "XIC", "CAD"),
    ("SRU.UN.TO", "TSX", "SRU.UN", "CAD"),
    ("XYZ.V", "TSXV", "XYZ", "CAD"),
    ("VOD.L", "LSE", "VOD", "GBP"),
    ("BHP.AX", "ASX", "BHP", "AUD"),
    ("7203.T", "TSEJ", "7203", "JPY"),
    ("0700.HK", "SEHK", "700", "HKD"),
    ("0005.hk", "SEHK", "5", "HKD"),
    ("BTC/USD", "CRYPTO", "BTC/USD", "USD"),
])
def test_suffix_routing(symbol: str, code: str, ib_symbol: str, ccy: str) -> None:
    venue, bare = venue_for(symbol)
    assert (venue.code, bare, venue.currency) == (code, ib_symbol, ccy)
    assert currency_of(symbol) == ccy


def test_crypto_quote_currency_comes_from_the_pair() -> None:
    assert currency_of("ETH/EUR") == "EUR"


def _utc(mo: int, d: int, h: int, mi: int = 0) -> datetime:
    return datetime(2026, mo, d, h, mi, tzinfo=UTC)


@pytest.mark.parametrize(("symbol", "ts", "expected"), [
    # Mon 2026-09-14. EDT = UTC-4 -> NYSE/TSX open 13:30-20:00 UTC.
    ("AAPL", _utc(9, 14, 13, 29), False),
    ("AAPL", _utc(9, 14, 13, 30), True),
    ("XIC.TO", _utc(9, 14, 19, 59), True),
    ("XIC.TO", _utc(9, 14, 20, 0), False),
    # After US DST ends (Mon 2026-11-09, EST = UTC-5) the open moves to 14:30 UTC.
    ("AAPL", _utc(11, 9, 13, 30), False),
    ("AAPL", _utc(11, 9, 14, 30), True),
    # London BST = UTC+1 -> 07:00-15:30 UTC.
    ("VOD.L", _utc(9, 14, 7, 0), True),
    ("VOD.L", _utc(9, 14, 15, 30), False),
    # Sydney AEST = UTC+10 -> 00:00-06:00 UTC.
    ("BHP.AX", _utc(9, 14, 0, 30), True),
    ("BHP.AX", _utc(9, 14, 6, 30), False),
    # Tokyo JST = UTC+9: 00:00-02:30 and 03:30-06:30 UTC, lunch closed.
    ("7203.T", _utc(9, 14, 1, 0), True),
    ("7203.T", _utc(9, 14, 3, 0), False),
    ("7203.T", _utc(9, 14, 6, 0), True),
    ("7203.T", _utc(9, 14, 6, 30), False),
    # Hong Kong HKT = UTC+8: 01:30-04:00 and 05:00-08:00 UTC.
    ("0700.HK", _utc(9, 14, 4, 30), False),
    ("0700.HK", _utc(9, 14, 7, 59), True),
    # Weekends: Saturday in every venue timezone.
    ("AAPL", _utc(9, 12, 15, 0), False),
    ("7203.T", _utc(9, 12, 1, 0), False),
    ("BTC/USD", _utc(9, 12, 3, 0), True),
])
def test_regular_trading_hours(symbol: str, ts: datetime, expected: bool) -> None:
    assert market_open(symbol, ts) is expected
