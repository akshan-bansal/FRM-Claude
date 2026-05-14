"""Live monitor loop (article skill #5).

Polls Questrade every ``interval`` seconds. For each symbol:
  * fetch recent candles, append latest quote as a synthetic "current bar"
  * run strategy.generate_signals on the rolling window
  * if last bar emits entry==1: size, gate, and dispatch via Router
  * if last bar emits exit==1 and we hold a position: emit close intent

Designed to be safe to restart: state is reloaded from positions/fills journal.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

import pandas as pd

from ..brokers.base import Broker
from ..brokers.models import OrderAction
from ..data.market import MarketData
from ..execution.router import OrderIntent, Router
from ..logging_setup import get_logger
from ..risk.sizing import PositionSizer
from ..strategies.base import Strategy, StrategyContext

log = get_logger(__name__)


@dataclass
class MonitorEvent:
    timestamp: datetime
    symbol: str
    kind: str  # 'entry' | 'exit' | 'hold'
    price: float
    detail: dict


class LiveMonitor:
    def __init__(
        self,
        *,
        broker: Broker,
        market: MarketData,
        strategy: Strategy,
        sizer: PositionSizer,
        router: Router,
        account_number: str,
        symbols: list[str],
        interval_seconds: int = 60,
        on_event: Callable[[MonitorEvent], None] | None = None,
    ) -> None:
        self.broker = broker
        self.market = market
        self.strategy = strategy
        self.sizer = sizer
        self.router = router
        self.account_number = account_number
        self.symbols = symbols
        self.interval_seconds = max(int(interval_seconds), 5)
        self.on_event = on_event or (lambda _: None)

    def _open_positions(self) -> dict[str, float]:
        positions = self.broker.positions(self.account_number)
        return {p.symbol: p.openQuantity for p in positions if p.openQuantity != 0}

    def step(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        equity = self.broker.equity(self.account_number)
        open_positions = self._open_positions()
        # rough existing risk: sum of |qty * 2% of price| (lacking real stops post-hoc)
        existing_risk = 0.0
        for sym, qty in open_positions.items():
            q = self.broker.quote(sym)
            px = q.mid or q.lastTradePrice or 0.0
            existing_risk += abs(qty) * px * 0.02

        bars_needed = self.strategy.required_history_bars()
        for symbol in self.symbols:
            df = self.market.recent(symbol, bars=bars_needed + 5, interval="1d")
            if len(df) < bars_needed:
                log.warning("monitor.insufficient_history", symbol=symbol, have=len(df), need=bars_needed)
                continue
            ctx = StrategyContext(symbol=symbol, timeframe="1d")
            signals = self.strategy.generate_signals(df, ctx)
            last = signals.iloc[-1]
            quote = self.broker.quote(symbol)
            price = quote.mid or quote.lastTradePrice or float(last["close"])

            entry = int(last.get("entry", 0)) == 1
            exit_ = int(last.get("exit", 0)) == 1
            atr_value = float(last.get("atr", price * 0.02)) or price * 0.02

            holds = open_positions.get(symbol, 0.0) > 0

            if entry and not holds:
                sized = self.sizer.size(equity=equity, entry=price, atr_value=atr_value, side="long")
                if sized.shares > 0:
                    intent = OrderIntent(
                        symbol=symbol,
                        action=OrderAction.BUY,
                        shares=sized.shares,
                        entry=sized.entry,
                        stop=sized.stop,
                        target=sized.target,
                        strategy=self.strategy.name,
                        risk_dollars=sized.dollar_risk,
                        account_number=self.account_number,
                    )
                    self.router.submit(
                        intent,
                        equity=equity,
                        existing_risk=existing_risk,
                        open_positions=len(open_positions),
                    )
                    events.append(MonitorEvent(datetime.now(UTC), symbol, "entry", price, {"sized": sized.shares}))
            elif exit_ and holds:
                qty = open_positions[symbol]
                intent = OrderIntent(
                    symbol=symbol,
                    action=OrderAction.SELL,
                    shares=int(abs(qty)),
                    entry=price,
                    stop=price * 1.10,  # protective; exits are market in v1
                    target=None,
                    strategy=self.strategy.name,
                    risk_dollars=0.0,
                    account_number=self.account_number,
                )
                self.router.submit(
                    intent,
                    equity=equity,
                    existing_risk=existing_risk,
                    open_positions=len(open_positions),
                )
                events.append(MonitorEvent(datetime.now(UTC), symbol, "exit", price, {"shares": int(abs(qty))}))
            else:
                events.append(MonitorEvent(datetime.now(UTC), symbol, "hold", price, {}))

        for ev in events:
            self.on_event(ev)
        return events

    def run_forever(self, max_iterations: int | None = None) -> None:
        i = 0
        while True:
            try:
                self.step()
            except Exception as e:  # pragma: no cover
                log.exception("monitor.step.error", error=str(e))
            i += 1
            if max_iterations is not None and i >= max_iterations:
                return
            time.sleep(self.interval_seconds)
