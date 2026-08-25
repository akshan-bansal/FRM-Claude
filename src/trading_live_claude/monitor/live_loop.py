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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

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
    detail: dict[str, object]


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
        account_currency: str = "CAD",
        emit_on_change_only: bool = True,
        strategy_map: dict[str, Strategy] | None = None,
    ) -> None:
        self.broker = broker
        self.market = market
        self.strategy = strategy
        # Per-symbol strategy overrides. A symbol not in the map uses ``strategy``
        # as the fallback, so single-strategy monitoring stays backward compatible.
        self.strategy_map = strategy_map or {}
        self.sizer = sizer
        self.router = router
        self.account_number = account_number
        self.symbols = symbols
        self.interval_seconds = max(int(interval_seconds), 5)
        self.on_event = on_event or (lambda _: None)
        self.account_currency = account_currency
        # Edge-triggering: when True, on_event fires only when a symbol's signal
        # state (entry/exit/hold) changes from the previous poll, so a persistent
        # signal alerts once instead of every interval. Order routing below is
        # unaffected — only the notification callback is deduplicated.
        self.emit_on_change_only = emit_on_change_only
        self._last_kind: dict[str, str] = {}

    def _strategy_for(self, symbol: str) -> Strategy:
        """The per-symbol strategy, falling back to the default ``strategy``."""
        return self.strategy_map.get(symbol, self.strategy)

    def _open_positions(self) -> dict[str, float]:
        positions = self.broker.positions(self.account_number)
        return {p.symbol: p.openQuantity for p in positions if p.openQuantity != 0}

    def step(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        open_positions = self._open_positions()
        # rough existing risk: sum of |qty * 2% of price| (lacking real stops post-hoc)
        existing_risk = 0.0
        for sym, qty in open_positions.items():
            q = self.broker.quote(sym)
            px = q.mid or q.lastTradePrice or 0.0
            existing_risk += abs(qty) * px * 0.02

        for symbol in self.symbols:
            strat = self._strategy_for(symbol)
            bars_needed = strat.required_history_bars()
            df = self.market.recent(symbol, bars=bars_needed + 5, interval="1d")
            if len(df) < bars_needed:
                log.warning("monitor.insufficient_history", symbol=symbol, have=len(df), need=bars_needed)
                continue
            ctx = StrategyContext(symbol=symbol, timeframe="1d")
            signals = strat.generate_signals(df, ctx)
            last = signals.iloc[-1]
            quote = self.broker.quote(symbol)
            price = quote.mid or quote.lastTradePrice or float(last["close"])

            entry = int(last.get("entry", 0)) == 1
            exit_ = int(last.get("exit", 0)) == 1
            atr_value = float(last.get("atr", price * 0.02)) or price * 0.02

            holds = open_positions.get(symbol, 0.0) > 0

            if entry and not holds:
                # Volatility targeting (annualized daily vol) + conviction from the strategy's
                # graded signal_strength; the ATR still defines the protective stop.
                rets = df["close"].pct_change().dropna()
                annual_vol = float(rets.tail(63).std(ddof=0) * (252.0 ** 0.5)) if len(rets) >= 20 else None
                ss = last.get("signal_strength", 1.0)
                conviction = 1.0 if (ss is None or pd.isna(ss)) else float(ss)
                sized = self.sizer.size(
                    equity=equity, entry=price, atr_value=atr_value, side="long",
                    annual_vol=annual_vol, conviction=conviction,
                )
                if sized.shares > 0:
                    intent = OrderIntent(
                        symbol=symbol,
                        action=OrderAction.BUY,
                        shares=sized.shares,
                        entry=sized.entry,
                        stop=sized.stop,
                        target=sized.target,
                        strategy=strat.name,
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
                    strategy=strat.name,
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
            if self.emit_on_change_only and self._last_kind.get(ev.symbol) == ev.kind:
                continue  # same state as last poll — suppress duplicate notification
            self._last_kind[ev.symbol] = ev.kind
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
