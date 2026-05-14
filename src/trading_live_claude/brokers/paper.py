"""In-memory paper broker. Wraps a real broker for *quotes/candles* but routes
every order through an in-memory book that simulates fills at the current quote
mid + a configurable slippage.

Use a real Questrade broker as the ``feed`` so paper mode exactly mirrors live
data; the paper broker only short-circuits order placement.
"""
from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from ..logging_setup import get_logger
from .base import Broker, OrderRejected
from .models import Account, Candle, Fill, Order, OrderAction, Position, Quote

log = get_logger(__name__)


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
    ) -> None:
        self._feed = feed
        self._equity = starting_equity
        self._cash = starting_equity
        self._slippage_bps = slippage_bps
        self._commission = commission_per_trade
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._journal_dir = journal_dir

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
        quote = self._feed.quote(order.symbol)
        ref_price = quote.mid or quote.lastTradePrice or order.limitPrice
        if ref_price is None or ref_price <= 0:
            raise OrderRejected(f"No reference price for {order.symbol}; cannot simulate fill")

        slippage = ref_price * (self._slippage_bps / 10_000.0)
        fill_price = ref_price + slippage if order.action == OrderAction.BUY else ref_price - slippage

        order.id = next(self._order_counter)
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
                # closing fill: realize PnL into cash already counted above; drop position
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
        )
        self._journal(fill)
        return order

    def cancel_order(self, account_number: str, order_id: int) -> None:
        # Paper orders fill immediately; nothing to cancel.
        log.info("paper.order.cancel.noop", order_id=order_id)

    # ----- helpers ---------------------------------------------------------

    def _journal(self, fill: Fill) -> None:
        if not self._journal_dir:
            return
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        path = self._journal_dir / "paper_fills.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(fill.model_dump(mode="json"), default=str) + "\n")

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)
