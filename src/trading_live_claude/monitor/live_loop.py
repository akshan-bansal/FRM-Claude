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
from typing import cast

import pandas as pd

from ..brokers.base import Broker
from ..brokers.models import OrderAction
from ..data.market import MarketData
from ..execution.router import OrderIntent, Router
from ..intel.interpret import THEME_EXEMPLARS, Thesis
from ..intel.overlay import OverlayDecision
from ..logging_setup import get_logger
from ..models.risk_mitigation import combine
from ..models.strategy_risk import scalar_from_signals
from ..risk.hedge import HedgePolicy, hedge_shares, hedge_weight, rebalance_delta
from ..risk.risk_model import HeatAggregation, RiskModel, per_trade_risk, portfolio_risk
from ..risk.sizing import PositionSizer
from ..strategies.base import Strategy, StrategyContext

# Interpret-bias floor. Multi-thesis stacking cannot pull conviction below this multiplier — the
# interpret layer is advisory, not a halt, so trimming to zero would violate that contract. The
# overlay layer (with its own floor) and the router's kill-switch handle the actual halt path.
_INTERPRET_BIAS_FLOOR = 0.25
# Per-confidence trim factors. tentative → advisory only (no trim). Multiplicative stacking.
_INTERPRET_BIAS_BY_CONFIDENCE = {"high": 0.5, "moderate": 0.75, "tentative": 1.0}

log = get_logger(__name__)


@dataclass
class MonitorEvent:
    timestamp: datetime
    symbol: str
    kind: str  # 'entry' | 'exit' | 'hold'
    price: float
    detail: dict[str, object]
    # Transition metadata, so persistence mode does not throw away edge information. In edge mode
    # every emitted event is by definition a transition; in level mode the same signal is re-emitted
    # each poll, and these say whether it is new and how long it has been standing.
    is_transition: bool = True
    poll_count: int = 1        # consecutive polls this symbol has been in ``kind`` (1 = just entered)


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
        risk_model: str = "cvar",
        heat_aggregation: str = "corr",
        hedge_symbol: str | None = None,
        hedge_policy: HedgePolicy | None = None,
        overlay_for: Callable[[str], OverlayDecision | None] | None = None,
        interpret_for: Callable[[], list[Thesis]] | None = None,
        weight_bias_for: Callable[[str], float] | None = None,
        strategy_risk: bool = False,
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
        # How the heat gate estimates risk: per-trade model (atr/var/cvar) and aggregation
        # (sum/corr). Default is tail-aware (CVaR) + covariance-aware (corr); set to
        # atr/sum for the original ATR-stop, correlation-blind behaviour.
        self.risk_model: RiskModel = cast(RiskModel, risk_model)
        self.heat_aggregation: HeatAggregation = cast(HeatAggregation, heat_aggregation)
        self.interval_seconds = max(int(interval_seconds), 5)
        self.on_event = on_event or (lambda _: None)
        self.account_currency = account_currency
        # Edge-triggering: when True, on_event fires only when a symbol's signal
        # state (entry/exit/hold) changes from the previous poll, so a persistent
        # signal alerts once instead of every interval. Order routing below is
        # unaffected — only the notification callback is deduplicated.
        self.emit_on_change_only = emit_on_change_only
        self._last_kind: dict[str, str] = {}
        # consecutive polls each symbol has held its current kind (drives poll_count)
        self._kind_runs: dict[str, int] = {}
        # Dynamic dollar-hedge overlay (opt-in): scale a UUP sleeve up as the book draws
        # down. ``_equity_peak`` is tracked in-memory (resets on restart → hedge starts at
        # 0 and re-ramps as fresh drawdown develops).
        self.hedge_symbol = hedge_symbol
        self.hedge_policy = hedge_policy or (HedgePolicy(symbol=hedge_symbol) if hedge_symbol else None)
        self._equity_peak = 0.0
        # Live WorldMonitor risk overlay (opt-in). Given a symbol it returns the current per-asset-
        # class decision, or None. De-risk only: its scalar trims entry conviction (smaller size) and
        # its halt flag blocks NEW entry routing for that class. Exits are never blocked — the overlay
        # can only stand the book down, never trap it in a position.
        self.overlay_for = overlay_for
        # Interpret-thesis bias: called at each entry evaluation to fetch the CURRENT list of
        # fired theses (from intel/interpret.py). If the entry symbol appears in any moderate-
        # or high-confidence thesis's implicated exemplars, conviction is trimmed by the
        # per-confidence factor. NEVER blocks and NEVER boosts — the interpret layer is advisory,
        # so it can only trim, and trimming stacks multiplicatively with a floor of 0.25.
        # Exits are untouched.
        self.interpret_for = interpret_for
        # Cache the last-computed bias per symbol so alerts can surface which theses applied
        # without recomputing at the alert boundary.
        self._interpret_last_applied: dict[str, list[str]] = {}
        # Per-symbol portfolio-allocation weight bias — multiplies conviction at sizing time.
        # Unlike interpret_for (which trims only, floor 0.25), this CAN boost above 1.0 because
        # it represents a diversification-aware rebalance rather than a risk signal: a low-
        # correlation name gets its share of the risk budget lifted, a redundant name gets it
        # cut, and equal-weight is the neutral case (multiplier 1.0). Bounded [0.1, 3.0] so a
        # runaway allocator can't leverage past a sane cap.
        self.weight_bias_for = weight_bias_for
        self._weight_bias_last: dict[str, float] = {}
        # Strategy-risk gate: the trailing-volatility scalar computed from the strategy's own return
        # stream. Chosen over the gradient-boosted classifier because an honest walk-forward showed
        # the simple rule is the better forward-drawdown predictor (6 of 8 real strategies).
        self.strategy_risk = strategy_risk

    def _strategy_for(self, symbol: str) -> Strategy:
        """The per-symbol strategy, falling back to the default ``strategy``."""
        return self.strategy_map.get(symbol, self.strategy)

    def _interpret_bias(self, symbol: str) -> tuple[float, list[str]]:
        """Multiplicative conviction bias from the current interpret() theses.

        Returns ``(multiplier, thesis_names_applied)``. When ``interpret_for`` is not wired or
        no thesis implicates this symbol, returns ``(1.0, [])`` — no effect. When theses do
        implicate it, the per-confidence factor is applied multiplicatively per thesis, with
        the product floored at ``_INTERPRET_BIAS_FLOOR``.

        This never boosts and never blocks — it can only trim conviction. The interpret layer
        is advisory-only per its docstring; enforcing that contract at the gate is the point of
        the floor. Exits ignore this method entirely (see step()).
        """
        if self.interpret_for is None:
            return 1.0, []
        try:
            theses = self.interpret_for() or []
        except Exception as e:                 # pragma: no cover — never break the poll on interpret I/O
            log.warning("monitor.interpret_bias.failed", symbol=symbol, error=str(e))
            return 1.0, []
        if not theses:
            return 1.0, []
        bias = 1.0
        applied: list[str] = []
        for t in theses:
            if t.name == "No notable configuration":
                continue                        # quiet-tape null is not evidence
            # Union of exemplar tickers across this thesis's themes.
            exemplars: set[str] = set()
            for theme in t.themes:
                exemplars.update(THEME_EXEMPLARS.get(theme, ()))
            if symbol in exemplars:
                factor = _INTERPRET_BIAS_BY_CONFIDENCE.get(t.confidence, 1.0)
                if factor < 1.0:
                    bias *= factor
                    applied.append(t.name)
        return max(_INTERPRET_BIAS_FLOOR, bias), applied

    def _open_positions(self) -> dict[str, float]:
        positions = self.broker.positions(self.account_number)
        return {p.symbol: p.openQuantity for p in positions if p.openQuantity != 0}

    def step(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        open_positions = self._open_positions()
        # Per-position risk (ATR-stop proxy by default, or a VaR/CVaR tail estimate of the
        # name's returns), then aggregated for the heat gate — a naive sum by default or a
        # covariance-aware combine that credits diversification.
        pos_risk: dict[str, float] = {}
        pos_rets: dict[str, pd.Series | None] = {}
        for sym, qty in open_positions.items():
            q = self.broker.quote(sym)
            px = q.mid or q.lastTradePrice or 0.0
            rets: pd.Series | None = None
            if self.risk_model != "atr" or self.heat_aggregation == "corr":
                try:
                    rets = self.market.recent(sym, bars=90, interval="1d")["close"].pct_change()
                except Exception:
                    rets = None
            pos_rets[sym] = rets
            pos_risk[sym] = per_trade_risk(qty, px, stop_distance=px * 0.02, returns=rets, model=self.risk_model)
        existing_risk = portfolio_risk(pos_risk, pos_rets, method=self.heat_aggregation)

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
                # Live intelligence overlay: trim conviction by the asset-class risk scalar, and note
                # whether new entries in this class are halted (routing is skipped, alert still fires).
                decision = self.overlay_for(symbol) if self.overlay_for else None
                # Strategy risk (backtestable, from the strategy's own returns) composed with the
                # live OSINT class scalar. Both only de-risk; either can halt new entries.
                srisk = 1.0
                if self.strategy_risk:
                    try:
                        srisk = scalar_from_signals(
                            signals, atr_stop_mult=strat.stop_atr_mult,
                            trail_atr_mult=strat.trail_atr_mult, time_stop_bars=strat.time_stop_bars)
                    except Exception as e:  # pragma: no cover - never break the poll on a risk calc
                        log.warning("monitor.strategy_risk.failed", symbol=symbol, error=str(e))
                mitigation = combine(srisk, decision)
                overlay_halt = mitigation.halt
                conviction *= mitigation.scalar
                # Interpret-thesis bias — trim conviction further when a moderate/high thesis
                # implicates this symbol. Applied AFTER the overlay/strategy composition so the
                # floor and the reasons compose cleanly; recorded on the entry event for audit.
                interp_bias, interp_applied = self._interpret_bias(symbol)
                if interp_bias < 1.0:
                    conviction *= interp_bias
                    self._interpret_last_applied[symbol] = interp_applied
                # Portfolio-allocator weight bias — applied last so it multiplies whatever the
                # gates have already left. Bounded so a runaway allocator can't lever past 3x.
                weight_bias = 1.0
                if self.weight_bias_for is not None:
                    try:
                        weight_bias = float(self.weight_bias_for(symbol))
                    except Exception:                         # never break the poll on allocator I/O
                        weight_bias = 1.0
                    weight_bias = max(0.1, min(3.0, weight_bias))
                    if weight_bias != 1.0:
                        conviction *= weight_bias
                        self._weight_bias_last[symbol] = weight_bias
                sized = self.sizer.size(
                    equity=equity, entry=price, atr_value=atr_value, side="long",
                    annual_vol=annual_vol, conviction=conviction,
                )
                if sized.shares > 0 and not overlay_halt:
                    entry_rets = df["close"].pct_change() if self.risk_model != "atr" else None
                    risk_dollars = per_trade_risk(
                        sized.shares, price, stop_distance=abs(price - sized.stop),
                        returns=entry_rets, model=self.risk_model,
                    )
                    intent = OrderIntent(
                        symbol=symbol,
                        action=OrderAction.BUY,
                        shares=sized.shares,
                        entry=sized.entry,
                        stop=sized.stop,
                        target=sized.target,
                        strategy=strat.name,
                        risk_dollars=risk_dollars,
                        account_number=self.account_number,
                    )
                    self.router.submit(
                        intent,
                        equity=equity,
                        existing_risk=existing_risk,
                        open_positions=len(open_positions),
                    )
                # Alert on the entry SIGNAL regardless of sizeability — the monitor is an
                # alerter, so a real signal must surface even when the account is too small
                # to size a position (0 shares); only the order routing is gated by shares.
                entry_detail: dict[str, object] = {"sized": sized.shares}
                if decision is not None or self.strategy_risk:
                    entry_detail["mitigation"] = {
                        "scalar": mitigation.scalar, "strategy": mitigation.strategy_scalar,
                        "osint": mitigation.osint_scalar, "halt": overlay_halt,
                        "class": decision.asset_class if decision else None,
                    }
                    if overlay_halt:
                        entry_detail["halt_reason"] = "; ".join(mitigation.reasons)
                if interp_bias < 1.0:
                    entry_detail["interpret"] = {
                        "bias": round(interp_bias, 4),
                        "theses": interp_applied,
                    }
                if weight_bias != 1.0:
                    entry_detail["allocator"] = {
                        "weight_bias": round(weight_bias, 4),
                    }
                events.append(MonitorEvent(datetime.now(UTC), symbol, "entry", price, entry_detail))
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
            same = self._last_kind.get(ev.symbol) == ev.kind
            run = self._kind_runs.get(ev.symbol, 0) + 1 if same else 1
            self._kind_runs[ev.symbol] = run
            ev.is_transition = not same
            ev.poll_count = run
            if self.emit_on_change_only and same:
                continue  # edge mode: same state as last poll — suppress duplicate notification
            self._last_kind[ev.symbol] = ev.kind
            self.on_event(ev)

        # Dynamic dollar-hedge overlay: size the hedge sleeve from the book's drawdown and
        # heat, and surface a rebalance when the target position drifts past the no-trade
        # band. A rebalance is an action (not a persistent state), so it bypasses the
        # edge-dedup and always surfaces when due.
        if self.hedge_symbol and self.hedge_policy:
            self._equity_peak = max(self._equity_peak, equity)
            drawdown = equity / self._equity_peak - 1.0 if self._equity_peak > 0 else 0.0
            heat = existing_risk / equity if equity > 0 else 0.0
            target_w = hedge_weight(drawdown, policy=self.hedge_policy, heat=heat)
            try:
                hq = self.broker.quote(self.hedge_symbol)
                hpx = hq.mid or hq.lastTradePrice or 0.0
            except Exception:  # pragma: no cover - broker hiccup shouldn't break the poll
                hpx = 0.0
            if hpx > 0:
                target = hedge_shares(equity=equity, hedge_price=hpx, target_weight=target_w)
                current = int(open_positions.get(self.hedge_symbol, 0.0))
                delta = rebalance_delta(current, target)
                if delta != 0:
                    action = OrderAction.BUY if delta > 0 else OrderAction.SELL
                    intent = OrderIntent(
                        symbol=self.hedge_symbol, action=action, shares=abs(delta), entry=hpx,
                        stop=hpx * 0.9 if delta > 0 else hpx * 1.1, target=None,
                        strategy="dollar_hedge", risk_dollars=abs(delta) * hpx * 0.02,
                        account_number=self.account_number,
                    )
                    try:
                        self.router.submit(intent, equity=equity, existing_risk=existing_risk,
                                            open_positions=len(open_positions))
                    except Exception as e:  # pragma: no cover
                        log.warning("monitor.hedge.route_failed", error=str(e))
                    self.on_event(MonitorEvent(datetime.now(UTC), self.hedge_symbol, "hedge", hpx,
                        {"weight": round(target_w, 3), "target": target, "delta": delta,
                         "drawdown": round(drawdown, 3)}))
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
