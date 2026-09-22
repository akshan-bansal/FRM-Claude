"""Live monitor loop (article skill #5).

Polls Questrade every ``interval`` seconds. For each symbol:
  * fetch recent candles, append latest quote as a synthetic "current bar"
  * run strategy.generate_signals on the rolling window
  * if last bar emits entry==1: size, gate, and dispatch via Router
  * if last bar emits exit==1 and we hold a position: emit close intent

Designed to be safe to restart: state is reloaded from positions/fills journal.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from ..brokers.base import Broker, StaleQuote
from ..brokers.models import OrderAction
from ..data.market import MarketData
from ..execution.router import OrderIntent, Router
from ..intel.interpret import THEME_EXEMPLARS, Thesis
from ..intel.overlay import OverlayDecision
from ..logging_setup import get_logger
from ..models.risk_mitigation import combine
from ..models.strategy_risk import scalar_from_signals
from ..risk.entry_allocation import EntryCandidate, allocate_entries
from ..risk.hedge import HedgePolicy, hedge_shares, hedge_weight, rebalance_delta
from ..risk.quantity import WHOLE_UNITS, QuantityRule
from ..risk.risk_model import HeatAggregation, RiskModel, per_trade_risk, portfolio_risk
from ..risk.sizing import PositionSizer, SizingResult
from ..risk.sizing_policy import TRADING_DAYS, SizingPolicy
from ..signals.candle_exit import CandleExit
from ..signals.overbought_exit import OverboughtExit
from ..signals.oversold_entry import OversoldEntry
from ..signals.profit_lock import ProfitLock, round_trip_cost_frac
from ..strategies.base import Strategy, StrategyContext

# Interpret-bias floor. Multi-thesis stacking cannot pull conviction below this multiplier — the
# interpret layer is advisory, not a halt, so trimming to zero would violate that contract. The
# overlay layer (with its own floor) and the router's kill-switch handle the actual halt path.
_INTERPRET_BIAS_FLOOR = 0.25
# Per-confidence trim factors. tentative → advisory only (no trim). Multiplicative stacking.
_INTERPRET_BIAS_BY_CONFIDENCE = {"high": 0.5, "moderate": 0.75, "tentative": 1.0}


# Clock/mtime tolerance for the global STOP sentinel (see LiveMonitor._check_stop_sentinel).
_STOP_MTIME_GRACE_S = 2.0


def _atr_or_default(raw: object, price: float) -> float:
    """The bar's ATR, or 2% of price when it is missing, NaN, non-numeric or <= 0.

    ``float(x) or default`` let NaN through (NaN is truthy), which then poisoned sizing and the
    profit lock's ATR-at-entry (audit gap, fixed 2026-09-18).
    """
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return price * 0.02
    return v if math.isfinite(v) and v > 0 else price * 0.02

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
        persistence_for: Callable[[str], tuple[bool, str]] | None = None,
        strategy_risk: bool = False,
        market_open_for: Callable[[str], bool] | None = None,
        corr_lead_lag: int = 0,
        roll_futures: Callable[..., None] | None = None,
        sizing_policy: SizingPolicy | None = None,
        flatten_on_exit: bool = False,
        stop_sentinel_dir: Path | None = None,
        warmup_interval_seconds: int | None = None,
        warmup_minutes: float = 60.0,
        profit_lock: ProfitLock | None = None,
        profit_lock_exempt: frozenset[str] | set[str] | None = None,
        candle_exit: CandleExit | None = None,
        candle_exit_exempt: frozenset[str] | set[str] | None = None,
        overbought_exit: OverboughtExit | None = None,
        overbought_exit_exempt: frozenset[str] | set[str] | None = None,
        oversold_entry: OversoldEntry | None = None,
        oversold_entry_only: frozenset[str] | set[str] | None = None,
        parallel_sizing: bool = False,
        mute_symbols: frozenset[str] | set[str] | None = None,
    ) -> None:
        # Warm-up cadence: poll faster for the first ``warmup_minutes`` after launch, then fall back
        # to ``interval_seconds`` without a restart (a restart would flatten the book). Used to
        # re-establish positions quickly after a flatten-on-exit. None/0 disables it.
        self._started_monotonic = time.monotonic()
        # Wall-clock start, compared against the global STOP file's mtime (see _check_stop_sentinel).
        self._started_wall = time.time()
        self.warmup_interval_seconds = (max(int(warmup_interval_seconds), 5)
                                        if warmup_interval_seconds else None)
        self.warmup_minutes = max(float(warmup_minutes), 0.0)
        self._warmup_logged_end = False
        # When True, ``run_forever`` closes every open position through the Router on the way out
        # (normal return, exception, or a cooperative stop). Off by default so existing callers
        # keep their current behaviour; the paper entry points opt in. See ``flatten``.
        self.flatten_on_exit = flatten_on_exit
        self._stop_requested = False
        # Profit lock (2026-09-18, opt-in): once a long is up ``arm_atr`` ATRs, exit when price
        # retraces from its best polled price by more than a giveback that shrinks as the gain grows
        # (signals.profit_lock). Peak and ATR-at-entry are tracked per symbol in memory; a restart
        # flattens the book anyway, so there is no position to carry them across.
        self.profit_lock = profit_lock
        # Strategy names the lock never applies to. Trend followers are the obvious case: the
        # 2026-09-18 backtest showed the lock cutting ts_momentum winners (QQQ +97% -> +4%).
        self.profit_lock_exempt = frozenset(profit_lock_exempt or ())
        # Exit Variant #2 (opt-in): sell a winner after a bearish reversal candle completes.
        self.candle_exit = candle_exit
        self.candle_exit_exempt = frozenset(candle_exit_exempt or ())
        # Exit Variant #3 (opt-in): sell a winner when the last completed bar is overbought.
        self.overbought_exit = overbought_exit
        self.overbought_exit_exempt = frozenset(overbought_exit_exempt or ())
        # Variant #4 (opt-in): add to a held trend position on an oversold pullback while the
        # strategy's own trend signal is on. Applies only to the strategies in
        # ``oversold_entry_only`` (trend family; user decision 2026-09-18). Each V4 tranche carries
        # its own ATR stop, and one V4 entry per symbol per ``cooldown_bars`` completed bars.
        self.oversold_entry = oversold_entry
        self.oversold_entry_only = frozenset(oversold_entry_only if oversold_entry_only is not None
                                             else {"ts_momentum"})
        self._v4_tranche: dict[str, tuple[float, float]] = {}   # symbol -> (shares, stop)
        self._v4_last_bar: dict[str, object] = {}                # symbol -> bar of the last V4 signal
        self._v4_bar_for: dict[str, object] = {}                 # symbol -> bar of this poll's signal
        # Parallel sizing (opt-in, 2026-09-22): collect every entry of a poll, then allocate slots
        # and leverage headroom jointly (risk.entry_allocation) before routing any of them, instead
        # of routing in watchlist order where the first names starve the rest.
        self.parallel_sizing = parallel_sizing
        # Symbols whose alerts are muted. Signals, sizing, routing and journals are unchanged;
        # only the ``on_event`` notification is skipped (logged as monitor.alert_muted).
        self.mute_symbols = frozenset(s.upper() for s in (mute_symbols or ()))
        self._pl_peak: dict[str, float] = {}
        self._pl_entry_atr: dict[str, float] = {}
        # Symbols sold by the lock whose entry signal has not yet switched off. A level-style entry
        # would otherwise re-buy on the next poll and pay a round trip per poll for nothing.
        self._pl_lockout: set[str] = set()
        # Directory watched for a graceful-stop sentinel. See ``_check_stop_sentinel``: this is
        # how an operator (or Claude) stops a background session cleanly, because a process
        # manager's terminate is a HARD kill on Windows and never reaches a signal handler —
        # measured 2026-09-16, the flatten did not run on TaskStop.
        self.stop_sentinel_dir = stop_sentinel_dir
        # Sizing v2 (opt-in): venue quantity rules, calendar-aware annualization, enforced stops
        # and a sizing decision journal. None keeps the original sizing behaviour exactly.
        self.sizing_policy = sizing_policy
        self.market_open_for = market_open_for
        self.corr_lead_lag = corr_lead_lag
        self.roll_futures = roll_futures
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
        # Cross-path tier 3: graph-persistence entry gate. Same shape as ``overlay_for`` (returns
        # a halt decision + reason), but grounded in the intel graph's ``edge_persistence`` query
        # rather than the current-poll overlay scalar. Enforces "the same 6x reading across five
        # polls is a regime, not noise" — an entry in a symbol whose overlay class is exposed to a
        # persistently-elevated domain is blocked, and the reason lands on the rejection alert.
        # Fail-open: if not wired, entries proceed unaffected.
        self.persistence_for = persistence_for
        self._persistence_last_reason: dict[str, str] = {}
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

    def _daily_returns(self, symbol: str) -> pd.Series:
        """Close-to-close returns indexed by UTC date, so names on different calendars align by day."""
        df = self.market.recent(symbol, bars=90, interval="1d")
        closes = df["close"]
        if "time" in df:
            closes = closes.set_axis(pd.DatetimeIndex(pd.to_datetime(df["time"], utc=True)).normalize())
            closes = closes[~closes.index.duplicated(keep="last")]
        return closes.pct_change()

    def _in_warmup(self) -> bool:
        if not self.warmup_interval_seconds or self.warmup_minutes <= 0:
            return False
        return (time.monotonic() - self._started_monotonic) < self.warmup_minutes * 60.0

    def _effective_interval(self) -> int:
        """The poll interval right now: the warm-up cadence inside the window, else the base.
        Warm-up only ever speeds polling up — it never slows the base interval."""
        if self._in_warmup():
            return min(self.interval_seconds, int(self.warmup_interval_seconds or 0))
        if self.warmup_interval_seconds and not self._warmup_logged_end:
            self._warmup_logged_end = True
            log.info("monitor.warmup.ended", interval_seconds=self.interval_seconds,
                     warmup_minutes=self.warmup_minutes)
        return self.interval_seconds

    def _sleep_seconds(self) -> float:
        interval = self._effective_interval()
        wake = getattr(self.router, "seconds_until_next_wake", None)
        next_wake = wake(self.symbols) if wake is not None else None
        if next_wake is None:
            return float(interval)
        if not any(self._is_open(s) for s in self.symbols):
            return float(min(max(next_wake, 5.0), 3600.0))    # all closed: sleep to next open
        return float(min(max(next_wake, 5.0), interval))

    def _is_open(self, symbol: str) -> bool:
        return self.market_open_for is None or self.market_open_for(symbol)

    def _avg_entry(self, symbol: str) -> float:
        for p in self.broker.positions(self.account_number):
            if p.symbol == symbol:
                return float(getattr(p, "averageEntryPrice", 0.0) or 0.0)
        return 0.0

    def _last_mark(self, symbol: str) -> float:
        for p in self.broker.positions(self.account_number):
            if p.symbol == symbol:
                return float(getattr(p, "currentPrice", 0.0) or getattr(p, "averageEntryPrice", 0.0) or 0.0)
        return 0.0

    def step(self) -> list[MonitorEvent]:
        events: list[MonitorEvent] = []
        # If the broker supports live mark-to-market (PaperBroker does; QuestradeBroker gets fresh
        # numbers from the account API directly and doesn't need it), refresh position currentPrice
        # from the feed and journal a fresh equity row BEFORE reading equity. That way the equity
        # + drawdown numbers we compute this poll reflect the current mid-market, not the stale
        # fill-time prices. Duck-typed check so non-paper brokers stay unaffected.
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                     # pragma: no cover — never break the poll
                log.warning("monitor.mtm.failed", error=str(e))
        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        open_positions = self._open_positions()
        # Per-position risk (ATR-stop proxy by default, or a VaR/CVaR tail estimate of the
        # name's returns), then aggregated for the heat gate — a naive sum by default or a
        # covariance-aware combine that credits diversification.
        pos_risk: dict[str, float] = {}
        pos_rets: dict[str, pd.Series | None] = {}
        for sym, qty in open_positions.items():
            # A zero price would drop a position out of the heat gate; when the venue is closed
            # or the quote is stale, count it at its last mark instead.
            if not self._is_open(sym):
                px = self._last_mark(sym)
            else:
                try:
                    q = self.broker.quote(sym)
                    px = q.mid or q.lastTradePrice or 0.0
                except StaleQuote:
                    px = self._last_mark(sym)
            rets: pd.Series | None = None
            if self.risk_model != "atr" or self.heat_aggregation == "corr":
                try:
                    rets = self._daily_returns(sym)
                except Exception:
                    rets = None
            pos_rets[sym] = rets
            pos_risk[sym] = per_trade_risk(qty, px, stop_distance=px * 0.02, returns=rets, model=self.risk_model)
        existing_risk = portfolio_risk(pos_risk, pos_rets, method=self.heat_aggregation,
                                       lead_lag=self.corr_lead_lag)

        if self.roll_futures is not None:
            try:
                self.roll_futures(equity=equity, existing_risk=existing_risk,
                                  open_positions=len(open_positions))
            except Exception as e:                         # pragma: no cover — never break the poll
                log.warning("monitor.futures_roll.failed", error=str(e))

        release_due = getattr(self.router, "release_due", None)
        if release_due is not None:
            try:
                release_due(equity=equity, existing_risk=existing_risk,
                            open_positions=len(open_positions))
            except Exception as e:                         # pragma: no cover — never break the poll
                log.warning("monitor.release_due.failed", error=str(e))

        # A router that queues closed-venue intents (SessionRouter) wants closed symbols evaluated
        # on their last close; otherwise closed symbols are skipped outright.
        queues_closed = bool(getattr(self.router, "queues_closed_venues", False))
        pending: list[dict[str, object]] = []   # parallel sizing: entries routed after the loop
        for symbol in self.symbols:
            open_now = self._is_open(symbol)
            if not open_now and not queues_closed:
                continue
            strat = self._strategy_for(symbol)
            bars_needed = strat.required_history_bars()
            df = self.market.recent(symbol, bars=bars_needed + 5, interval="1d")
            if len(df) < bars_needed:
                log.warning("monitor.insufficient_history", symbol=symbol, have=len(df), need=bars_needed)
                continue
            ctx = StrategyContext(symbol=symbol, timeframe="1d")
            signals = strat.generate_signals(df, ctx)
            last = signals.iloc[-1]
            if not open_now:
                price = float(last["close"])    # router queues; it re-prices at the open
            else:
                try:
                    quote = self.broker.quote(symbol)
                except StaleQuote:
                    continue    # no entry or exit on a price that isn't moving; other symbols still run
                price = quote.mid or quote.lastTradePrice or float(last["close"])

            # Both entry-trigger channels (2026-09-09). ``entry`` is event-triggered —
            # fires on the fresh cross. ``entry_level`` is state-triggered — fires
            # while the current bar's state satisfies the entry condition. Strategy
            # must set ``supports_level_trigger=True`` for the level column to be
            # consulted; if not, we ignore ``entry_level`` even if present (backward
            # compatible with legacy strategies that don't emit it).
            entry_event = int(last.get("entry", 0)) == 1
            entry_level = (
                bool(getattr(strat, "supports_level_trigger", False))
                and int(last.get("entry_level", 0)) == 1
            )
            entry = entry_event or entry_level
            entry_trigger: str | None = None
            if entry_event and entry_level:
                entry_trigger = "event+level"
            elif entry_event:
                entry_trigger = "event"
            elif entry_level:
                entry_trigger = "level"
            exit_ = int(last.get("exit", 0)) == 1
            atr_value = _atr_or_default(last.get("atr"), price)

            # ``holds`` guards re-entry from level-triggered re-fires: while a position
            # is open, subsequent level-eligible polls fall through to the HOLD branch.
            holds = open_positions.get(symbol, 0.0) > 0
            policy = self.sizing_policy

            # Sizing v2: the vol-target size assumes the ATR stop is honoured, so exit when a live
            # price trades through it. Queued (closed-venue) evaluations use the last close and
            # never trigger a stop.
            stop_hit = False
            stop_level: float | None = None
            if policy is not None and policy.enforce_stops and holds and open_now:
                stop_level = policy.stop_for(symbol, avg_entry=self._avg_entry(symbol), atr_value=atr_value,
                                             atr_multiple=self.sizer.atr_multiple)
                stop_hit = stop_level is not None and price <= stop_level

            # Exit variants — #1 profit lock, #2 candle exit. Long-only (the live loop only opens
            # longs) and evaluated on live prices only; queued closed-venue evaluations never act.
            pl_hit = False
            pl_level: float | None = None
            ce_hit = False                   # any completed-bar signal exit (#2 candle, #3 overbought)
            ce_reason = ""
            ce_patterns: list[str] = []
            rt_cost = 0.0
            pl_on = self.profit_lock is not None and strat.name not in self.profit_lock_exempt
            signal_exits = [(reason, sx) for reason, sx, exempt in (
                ("candle_exit", self.candle_exit, self.candle_exit_exempt),
                ("overbought_exit", self.overbought_exit, self.overbought_exit_exempt),
            ) if sx is not None and strat.name not in exempt]
            ce_on = bool(signal_exits)
            if not holds:
                self._pl_peak.pop(symbol, None)
                self._pl_entry_atr.pop(symbol, None)
            elif open_now and (pl_on or ce_on):
                entry_px = self._avg_entry(symbol)
                if entry_px > 0:
                    atr0 = self._pl_entry_atr.setdefault(symbol, atr_value)
                    # Transaction-cost cross-check: commission per fill from the executing broker
                    # (paper = $4.95), live half-spread from the quote when it has one.
                    bid = getattr(quote, "bidPrice", None)
                    ask = getattr(quote, "askPrice", None)
                    hs_bps = ((ask - bid) / 2.0 / ((ask + bid) / 2.0) * 10_000.0
                              if bid and ask and ask >= bid else None)
                    rt_cost = round_trip_cost_frac(
                        price=entry_px, qty=abs(open_positions.get(symbol, 0.0)),
                        commission_per_fill=float(getattr(self.broker, "commission_per_trade", None)
                                                  or getattr(self.broker, "_commission", 4.95)),
                        half_spread_bps=hs_bps)
                    if pl_on and self.profit_lock is not None:
                        peak = max(self._pl_peak.get(symbol, entry_px), price)
                        self._pl_peak[symbol] = peak
                        pl_level = self.profit_lock.stop_level(side=1, entry=entry_px, extreme=peak,
                                                               atr=atr0, round_trip_cost_frac=rt_cost)
                        pl_hit = pl_level is not None and price <= pl_level
                    if ce_on and len(df) >= 2:
                        # The last daily row can be today's forming candle, so read the signal on
                        # the last COMPLETED bar (iloc[-2]); every detector is past-only, so that
                        # row does not depend on the forming one. Matches the backtest's timing.
                        for reason, sx in signal_exits:
                            fired = sx.fired(df)
                            if fired.empty:
                                continue
                            row = fired.iloc[-2]
                            trig = [str(k) for k, v in row.items() if bool(v)]
                            if trig and sx.armed(entry=entry_px, ref_price=float(df["close"].iloc[-2]),
                                                 atr=atr0, round_trip_cost_frac=rt_cost):
                                ce_hit, ce_reason, ce_patterns = True, reason, trig
                                break

            # Variant #4: a V4 tranche's own ATR stop, then a new V4 add. Live prices only.
            v4_stop_hit = False
            v4_add = False
            v4_trig: list[str] = []
            if not holds:
                self._v4_tranche.pop(symbol, None)
            elif open_now and symbol in self._v4_tranche:
                v4_stop_hit = price <= self._v4_tranche[symbol][1]
            if (self.oversold_entry is not None and holds and open_now
                    and not (exit_ or stop_hit or pl_hit or ce_hit or v4_stop_hit)
                    and strat.name in self.oversold_entry_only and len(df) >= 2
                    and OversoldEntry.trend_on(signals, -2)):
                fired = self.oversold_entry.fired(df)
                if not fired.empty:
                    v4_trig = [str(k) for k, v in fired.iloc[-2].items() if bool(v)]
                has_time = "time" in df.columns
                bar_t = df["time"].iloc[-2] if has_time else df.index[-2]
                last_t = self._v4_last_bar.get(symbol)
                if last_t is None:
                    cooled = True
                elif has_time:
                    cooled = int((df["time"].iloc[:-1] > last_t).sum()) >= self.oversold_entry.cooldown_bars
                else:
                    cooled = False
                v4_add = bool(v4_trig) and cooled
                if v4_add:
                    self._v4_bar_for[symbol] = bar_t

            pl_cooldown = False
            if symbol in self._pl_lockout and not holds:
                if entry:
                    entry = False              # same signal the lock just sold into: wait it out
                    pl_cooldown = True
                else:
                    self._pl_lockout.discard(symbol)

            if (entry and not holds) or v4_add:
                # Volatility targeting (annualized daily vol) + conviction from the strategy's
                # graded signal_strength; the ATR still defines the protective stop.
                rets = df["close"].pct_change().dropna()
                periods = policy.periods_per_year_for(symbol) if policy is not None else TRADING_DAYS
                annual_vol = float(rets.tail(63).std(ddof=0) * (periods ** 0.5)) if len(rets) >= 20 else None
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
                # When the allocator bias already carries the OSINT scalar (``OverlaidBias``, via
                # intel.apply.apply_overlay), take only the strategy-risk part here; the halt and
                # its reasons still come from the full combination above.
                if getattr(self.weight_bias_for, "applies_overlay", False):
                    conviction *= mitigation.strategy_scalar
                else:
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
                qty_rule = policy.quantity_rule_for(symbol) if policy is not None else None
                sized = self.sizer.size(
                    equity=equity, entry=price, atr_value=atr_value, side="long",
                    annual_vol=annual_vol, conviction=conviction,
                    qty_rule=qty_rule or WHOLE_UNITS,
                )
                # Cross-path tier 3: persistence-driven halt. Queries the intel graph via the
                # injected ``persistence_for`` callable; blocks the router submit (but not the
                # alert) when the symbol's overlay class has been exposed to a persistently-
                # elevated domain. Fail-open: if the callable is not wired or its refresh raises,
                # halt stays False and the entry proceeds as before.
                persistence_halt = False
                persistence_reason = ""
                if self.persistence_for is not None:
                    try:
                        persistence_halt, persistence_reason = self.persistence_for(symbol)
                    except Exception as e:                       # never break the poll on graph I/O
                        log.warning("monitor.persistence.failed", symbol=symbol, error=str(e))
                        persistence_halt, persistence_reason = False, ""
                    if persistence_halt:
                        self._persistence_last_reason[symbol] = persistence_reason
                routable = sized.shares > 0 and not overlay_halt and not persistence_halt
                entry_rets = df["close"].pct_change() if self.risk_model != "atr" else None
                if routable and not self.parallel_sizing:
                    self._route_entry(symbol, sized.shares, sized, strat.name, price, entry_rets,
                                      equity=equity, existing_risk=existing_risk,
                                      open_positions=len(open_positions), v4=v4_add)
                journal_row: dict[str, object] | None = None
                if policy is not None:
                    journal_row = {
                        "symbol": symbol, "strategy": strat.name, "price": price, "equity": equity,
                        "signal_strength": None if (ss is None or pd.isna(ss)) else float(ss),
                        "mitigation": mitigation.scalar, "interpret": interp_bias, "weight_bias": weight_bias,
                        "conviction": conviction, "annual_vol": annual_vol, "periods": periods,
                        "qty_raw": sized.raw_shares, "step": qty_rule.step if qty_rule else None,
                        "min_qty": qty_rule.min_qty if qty_rule else None,
                        "min_notional": qty_rule.min_notional if qty_rule else None,
                        "qty": sized.shares, "stop": sized.stop,
                        "routed": bool(sized.shares > 0 and not overlay_halt and not persistence_halt),
                        "reason": ("overlay halt" if overlay_halt else "persistence halt" if persistence_halt
                                   else qty_rule.why_zero(sized.raw_shares, price) if (qty_rule and sized.shares <= 0)
                                   else ""),
                    }
                    if not (routable and self.parallel_sizing):
                        policy.journal(journal_row)
                # Alert on the entry SIGNAL regardless of sizeability — the monitor is an
                # alerter, so a real signal must surface even when the account is too small
                # to size a position (0 shares); only the order routing is gated by shares.
                entry_detail: dict[str, object] = {"sized": sized.shares}
                if v4_add:
                    entry_detail["reason"] = "oversold_entry"
                    entry_detail["patterns"] = v4_trig
                    entry_detail["pattern_bar_close"] = float(df["close"].iloc[-2])
                elif entry_trigger is not None:
                    entry_detail["trigger"] = entry_trigger
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
                if persistence_halt:
                    entry_detail["persistence"] = {"halt": True, "reason": persistence_reason}
                if routable and self.parallel_sizing:
                    pending.append({
                        "symbol": symbol, "sized": sized, "strategy": strat.name, "price": price,
                        "rets": entry_rets, "conviction": conviction, "v4": v4_add,
                        "qty_rule": qty_rule or WHOLE_UNITS, "detail": entry_detail,
                        "journal": journal_row,
                    })
                elif v4_add:
                    self._mark_v4_signal(symbol)   # unroutable add still starts the cooldown
                events.append(MonitorEvent(datetime.now(UTC), symbol, "entry", price, entry_detail))
            elif holds and (exit_ or stop_hit or pl_hit or ce_hit or v4_stop_hit):
                # Close the exact held quantity — truncating to whole units would strand a fractional
                # crypto position (0.36 PAXG -> 0) with no way out. A V4 tranche stop on its own sells
                # only that tranche.
                v4_only = v4_stop_hit and not (exit_ or stop_hit or pl_hit or ce_hit)
                qty = abs(open_positions[symbol])
                if v4_only:
                    qty = min(qty, self._v4_tranche[symbol][0])
                intent = OrderIntent(
                    symbol=symbol,
                    action=OrderAction.SELL,
                    shares=qty,
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
                exit_detail: dict[str, object] = {"shares": qty}
                if stop_hit and not exit_:
                    exit_detail["reason"] = "stop"
                    exit_detail["stop"] = stop_level
                elif pl_hit and not exit_:
                    exit_detail["reason"] = "profit_lock"
                    exit_detail["lock_level"] = pl_level
                    exit_detail["round_trip_cost_frac"] = rt_cost
                    exit_detail["peak"] = self._pl_peak.get(symbol)
                elif ce_hit and not exit_:
                    exit_detail["reason"] = ce_reason
                    exit_detail["patterns"] = ce_patterns
                    exit_detail["pattern_bar_close"] = float(df["close"].iloc[-2])
                    exit_detail["round_trip_cost_frac"] = rt_cost
                elif v4_only:
                    exit_detail["reason"] = "oversold_entry_stop"
                    exit_detail["stop"] = self._v4_tranche[symbol][1]
                self._v4_tranche.pop(symbol, None)
                if v4_only:
                    events.append(MonitorEvent(datetime.now(UTC), symbol, "exit", price, exit_detail))
                    continue
                self._pl_peak.pop(symbol, None)
                self._pl_entry_atr.pop(symbol, None)
                if pl_hit or ce_hit:
                    self._pl_lockout.add(symbol)
                if policy is not None:
                    policy.clear_stop(symbol)
                events.append(MonitorEvent(datetime.now(UTC), symbol, "exit", price, exit_detail))
            else:
                events.append(MonitorEvent(datetime.now(UTC), symbol, "hold", price,
                                           {"profit_lock_cooldown": True} if pl_cooldown else {}))

        if pending:
            self._route_pending(pending, equity=equity, existing_risk=existing_risk,
                                open_positions=open_positions)

        for ev in events:
            same = self._last_kind.get(ev.symbol) == ev.kind
            run = self._kind_runs.get(ev.symbol, 0) + 1 if same else 1
            self._kind_runs[ev.symbol] = run
            ev.is_transition = not same
            ev.poll_count = run
            if self.emit_on_change_only and same:
                continue  # edge mode: same state as last poll — suppress duplicate notification
            self._last_kind[ev.symbol] = ev.kind
            if ev.symbol.upper() in self.mute_symbols:
                log.info("monitor.alert_muted", symbol=ev.symbol, kind=ev.kind)
                continue
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

    def _mark_v4_signal(self, symbol: str) -> None:
        """Start the V4 cooldown from this poll's signal bar, whether or not the add was filled."""
        t = self._v4_bar_for.pop(symbol, None)
        if t is not None:
            self._v4_last_bar[symbol] = t

    def _route_entry(self, symbol: str, shares: float, sized: SizingResult, strategy: str, price: float,
                     entry_rets: pd.Series | None, *, equity: float, existing_risk: float,
                     open_positions: int, v4: bool) -> bool:
        """Build and submit one BUY intent through the Router. True when the Router accepted it."""
        stop = float(sized.stop)
        risk_dollars = per_trade_risk(shares, price, stop_distance=abs(price - stop),
                                      returns=entry_rets, model=self.risk_model)
        intent = OrderIntent(
            symbol=symbol, action=OrderAction.BUY, shares=shares, entry=float(sized.entry),
            stop=stop, target=sized.target, strategy=strategy,
            risk_dollars=risk_dollars, account_number=self.account_number,
        )
        order = self.router.submit(intent, equity=equity, existing_risk=existing_risk,
                                   open_positions=open_positions)
        if self.sizing_policy is not None:
            self.sizing_policy.record_stop(symbol, stop)
        if v4:
            # One V4 per symbol per cooldown window, counted from this signal bar even when the
            # Router trimmed or refused it, so a refused add does not re-fire every poll.
            self._mark_v4_signal(symbol)
            if order is not None:
                prev = self._v4_tranche.get(symbol, (0.0, stop))[0]
                self._v4_tranche[symbol] = (prev + float(order.totalQuantity), stop)
        return order is not None

    def _route_pending(self, pending: list[dict[str, object]], *, equity: float, existing_risk: float,
                       open_positions: dict[str, float]) -> None:
        """Parallel sizing: allocate the poll's entries jointly, then route each one in rank order."""
        cap_pct_for = getattr(self.router, "_position_cap_pct", None)
        max_lev = getattr(self.router, "max_gross_leverage", None)
        max_pos = getattr(self.router, "max_open_positions", None)
        held: dict[str, float] = {}
        book = 0.0
        try:
            for p in self.broker.positions(self.account_number):
                px = float(getattr(p, "currentPrice", 0.0) or getattr(p, "averageEntryPrice", 0.0) or 0.0)
                n = abs(float(p.openQuantity)) * px
                book += n
                held[p.symbol] = held.get(p.symbol, 0.0) + n
        except Exception as e:                    # the Router still gates each intent
            log.warning("monitor.allocation.positions_unavailable", error=str(e))
        budget = (equity * float(max_lev) - book) if isinstance(max_lev, (int, float)) else math.inf
        cands: list[EntryCandidate] = []
        for pe in pending:
            sym = str(pe["symbol"])
            cap = ((equity * float(cap_pct_for(sym)) - held.get(sym, 0.0)) if callable(cap_pct_for)
                   else math.inf)
            cands.append(EntryCandidate(
                symbol=sym, shares=float(cast(SizingResult, pe["sized"]).shares), price=float(cast(float, pe["price"])),
                conviction=float(cast(float, pe["conviction"])), cap_notional=cap,
                new_position=not bool(pe["v4"]), qty_rule=cast(QuantityRule, pe["qty_rule"]),
            ))
        slots = (int(max_pos) - len(open_positions)) if isinstance(max_pos, int) else None
        allocs = allocate_entries(cands, slots=slots, budget=budget)
        log.info("monitor.allocation", candidates=len(cands), slots=slots, budget=round(budget, 2),
                 routed=[a.symbol for a in allocs if a.routed])
        opened = 0
        by_sym = {str(pe["symbol"]): pe for pe in pending}
        for a in allocs:                           # rank order
            pe = by_sym[a.symbol]
            detail = cast(dict[str, object], pe["detail"])
            detail["sized"] = a.shares
            detail["allocation"] = {"proposed": a.proposed_shares, "scale": round(a.scale, 4),
                                    **({"dropped": a.dropped} if a.dropped else {})}
            if a.routed:
                accepted = self._route_entry(
                    a.symbol, a.shares, cast(SizingResult, pe["sized"]), str(pe["strategy"]), float(cast(float, pe["price"])),
                    cast("pd.Series | None", pe["rets"]), equity=equity, existing_risk=existing_risk,
                    open_positions=len(open_positions) + opened, v4=bool(pe["v4"]))
                if accepted and not pe["v4"]:
                    opened += 1
            elif pe["v4"]:
                self._mark_v4_signal(a.symbol)
            row = pe["journal"]
            if self.sizing_policy is not None and isinstance(row, dict):
                row.update({"qty": a.shares, "routed": a.routed, "allocation_scale": round(a.scale, 4),
                            "reason": a.dropped or row.get("reason", "")})
                self.sizing_policy.journal(row)

    def request_stop(self) -> None:
        """Ask ``run_forever`` to leave the loop after the current poll.

        Cooperative so the loop exits through its ``finally`` and the flatten runs. A hard
        kill (SIGKILL, or a Windows task terminate that does not deliver a catchable signal)
        bypasses this entirely — the caller installing a signal handler must not promise
        otherwise. See ``flatten_on_exit``.
        """
        self._stop_requested = True

    # Graceful-stop sentinels, watched under ``stop_sentinel_dir``.
    #   ``STOP``               — stops EVERY session running when it was written. It is not
    #                            consumed: each session honours it only if its mtime is at or after
    #                            that session's own start, so all running sessions stop and a later
    #                            launch ignores the stale file. (Until 2026-09-18 the first session
    #                            to poll deleted it, so only one session stopped.)
    #   ``STOP_<session_id>``  — stops only the session with that PaperBroker session_id, and is
    #                            consumed (deleted) once acted on.
    # These are a one-shot *request*, NOT a risk state, which is the whole difference from the
    # kill-switch's ``HALTED``. HALTED is deliberately persistent and Claude must never clear it —
    # do not wire these two together.
    STOP_SENTINEL = "STOP"

    def _check_stop_sentinel(self) -> bool:
        """True if a stop sentinel applies to this session; requests a cooperative stop.

        The global ``STOP`` counts only if written at/after this session started, and is left in
        place for the other sessions. A per-session ``STOP_<id>`` is consumed.
        """
        if self.stop_sentinel_dir is None:
            return False
        global_stop = self.stop_sentinel_dir / self.STOP_SENTINEL
        try:
            # 2 s grace: on Windows a file's mtime can land a few ms before a time.time() taken
            # just earlier. A genuinely stale STOP is minutes or hours old.
            if (global_stop.exists()
                    and global_stop.stat().st_mtime >= self._started_wall - _STOP_MTIME_GRACE_S):
                log.info("monitor.stop_sentinel.seen", path=str(global_stop), scope="all",
                         flatten_on_exit=self.flatten_on_exit)
                self.request_stop()
                return True
        except OSError:                                    # pragma: no cover — unreadable dir
            pass
        session_id = getattr(self.broker, "session_id", None)
        candidates = [self.stop_sentinel_dir / f"{self.STOP_SENTINEL}_{session_id}"] if session_id else []
        for path in candidates:
            try:
                if not path.exists():
                    continue
            except OSError:                                # pragma: no cover — unreadable dir
                continue
            log.info("monitor.stop_sentinel.seen", path=str(path), scope="session",
                     flatten_on_exit=self.flatten_on_exit)
            try:
                path.unlink()
            except OSError as e:                           # pragma: no cover
                # Could not consume it. Still stop — but say so, because the next launch of a
                # session watching this directory will stop immediately on the same file.
                log.error("monitor.stop_sentinel.unlink_failed", path=str(path), error=str(e))
            self.request_stop()
            return True
        return False

    def flatten(self) -> list[str]:
        """Close every open position through the Router. Returns symbols that FAILED to close.

        Exits route through ``Router.submit`` exactly as a strategy-driven exit does — the risk
        gate is never bypassed, including on the way out. That has a consequence worth knowing:
        ``Router._gate`` applies the kill-switch, the min-ticket floor AND the portfolio-heat cap
        to SELL intents (only max-open-positions, stop-side sanity and the notional cap are
        BUY-guarded). So a flatten CAN be rejected — a tiny residual below min ticket, or a
        tripped kill-switch, will refuse. Rejections are returned to the caller and logged at
        error level rather than swallowed, because a silently half-flattened book is the exact
        failure this method exists to prevent.
        """
        failed: list[str] = []
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                         # pragma: no cover
                log.warning("monitor.flatten.mtm_failed", error=str(e))

        open_positions = self._open_positions()
        if not open_positions:
            log.info("monitor.flatten.already_flat")
            return []

        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        existing_risk = self._book_risk(open_positions)
        log.info("monitor.flatten.start", positions=len(open_positions), equity=equity)

        for symbol, qty in list(open_positions.items()):
            price = self._exit_price(symbol)
            if price <= 0:
                failed.append(symbol)
                log.error("monitor.flatten.no_price", symbol=symbol, qty=qty)
                continue
            intent = OrderIntent(
                symbol=symbol,
                action=OrderAction.SELL,
                shares=abs(qty),
                entry=price,
                stop=price * 1.10,      # protective; exits are market — mirrors the step() exit
                target=None,
                strategy="flatten",
                risk_dollars=0.0,
                account_number=self.account_number,
            )
            try:
                order = self.router.submit(
                    intent,
                    equity=equity,
                    existing_risk=existing_risk,
                    open_positions=len(open_positions),
                )
            except Exception as e:
                failed.append(symbol)
                log.error("monitor.flatten.submit_raised", symbol=symbol, qty=qty, error=str(e))
                continue
            if order is None:
                # Gate rejection. The reason is already in the order journal via
                # Router.submit's intent row (accepted=False, rejected_reasons=[...]).
                failed.append(symbol)
                log.error("monitor.flatten.rejected", symbol=symbol, qty=qty,
                          notional=abs(qty) * price)
                continue
            self.on_event(MonitorEvent(datetime.now(UTC), symbol, "exit", price,
                                       {"shares": abs(qty), "reason": "flatten"}))

        # Re-mark so the session's final journalled equity row reflects the flat (or
        # partially flat) book rather than the pre-flatten marks.
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                         # pragma: no cover
                log.warning("monitor.flatten.final_mtm_failed", error=str(e))

        remaining = self._open_positions()
        if remaining:
            log.error("monitor.flatten.incomplete", remaining=sorted(remaining),
                      failed=sorted(failed))
        else:
            log.info("monitor.flatten.complete", closed=len(open_positions))
        return failed

    def trim_to_slots(self, *, tolerance: float = 0.05) -> list[dict[str, object]]:
        """Trim every holding by the same factor so a raised position cap has room, via the Router.

        One-shot, for after ``max_open_positions`` is raised: a book that filled its slots has no
        cash left for the new ones. The gross book is scaled to ``equity * held / cap`` (4 held
        under a cap of 5 -> 80%), pro rata, so every holding keeps its weight relative to the
        others, as the sizer set them, and the freed cash is about one average position per free
        slot. Nothing happens when the book is already within ``tolerance`` of that. Trims are
        ordinary SELL intents, so they pass the risk gate and land in this session's own
        journals, which is what makes a later ``--resume-session`` replay the trimmed book.
        Symbols whose venue is closed are skipped. Returns one row per trim attempted.
        """
        max_pos = getattr(self.router, "max_open_positions", None)
        if not isinstance(max_pos, int) or max_pos <= 0:
            log.warning("monitor.trim_to_slots.no_cap")
            return []
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                         # pragma: no cover
                log.warning("monitor.trim_to_slots.mtm_failed", error=str(e))
        open_positions = {s: q for s, q in self._open_positions().items() if q > 0}
        n = len(open_positions)
        if n == 0 or n >= max_pos:
            return []
        equity = self.broker.equity(self.account_number, currency=self.account_currency)
        prices = {s: self._exit_price(s) for s in open_positions}
        gross = sum(q * prices[s] for s, q in open_positions.items() if prices[s] > 0)
        target = equity * n / max_pos
        if gross <= 0 or gross <= target * (1.0 + tolerance):
            return []
        scale = target / gross
        existing_risk = self._book_risk(open_positions)
        rule_for = getattr(self.router, "quantity_rule_for", None)
        out: list[dict[str, object]] = []
        for symbol, qty in sorted(open_positions.items()):
            price = prices[symbol]
            if price <= 0:
                continue
            if not self._is_open(symbol):
                log.info("monitor.trim_to_slots.venue_closed", symbol=symbol)
                continue
            rule = rule_for(symbol) if callable(rule_for) else WHOLE_UNITS
            keep = float(rule.floor(qty * scale, price))
            sell = qty - keep
            if sell <= 0:
                continue
            intent = OrderIntent(
                symbol=symbol, action=OrderAction.SELL, shares=sell, entry=price,
                stop=price * 1.10, target=None, strategy="slot_trim", risk_dollars=0.0,
                account_number=self.account_number,
            )
            order = self.router.submit(intent, equity=equity, existing_risk=existing_risk,
                                       open_positions=n)
            row: dict[str, object] = {"symbol": symbol, "held": qty, "sold": sell, "kept": keep,
                                      "price": price, "scale": round(scale, 4),
                                      "accepted": order is not None}
            out.append(row)
            log.info("monitor.trim_to_slots", **row)
            if order is not None:
                self.on_event(MonitorEvent(datetime.now(UTC), symbol, "exit", price,
                                           {"shares": sell, "reason": "slot_trim", "kept": keep,
                                            "scale": round(scale, 4)}))
        if hasattr(self.broker, "mark_to_market"):
            try:
                self.broker.mark_to_market()
            except Exception as e:                         # pragma: no cover
                log.warning("monitor.trim_to_slots.final_mtm_failed", error=str(e))
        return out

    def _book_risk(self, open_positions: dict[str, float]) -> float:
        """Aggregate open risk for the heat gate, the same way ``step`` derives it.

        Duplicates step()'s inline block rather than refactoring it — step is the hot path and
        this is a shutdown-only caller. Unifying the two into one helper is a follow-up.
        """
        pos_risk: dict[str, float] = {}
        pos_rets: dict[str, pd.Series | None] = {}
        for sym, qty in open_positions.items():
            px = self._last_mark(sym) if not self._is_open(sym) else self._exit_price(sym)
            rets: pd.Series | None = None
            if self.risk_model != "atr" or self.heat_aggregation == "corr":
                try:
                    rets = self._daily_returns(sym)
                except Exception:
                    rets = None
            pos_rets[sym] = rets
            pos_risk[sym] = per_trade_risk(qty, px, stop_distance=px * 0.02,
                                           returns=rets, model=self.risk_model)
        return portfolio_risk(pos_risk, pos_rets, method=self.heat_aggregation,
                              lead_lag=self.corr_lead_lag)

    def _exit_price(self, symbol: str) -> float:
        """Best available price to exit at: live mid, else last trade, else the last good mark."""
        try:
            q = self.broker.quote(symbol)
            px = q.mid or q.lastTradePrice or 0.0
        except StaleQuote:
            px = self._last_mark(symbol)
        except Exception:
            px = self._last_mark(symbol)
        return float(px or 0.0)

    def run_forever(self, max_iterations: int | None = None) -> None:
        i = 0
        try:
            # Checked before the first poll too, so a sentinel dropped between launch and the
            # first step still stops the session rather than opening a book first.
            if self._check_stop_sentinel():
                return
            while not self._stop_requested:
                try:
                    self.step()
                except Exception as e:  # pragma: no cover
                    log.exception("monitor.step.error", error=str(e))
                i += 1
                if max_iterations is not None and i >= max_iterations:
                    return
                if self._check_stop_sentinel():
                    return
                time.sleep(self._sleep_seconds())
        finally:
            # Runs on normal return, on a cooperative stop, and on an exception propagating out.
            # Does NOT run on a hard kill — see request_stop's docstring.
            if self.flatten_on_exit:
                try:
                    self.flatten()
                except Exception as e:                     # pragma: no cover — never mask the exit
                    log.exception("monitor.flatten.failed", error=str(e))
