"""Listing venues: exchange routing, currency and regular trading hours per symbol suffix.

Single source of truth for both IB adapters and the session-hours guard. Exchange holidays
are not modelled; on a holiday the feed stops moving and the stale-quote guard catches it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Venue:
    code: str
    ib_exchange: str
    currency: str
    tz: str
    sessions: tuple[tuple[time, time], ...] = ()
    weekdays: frozenset[int] = frozenset(range(5))    # Monday = 0
    always_open: bool = False
    # Shares per board lot. None = any quantity; 0 = varies per stock and must be configured.
    lot_size: int | None = 1

    def is_open(self, ts: datetime | None = None) -> bool:
        return self.tradeable(ts)

    def _windows(self, ts: datetime, open_buffer_min: float, close_buffer_min: float,
                 ) -> list[tuple[datetime, datetime]]:
        """Tradeable (start, end) windows in UTC from ``ts``'s local day through the next 8 days."""
        zone = _zone(self.tz)
        day = ts.astimezone(zone).date()
        out: list[tuple[datetime, datetime]] = []
        for offset in range(9):
            d = day + timedelta(days=offset)
            if d.weekday() not in self.weekdays:
                continue
            for open_, close in self.sessions:
                start = datetime.combine(d, open_, zone) + timedelta(minutes=open_buffer_min)
                end = datetime.combine(d, close, zone) - timedelta(minutes=close_buffer_min)
                if end > start:
                    out.append((start.astimezone(UTC), end.astimezone(UTC)))
        return out

    def tradeable(self, ts: datetime | None = None, *, open_buffer_min: float = 0.0,
                  close_buffer_min: float = 0.0) -> bool:
        """Inside a session, excluding ``open_buffer_min`` after each open and ``close_buffer_min``
        before each close (auction and first-print volatility)."""
        if self.always_open:
            return True
        now = ts or datetime.now(UTC)
        return any(start <= now < end
                   for start, end in self._windows(now - timedelta(days=1), open_buffer_min,
                                                   close_buffer_min))

    def next_tradeable_start(self, ts: datetime | None = None, *, open_buffer_min: float = 0.0,
                             close_buffer_min: float = 0.0) -> datetime:
        """``ts`` itself when tradeable now, else the start of the next tradeable window."""
        now = ts or datetime.now(UTC)
        if self.always_open:
            return now
        for start, end in sorted(self._windows(now - timedelta(days=1), open_buffer_min,
                                               close_buffer_min)):
            if now < end:
                return max(start, now)
        raise ValueError(f"{self.code}: no known upcoming session")


@cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _sessions(*spans: str) -> tuple[tuple[time, time], ...]:
    return tuple((_hm(a), _hm(b)) for a, b in (span.split("-") for span in spans))


VENUES: dict[str, Venue] = {
    "US":     Venue("US", "SMART", "USD", "America/New_York", _sessions("09:30-16:00")),
    "TSX":    Venue("TSX", "TSE", "CAD", "America/Toronto", _sessions("09:30-16:00")),
    "TSXV":   Venue("TSXV", "VENTURE", "CAD", "America/Toronto", _sessions("09:30-16:00")),
    "LSE":    Venue("LSE", "LSE", "GBP", "Europe/London", _sessions("08:00-16:30")),
    "ASX":    Venue("ASX", "ASX", "AUD", "Australia/Sydney", _sessions("10:00-16:00")),
    # Tokyo extended its afternoon session to 15:30 on 2024-11-05; domestic stocks trade in 100s.
    "TSEJ":   Venue("TSEJ", "TSEJ", "JPY", "Asia/Tokyo", _sessions("09:00-11:30", "12:30-15:30"),
                    lot_size=100),
    # Hong Kong board lots are set per stock (e.g. 100, 400, 500) — configure board_lots.
    "SEHK":   Venue("SEHK", "SEHK", "HKD", "Asia/Hong_Kong", _sessions("09:30-12:00", "13:00-16:00"),
                    lot_size=0),
    "CRYPTO": Venue("CRYPTO", "", "USD", "UTC", always_open=True, lot_size=None),
}

# Checked in order; ".TO" must precede ".T".
_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".TO", "TSX"), (".V", "TSXV"), (".L", "LSE"), (".AX", "ASX"), (".T", "TSEJ"), (".HK", "SEHK"),
)


@dataclass(frozen=True)
class DatedVenue(Venue):
    """A venue whose sessions are explicit UTC windows (e.g. a futures contract's IB liquidHours)."""

    windows: tuple[tuple[datetime, datetime], ...] = ()

    def _windows(self, ts: datetime, open_buffer_min: float, close_buffer_min: float,
                 ) -> list[tuple[datetime, datetime]]:
        ob, cb = timedelta(minutes=open_buffer_min), timedelta(minutes=close_buffer_min)
        return [(s + ob, e - cb) for s, e in self.windows if e - cb > s + ob]


_FUTURES: dict[str, Venue] = {}
# An unregistered /ROOT has no sessions, so it can never trade: fail closed rather than guess.
_UNREGISTERED_FUTURE = Venue("FUT", "", "USD", "UTC", lot_size=None)


def register_futures_venue(symbol: str, venue: Venue) -> None:
    _FUTURES[symbol.upper()] = venue


def venue_for(symbol: str) -> tuple[Venue, str]:
    """``(venue, ib_symbol)``. ``/ROOT`` = registered future; ``BASE/QUOTE`` = 24/7 crypto;
    unsuffixed tickers = US stocks."""
    if symbol.startswith("/"):
        return _FUTURES.get(symbol.upper(), _UNREGISTERED_FUTURE), symbol[1:]
    if "/" in symbol:
        return VENUES["CRYPTO"], symbol
    up = symbol.upper()
    for suffix, code in _SUFFIXES:
        if up.endswith(suffix):
            bare = symbol[: -len(suffix)]
            if code == "SEHK":
                bare = bare.lstrip("0") or "0"    # IB lists 0700.HK as "700"
            return VENUES[code], bare
    return VENUES["US"], symbol


def currency_of(symbol: str) -> str:
    if "/" in symbol and not symbol.startswith("/"):
        return symbol.split("/", 1)[1].upper()
    return venue_for(symbol)[0].currency


def market_open(symbol: str, ts: datetime | None = None) -> bool:
    return venue_for(symbol)[0].is_open(ts)
