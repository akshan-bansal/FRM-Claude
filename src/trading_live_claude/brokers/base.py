"""Broker protocol. Both QuestradeBroker and PaperBroker implement this."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .models import Account, Candle, Order, Position, Quote


class BrokerError(Exception):
    """Base broker error."""


class TokenExpired(BrokerError):
    """Refresh token rejected; re-auth required."""


class OrderRejected(BrokerError):
    """Broker rejected the order (validation, insufficient funds, halted, etc.)."""


@runtime_checkable
class Broker(Protocol):
    """Minimal broker surface for strategies/router."""

    name: str

    def accounts(self) -> list[Account]: ...

    def positions(self, account_number: str) -> list[Position]: ...

    def quote(self, symbol: str) -> Quote: ...

    def quotes(self, symbols: list[str]) -> list[Quote]: ...

    def candles(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "OneDay",
    ) -> list[Candle]: ...

    def equity(self, account_number: str, currency: str = "CAD") -> float: ...

    def place_order(self, order: Order) -> Order: ...

    def cancel_order(self, account_number: str, order_id: int) -> None: ...
