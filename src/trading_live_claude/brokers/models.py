"""Pydantic models at the broker boundary. Mapped from Questrade JSON to typed objects."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(StrEnum):
    BUY = "Buy"
    SELL = "Sell"


class OrderAction(StrEnum):
    BUY = "Buy"
    SELL = "Sell"
    BUY_TO_COVER = "BTC"
    SELL_SHORT = "SShort"


class OrderType(StrEnum):
    MARKET = "Market"
    LIMIT = "Limit"
    STOP = "Stop"
    STOP_LIMIT = "StopLimit"


class TimeInForce(StrEnum):
    DAY = "Day"
    GTC = "GoodTillCanceled"
    GTD = "GoodTillDate"


class Account(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    number: str
    status: str
    isPrimary: bool = False
    isBilling: bool = False
    clientAccountType: str = ""


class Position(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    symbolId: int
    openQuantity: float
    closedQuantity: float = 0.0
    currentMarketValue: float = 0.0
    currentPrice: float = 0.0
    averageEntryPrice: float = 0.0
    closedPnl: float = 0.0
    openPnl: float = 0.0
    totalCost: float = 0.0
    isRealTime: bool = False
    isUnderReorg: bool = False


class Quote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symbol: str
    symbolId: int
    bidPrice: float | None = None
    bidSize: int | None = None
    askPrice: float | None = None
    askSize: int | None = None
    lastTradePriceTrHrs: float | None = None
    lastTradePrice: float | None = None
    lastTradeSize: int | None = None
    lastTradeTime: datetime | None = None
    volume: int | None = None
    openPrice: float | None = None
    highPrice: float | None = None
    lowPrice: float | None = None
    delay: int | None = None
    isHalted: bool = False

    @property
    def mid(self) -> float | None:
        if self.bidPrice is not None and self.askPrice is not None and self.bidPrice > 0 and self.askPrice > 0:
            return (self.bidPrice + self.askPrice) / 2.0
        return self.lastTradePrice or self.lastTradePriceTrHrs


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: datetime
    end: datetime
    low: float
    high: float
    open: float
    close: float
    volume: int = 0
    VWAP: float | None = None


class Order(BaseModel):
    """Order intent + lifecycle. Used both as request body and as response normalized form."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    symbol: str
    symbolId: int | None = None
    accountId: str | None = None
    action: OrderAction
    orderType: OrderType
    timeInForce: TimeInForce = TimeInForce.DAY
    totalQuantity: float
    limitPrice: float | None = None
    stopPrice: float | None = None
    primaryRoute: str = "AUTO"
    secondaryRoute: str = "AUTO"
    isAllOrNone: bool = False
    isAnonymous: bool = False

    # Risk metadata (not sent to Questrade — used by router/journal)
    intended_stop: float | None = Field(default=None, exclude=True)
    intended_target: float | None = Field(default=None, exclude=True)
    risk_dollars: float | None = Field(default=None, exclude=True)
    strategy: str | None = Field(default=None, exclude=True)

    def to_questrade_payload(self) -> dict:
        body: dict = {
            "accountNumber": self.accountId,
            "symbolId": self.symbolId,
            "quantity": self.totalQuantity,
            "icebergQuantity": 0,
            "isAllOrNone": self.isAllOrNone,
            "isAnonymous": self.isAnonymous,
            "orderType": self.orderType.value,
            "timeInForce": self.timeInForce.value,
            "action": self.action.value,
            "primaryRoute": self.primaryRoute,
            "secondaryRoute": self.secondaryRoute,
        }
        if self.limitPrice is not None:
            body["limitPrice"] = self.limitPrice
        if self.stopPrice is not None:
            body["stopPrice"] = self.stopPrice
        return body


class Fill(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: int | None
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    commission: float = 0.0
    fill_time: datetime
    venue: Literal["paper", "questrade-practice", "questrade-live"]
