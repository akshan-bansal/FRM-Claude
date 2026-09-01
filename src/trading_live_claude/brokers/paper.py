"""In-memory paper broker. Wraps a real broker for *quotes/candles* but routes
every order through an in-memory book that simulates fills at the current quote
mid + a configurable slippage.

Use a real Questrade broker as the ``feed`` so paper mode exactly mirrors live
data; the paper broker only short-circuits order placement.

Journals (all under ``journal_dir``, one row per event):

* ``paper_fills.jsonl`` — executed fills (compat with the existing writer).
* ``paper_orders.jsonl`` — every intent that reached this broker, accepted or rejected,
  so the intent→fill funnel is reconstructable.
* ``paper_equity.csv`` — equity, cash, positions_value, realized/unrealized P&L,
  peak_equity, and drawdown_pct on every fill. Feeds the go-live pre-check.

Every row carries a per-instance ``session_id`` (uuid4 hex) so overlapping paper
runs stay separable in the record — the go-live drawdown / trade-count asserts
against one session, not an accidental average of two.
"""
from __future__ import annotations

import csv
import itertools
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from ..logging_setup import get_logger
from .base import Broker, OrderRejected
from .models import Account, Candle, Fill, Order, OrderAction, Position, Quote

log = get_logger(__name__)


_EQUITY_COLUMNS = (
    "ts", "session_id", "equity", "cash", "positions_value",
    "realized_pnl", "unrealized_pnl", "peak_equity", "drawdown_pct",
)


class PaperBroker(Broker):
    name = "paper"

    _order_counter: Iterator[int] = itertools.count(1)

    def __init__(
        self,
        feed: Broker,
        starting_equity: float = 100_000.0,
        slippage_bps: float = 5.0,
        commission_per_trade: float = 4.95,
        journal_dir: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self._feed = feed
        self._starting_equity = starting_equity
        self._equity = starting_equity
        self._cash = starting_equity
        self._slippage_bps = slippage_bps
        self._commission = commission_per_trade
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._journal_dir = journal_dir
        # Per-instance session id — kept short (uuid4 hex is 32 chars). Two paper runs against the
        # same journal_dir must never commingle in accounting queries, and this is the primary key
        # for that.
        self.session_id = session_id or uuid.uuid4().hex
        # Realized P&L is accrued on closing fills, tracked here so the equity CSV can carry it
        # without recomputing from the fills journal.
        self._realized_pnl = 0.0
        # Peak equity tracked for the drawdown series feeding the max-drawdown kill-switch invariant.
        self._peak_equity = starting_equity

    # ----- read-only data passes through feed -----------------------------

    def accounts(self) -> list[Account]:
        return [Account(type="Paper", number="PAPER-001", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        return list(self._positions.values())

    def quote(self, symbol: str) -> Quote:
        return self._feed.quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return self._feed.quotes(symbols)

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list[Candle]:
        return self._feed.candles(symbol, start, end, interval)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        """Paper broker is single-currency; `currency` arg accepted for protocol parity."""
        mtm = sum(p.openQuantity * p.currentPrice for p in self._positions.values())
        return self._cash + mtm

    # ----- order placement simulator --------------------------------------

    def place_order(self, order: Order) -> Order:
        # Journal the intent BEFORE trying to fill so a reject is on the record with the same
        # order id as the (eventual) attempt. The alternative — only journaling successes — makes
        # a poll where the strategy fired but the broker declined invisible.
        order.id = next(self._order_counter)
        quote = self._feed.quote(order.symbol)
        ref_price = quote.mid or quote.lastTradePrice or order.limitPrice
        if ref_price is None or ref_price <= 0:
            self._journal_order(order, ref_price=None, accepted=False,
                                rejected_reasons=["no_reference_price"])
            raise OrderRejected(f"No reference price for {order.symbol}; cannot simulate fill")

        slippage = ref_price * (self._slippage_bps / 10_000.0)
        fill_price = ref_price + slippage if order.action == OrderAction.BUY else ref_price - slippage

        signed_qty = order.totalQuantity if order.action == OrderAction.BUY else -order.totalQuantity
        notional = abs(signed_qty) * fill_price
        self._cash -= signed_qty * fill_price + self._commission

        pos = self._positions.get(order.symbol)
        if pos is None:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                symbolId=order.symbolId or 0,
                openQuantity=signed_qty,
                averageEntryPrice=fill_price,
                currentPrice=fill_price,
                totalCost=notional,
            )
        else:
            new_qty = pos.openQuantity + signed_qty
            if new_qty == 0:
                # Closing fill: realize P&L against the average entry price. Sign convention:
                # if we're closing a long (pos.openQuantity > 0, signed_qty < 0), profit is
                # (fill - avg) * closed_qty; symmetric for a short.
                closed_qty = abs(pos.openQuantity)
                if pos.openQuantity > 0:
                    self._realized_pnl += (fill_price - pos.averageEntryPrice) * closed_qty
                else:
                    self._realized_pnl += (pos.averageEntryPrice - fill_price) * closed_qty
                self._positions.pop(order.symbol)
            else:
                if (pos.openQuantity > 0) == (signed_qty > 0):
                    # adding to existing direction -> recompute weighted avg
                    pos.averageEntryPrice = (
                        pos.averageEntryPrice * pos.openQuantity + fill_price * signed_qty
                    ) / new_qty
                pos.openQuantity = new_qty
                pos.currentPrice = fill_price

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=OrderAction(order.action).value,  # type: ignore[arg-type]
            quantity=order.totalQuantity,
            price=fill_price,
            commission=self._commission,
            fill_time=datetime.now(UTC),
            venue="paper",
        )
        self._fills.append(fill)
        log.info(
            "paper.order.filled",
            order_id=order.id,
            symbol=order.symbol,
            qty=order.totalQuantity,
            fill_price=fill_price,
            action=order.action.value,
            session_id=self.session_id,
        )
        self._journal_order(order, ref_price=ref_price, accepted=True, rejected_reasons=[])
        self._journal_fill(fill)
        self._journal_equity()
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        # Paper orders fill immediately; nothing to cancel.
        log.info("paper.order.cancel.noop", order_id=order_id, session_id=self.session_id)

    # ----- helpers ---------------------------------------------------------

    def _ensure_journal_dir(self) -> Path | None:
        if not self._journal_dir:
            return None
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        return self._journal_dir

    def _journal_fill(self, fill: Fill) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        # Session id is carried alongside the fill payload; the Fill model itself is broker-neutral,
        # so the wrapper row keeps the schema stable while adding what we need.
        row = {"session_id": self.session_id, **fill.model_dump(mode="json")}
        with (d / "paper_fills.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def _journal_order(self, order: Order, *, ref_price: float | None, accepted: bool,
                       rejected_reasons: list[str]) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        row = {
            "session_id": self.session_id,
            "order_id": order.id,
            "symbol": order.symbol,
            "action": order.action.value,
            "shares": order.totalQuantity,
            "ref_price": ref_price,
            "limit_price": order.limitPrice,
            "accepted": accepted,
            "rejected_reasons": rejected_reasons,
            "ts": datetime.now(UTC).isoformat(),
        }
        with (d / "paper_orders.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def _journal_equity(self) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        positions_value = sum(p.openQuantity * p.currentPrice for p in self._positions.values())
        equity = self._cash + positions_value
        # Unrealized P&L is the mark-to-market against average entry across all open positions.
        unrealized = sum(
            (p.currentPrice - p.averageEntryPrice) * p.openQuantity
            for p in self._positions.values()
        )
        self._peak_equity = max(self._peak_equity, equity)
        drawdown_pct = 0.0 if self._peak_equity <= 0 else (self._peak_equity - equity) / self._peak_equity
        row = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": self.session_id,
            "equity": round(equity, 4),
            "cash": round(self._cash, 4),
            "positions_value": round(positions_value, 4),
            "realized_pnl": round(self._realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "peak_equity": round(self._peak_equity, 4),
            "drawdown_pct": round(drawdown_pct, 6),
        }
        path = d / "paper_equity.csv"
        # Header only on first create — a second run against the same file must not re-emit it.
        write_header = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_EQUITY_COLUMNS)
            if write_header:
                w.writeheader()
            w.writerow(row)

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)
