"""Session-aware routing for a 24-hour book (exchange hopping, level 4).

``SessionRouter`` wraps a :class:`Router`. An intent for a venue that is closed, or inside its
open/close auction buffer, is queued and released at the venue's next tradeable window, where
it is re-priced on a fresh quote and sent through every normal risk gate. Live-quote
microstructure controls run just before the gates: a spread ceiling for entries and board-lot
rounding. Exits are never blocked by the spread check, so the book cannot be trapped.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from ..brokers.base import Broker, StaleQuote
from ..brokers.models import Order, OrderAction, Quote
from ..logging_setup import get_logger
from ..venues import Venue, venue_for
from .router import OrderIntent, Router

log = get_logger(__name__)

QueueEvent = Literal["queued", "replaced", "released", "expired", "rejected"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MicrostructureConfig:
    open_buffer_min: float = 5.0
    close_buffer_min: float = 10.0
    intent_ttl_min: float = 30.0
    max_spread_bps_equity: float = 50.0
    max_spread_bps_crypto: float = 30.0
    board_lots: Mapping[str, int] = field(default_factory=dict)

    def lot_for(self, symbol: str, venue: Venue) -> int | None:
        return self.board_lots.get(symbol.upper(), venue.lot_size)

    def max_spread_bps(self, venue: Venue) -> float:
        return self.max_spread_bps_crypto if venue.code == "CRYPTO" else self.max_spread_bps_equity


def spread_bps(q: Quote) -> float | None:
    if not q.bidPrice or not q.askPrice or q.bidPrice <= 0 or q.askPrice <= 0:
        return None
    mid = (q.bidPrice + q.askPrice) / 2.0
    return (q.askPrice - q.bidPrice) / mid * 10_000.0


@dataclass
class QueuedIntent:
    intent: OrderIntent
    is_exit: bool
    queued_at: datetime
    release_at: datetime
    expires_at: datetime


class SessionRouter:
    queues_closed_venues = True

    def __init__(
        self,
        inner: Router,
        broker: Broker,
        *,
        account_number: str,
        config: MicrostructureConfig | None = None,
        journal_path: Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.inner = inner
        self.broker = broker
        self.account_number = account_number
        self.cfg = config or MicrostructureConfig()
        self.journal_path = journal_path
        self._clock = clock
        self.queue: dict[tuple[str, str], QueuedIntent] = {}

    # ----- journal ---------------------------------------------------------

    def _journal(self, event: QueueEvent, intent: OrderIntent, **extra: object) -> None:
        row = {"ts": self._clock().isoformat(), "event": event, "symbol": intent.symbol,
               "action": intent.action.value, "shares": intent.shares, "entry": intent.entry,
               "stop": intent.stop, "strategy": intent.strategy, **extra}
        log.info(f"scheduler.{event}", **{k: v for k, v in row.items() if k not in ("ts", "event")})
        if self.journal_path is None:
            return
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def _reject(self, intent: OrderIntent, reasons: list[str]) -> None:
        self.inner.journal.rejected({"symbol": intent.symbol, "reasons": reasons})
        self._journal("rejected", intent, reasons=reasons)

    # ----- routing ---------------------------------------------------------

    def _is_exit(self, intent: OrderIntent) -> bool:
        held = sum(p.openQuantity for p in self.broker.positions(self.account_number)
                   if p.symbol == intent.symbol)
        return (held > 0 and intent.action == OrderAction.SELL) or (
            held < 0 and intent.action == OrderAction.BUY)

    def _tradeable(self, venue: Venue, now: datetime) -> bool:
        return venue.tradeable(now, open_buffer_min=self.cfg.open_buffer_min,
                               close_buffer_min=self.cfg.close_buffer_min)

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float = 0.0,
    ) -> Order | None:
        now = self._clock()
        venue, _ = venue_for(intent.symbol)
        is_exit = self._is_exit(intent)
        if not self._tradeable(venue, now):
            self._enqueue(intent, venue, is_exit, now)
            return None
        return self._route(intent, venue, is_exit, equity=equity, existing_risk=existing_risk,
                           open_positions=open_positions,
                           current_open_notional=current_open_notional)

    def _enqueue(self, intent: OrderIntent, venue: Venue, is_exit: bool, now: datetime) -> None:
        release_at = venue.next_tradeable_start(now, open_buffer_min=self.cfg.open_buffer_min,
                                                close_buffer_min=self.cfg.close_buffer_min)
        key = (intent.symbol, intent.action.value)
        replaced = key in self.queue
        self.queue[key] = QueuedIntent(intent, is_exit, now, release_at,
                                       release_at + timedelta(minutes=self.cfg.intent_ttl_min))
        self._journal("replaced" if replaced else "queued", intent, exit=is_exit,
                      release_at=release_at.isoformat())

    def _route(self, intent: OrderIntent, venue: Venue, is_exit: bool, *, equity: float,
               existing_risk: float, open_positions: int,
               current_open_notional: float) -> Order | None:
        if not is_exit:
            try:
                q = self.broker.quote(intent.symbol)
            except StaleQuote as e:
                self._reject(intent, [f"stale quote: {'; '.join(e.reasons)}"])
                return None
            spread = spread_bps(q)
            limit = self.cfg.max_spread_bps(venue)
            if spread is None or spread > limit:
                shown = "no two-sided quote" if spread is None else f"{spread:.0f}bps"
                self._reject(intent, [f"spread {shown} above {limit:.0f}bps"])
                return None
            lot = self.cfg.lot_for(intent.symbol, venue)
            if lot == 0:
                self._reject(intent, [f"board lot unknown for {intent.symbol}; set board_lots"])
                return None
            if lot is not None and lot > 1:
                # Size-cap gates may trim, so round the post-trim size down to a whole lot.
                probe = replace(intent)
                self.inner._gate(probe, equity=equity, existing_risk=existing_risk,
                                 open_positions=open_positions,
                                 current_open_notional=current_open_notional)
                lots = int(probe.shares // lot) * lot
                if lots <= 0:
                    self._reject(intent, [f"{probe.shares} shares below one board lot of {lot}"])
                    return None
                intent.shares = lots
                intent.risk_dollars = lots * abs(intent.entry - intent.stop)
        return self.inner.submit(intent, equity=equity, existing_risk=existing_risk,
                                 open_positions=open_positions,
                                 current_open_notional=current_open_notional)

    # ----- release ---------------------------------------------------------

    def release_due(self, *, equity: float, existing_risk: float, open_positions: int,
                    current_open_notional: float = 0.0) -> list[Order]:
        now = self._clock()
        placed: list[Order] = []
        for key, item in list(self.queue.items()):
            if now < item.release_at:
                continue
            intent = item.intent
            if now >= item.expires_at:
                del self.queue[key]
                self._journal("expired", intent, reason="release window passed")
                continue
            venue, _ = venue_for(intent.symbol)
            if not self._tradeable(venue, now):
                continue
            try:
                q = self.broker.quote(intent.symbol)
            except StaleQuote:
                continue          # first prints can lag the open; retry until the window expires
            mid = q.mid
            if mid is None:
                continue
            del self.queue[key]
            if not item.is_exit:
                gapped = (intent.action == OrderAction.BUY and mid <= intent.stop) or (
                    intent.action == OrderAction.SELL and mid >= intent.stop)
                if gapped:
                    self._journal("expired", intent, reason=f"gapped through stop at {mid:.4f}")
                    continue
            shift = mid - intent.entry
            intent.stop += shift
            if intent.target is not None:
                intent.target += shift
            intent.entry = mid
            intent.risk_dollars = intent.shares * abs(intent.entry - intent.stop)
            self._journal("released", intent, exit=item.is_exit)
            order = self._route(intent, venue, item.is_exit, equity=equity,
                                existing_risk=existing_risk, open_positions=open_positions,
                                current_open_notional=current_open_notional)
            if order is not None:
                placed.append(order)
        return placed

    def seconds_until_next_wake(self, symbols: list[str]) -> float | None:
        """Seconds until the next queued release or, if no symbol can trade now, the next open.
        ``None`` when a symbol is tradeable now and nothing is queued sooner (poll normally)."""
        now = self._clock()
        wakes = [q.release_at for q in self.queue.values()]
        venues = {venue_for(s)[0] for s in symbols}
        if not any(self._tradeable(v, now) for v in venues):
            wakes += [v.next_tradeable_start(now, open_buffer_min=self.cfg.open_buffer_min,
                                             close_buffer_min=self.cfg.close_buffer_min)
                      for v in venues]
        future = [w for w in wakes if w > now]
        if not future:
            return None
        return (min(future) - now).total_seconds()
