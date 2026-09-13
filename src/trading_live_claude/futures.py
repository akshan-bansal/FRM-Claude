"""Futures (``/ROOT``) for the paper book: IB contract specs, liquid-hours sessions and rolls.

Prices are rescaled at the feed by ``multiplier / price_magnifier`` so one unit is one contract at
full notional. Rolls happen ``roll_bdays`` business days before the earlier of last trade and the
contract month start, ahead of first notice. Exchange holidays are not modelled.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .brokers.base import Broker, StaleQuote
from .brokers.models import OrderAction
from .execution.router import OrderIntent
from .logging_setup import get_logger
from .venues import DatedVenue, register_futures_venue

log = get_logger(__name__)

# IB timeZoneId values that are abbreviations or legacy names ZoneInfo would misread.
_TZ_ALIASES = {
    "EST": "America/New_York", "EDT": "America/New_York", "US/Eastern": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago", "US/Central": "America/Chicago",
    "GMT": "Europe/London", "BST": "Europe/London", "GB": "Europe/London", "GB-Eire": "Europe/London",
    "MET": "Europe/Berlin", "CET": "Europe/Berlin", "JST": "Asia/Tokyo", "Japan": "Asia/Tokyo",
    "SGT": "Asia/Singapore", "Singapore": "Asia/Singapore", "HKT": "Asia/Hong_Kong",
    "Hongkong": "Asia/Hong_Kong",
}


def _zone(tz_id: str) -> ZoneInfo:
    return ZoneInfo(_TZ_ALIASES.get(tz_id, tz_id))


def parse_ib_hours(hours: str, tz_id: str) -> tuple[tuple[datetime, datetime], ...]:
    """IB ``liquidHours``/``tradingHours`` -> sorted UTC windows. Handles both
    ``20260914:1700-20260915:1600`` and the older ``20260914:0930-1600,1700-1800`` forms."""
    zone = _zone(tz_id)
    out: list[tuple[datetime, datetime]] = []
    for part in filter(None, (p.strip() for p in hours.split(";"))):
        day, _, spans = part.partition(":")
        if spans.upper() == "CLOSED" or not spans:
            continue
        for span in spans.split(","):
            m = re.fullmatch(r"(\d{4})-(?:(\d{8}):)?(\d{4})", span.strip())
            if not m:
                continue
            start_hm, end_day, end_hm = m.groups()
            start = datetime.strptime(day + start_hm, "%Y%m%d%H%M").replace(tzinfo=zone)
            end = datetime.strptime((end_day or day) + end_hm, "%Y%m%d%H%M").replace(tzinfo=zone)
            if end > start:
                out.append((start.astimezone(UTC), end.astimezone(UTC)))
    return tuple(sorted(out))


def _minus_bdays(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


@dataclass(frozen=True)
class FuturesContract:
    con_id: int
    local_symbol: str
    last_trade: date
    contract_month: date

    def roll_date(self, roll_bdays: int) -> date:
        return min(_minus_bdays(self.last_trade, roll_bdays),
                   _minus_bdays(self.contract_month, roll_bdays))


@dataclass(frozen=True)
class FuturesSpec:
    symbol: str                      # "/ROOT"
    root: str
    exchange: str
    currency: str
    multiplier: float
    price_magnifier: float
    tz_id: str
    contracts: tuple[FuturesContract, ...]
    liquid_windows: tuple[tuple[datetime, datetime], ...]
    long_name: str = ""

    @property
    def scale(self) -> float:
        return self.multiplier / (self.price_magnifier or 1.0)

    def active(self, today: date, roll_bdays: int) -> FuturesContract | None:
        for c in self.contracts:
            if c.roll_date(roll_bdays) > today:
                return c
        return None


def _parse_date(s: str) -> date | None:
    s = (s or "").strip()
    for fmt, n in (("%Y%m%d", 8), ("%Y%m", 6)):
        if len(s) >= n:
            try:
                return datetime.strptime(s[:n], fmt).date()
            except ValueError:
                continue
    return None


def spec_from_ib_details(details: Iterable[object], symbol: str | None = None) -> FuturesSpec | None:
    """Build a spec from ib_insync ``ContractDetails`` for one root on one exchange."""
    rows = list(details)
    if not rows:
        return None
    contracts: list[FuturesContract] = []
    for d in rows:
        c = d.contract  # type: ignore[attr-defined]
        last = _parse_date(getattr(d, "realExpirationDate", "") or c.lastTradeDateOrContractMonth)
        month = _parse_date(getattr(d, "contractMonth", "") or c.lastTradeDateOrContractMonth)
        if last is None or month is None:
            continue
        contracts.append(FuturesContract(int(c.conId), str(c.localSymbol), last,
                                         month.replace(day=1)))
    if not contracts:
        return None
    first = rows[0]
    c0 = first.contract  # type: ignore[attr-defined]
    tz_id = str(getattr(first, "timeZoneId", "") or "UTC")
    return FuturesSpec(
        symbol=(symbol or f"/{c0.symbol}").upper(),
        root=str(c0.symbol),
        exchange=str(c0.exchange),
        currency=str(c0.currency),
        multiplier=float(c0.multiplier or 1.0),
        price_magnifier=float(getattr(first, "priceMagnifier", 1) or 1),
        tz_id=tz_id,
        contracts=tuple(sorted(contracts, key=lambda k: k.last_trade)),
        liquid_windows=parse_ib_hours(str(getattr(first, "liquidHours", "") or ""), tz_id),
        long_name=str(getattr(first, "longName", "") or ""),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FuturesBook:
    """The futures in play: specs, the contract each symbol currently quotes, and roll state."""

    roll_bdays: int = 5
    clock: Callable[[], datetime] = _utc_now
    specs: dict[str, FuturesSpec] = field(default_factory=dict)
    current: dict[str, FuturesContract] = field(default_factory=dict)

    def add(self, spec: FuturesSpec) -> None:
        self.specs[spec.symbol] = spec
        register_futures_venue(spec.symbol, DatedVenue(
            code="FUT", ib_exchange=spec.exchange, currency=spec.currency, tz="UTC",
            lot_size=None, windows=spec.liquid_windows))
        if spec.symbol not in self.current:
            active = spec.active(self.clock().date(), self.roll_bdays)
            if active is not None:
                self.current[spec.symbol] = active

    def contract_for(self, symbol: str) -> tuple[int, str, str, str] | None:
        spec = self.specs.get(symbol.upper())
        con = self.current.get(symbol.upper())
        if spec is None or con is None:
            return None
        return con.con_id, spec.root, spec.exchange, spec.currency

    def multiplier_for(self, symbol: str) -> float:
        spec = self.specs.get(symbol.upper())
        return spec.scale if spec is not None else 1.0

    def pending_rolls(self) -> dict[str, FuturesContract]:
        today = self.clock().date()
        out: dict[str, FuturesContract] = {}
        for sym, spec in self.specs.items():
            target = spec.active(today, self.roll_bdays)
            if target is not None and self.current.get(sym) != target:
                out[sym] = target
        return out


class RollRouterProtocol(Protocol):
    def submit(self, intent: OrderIntent, *, equity: float, existing_risk: float,
               open_positions: int) -> object: ...


def make_roller(
    book: FuturesBook,
    broker: Broker,
    router: RollRouterProtocol,
    *,
    account_number: str,
    is_tradeable: Callable[[str], bool],
) -> Callable[..., None]:
    """Monitor hook: roll held futures through the router — exit on the old contract, switch, re-enter
    on the new one, so the calendar spread is never booked as P&L. Waits while the venue is closed."""
    def roll(*, equity: float, existing_risk: float, open_positions: int) -> None:
        pending = book.pending_rolls()
        if not pending:
            return
        held: Mapping[str, float] = {p.symbol.upper(): p.openQuantity
                                     for p in broker.positions(account_number) if p.openQuantity}
        for sym, target in pending.items():
            qty = held.get(sym, 0.0)
            old = book.current.get(sym)
            if not qty:
                book.current[sym] = target
                log.info("futures.roll.switch", symbol=sym, to=target.local_symbol)
                continue
            if not is_tradeable(sym):
                continue
            try:
                exit_px = broker.quote(sym).mid
            except StaleQuote:
                continue
            if exit_px is None:
                continue
            closing = OrderAction.SELL if qty > 0 else OrderAction.BUY
            size = int(abs(qty))
            exit_stop = exit_px * (1.02 if closing == OrderAction.SELL else 0.98)
            placed = router.submit(OrderIntent(
                symbol=sym, action=closing, shares=size, entry=exit_px, stop=exit_stop,
                target=None, strategy="futures_roll", risk_dollars=size * abs(exit_px - exit_stop),
                account_number=account_number, symbolId=old.con_id if old else None),
                equity=equity, existing_risk=existing_risk, open_positions=open_positions)
            if placed is None:
                continue
            book.current[sym] = target
            log.info("futures.roll.closed", symbol=sym, frm=old.local_symbol if old else None,
                     to=target.local_symbol, qty=qty)
            try:
                entry_px = broker.quote(sym).mid
            except StaleQuote:
                entry_px = None
            if entry_px is None:
                log.warning("futures.roll.reentry_skipped", symbol=sym, reason="no quote")
                continue
            reopen = OrderAction.BUY if qty > 0 else OrderAction.SELL
            stop = entry_px * (0.98 if reopen == OrderAction.BUY else 1.02)
            router.submit(OrderIntent(
                symbol=sym, action=reopen, shares=size, entry=entry_px, stop=stop, target=None,
                strategy="futures_roll", risk_dollars=size * abs(entry_px - stop),
                account_number=account_number, symbolId=target.con_id),
                equity=equity, existing_risk=existing_risk, open_positions=max(open_positions - 1, 0))
    return roll
