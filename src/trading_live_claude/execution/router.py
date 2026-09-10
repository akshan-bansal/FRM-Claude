"""Execution router. The ONLY thing that places orders.

A strategy emits a signal -> ``Router.submit(intent)`` checks every risk gate
and either dispatches to the chosen broker (paper or live) or rejects+logs.

Modes:
  * ``paper`` / ``dry-run``  — safe defaults; no human confirmation.
  * ``live``                 — real-money orders. Requires the typed phrase
                               ``I UNDERSTAND THE RISK`` passed by a human.
  * ``autonomous``           — Claude-driven loop. NO typed confirmation, BUT
                               the AUTONOMOUS_ENABLED env-var sentinel must be
                               true, AND a daily trade/notional budget is
                               enforced in addition to all other gates.

Constructing ``Router(mode="live"|"autonomous", ...)`` directly raises
``LiveModeNotConfirmed``. Use ``Router.confirm_live`` / ``Router.confirm_autonomous``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..brokers.base import Broker, OrderRejected
from ..brokers.models import Order, OrderAction, OrderType
from ..logging_setup import get_logger
from ..risk.heat import PortfolioHeat
from ..risk.kill_switch import KillSwitch
from .daily_budget import DailyBudget
from .journal import OrderJournal

RouterMode = Literal["paper", "dry-run", "live", "autonomous"]
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISK"
AUTONOMOUS_ENV_VAR = "AUTONOMOUS_ENABLED"

log = get_logger(__name__)


class LiveModeNotConfirmed(RuntimeError):
    """Raised when live/autonomous mode is requested without the explicit confirmation."""


class AutonomousNotEnabled(RuntimeError):
    """Raised when autonomous mode is requested without AUTONOMOUS_ENABLED env var."""


@dataclass
class OrderIntent:
    """High-level order request emitted by a strategy."""

    symbol: str
    action: OrderAction
    shares: int
    entry: float
    stop: float
    target: float | None
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class GateDecision:
    accepted: bool
    rejected_reasons: list[str]


class Router:
    def __init__(
        self,
        *,
        mode: RouterMode,
        broker: Broker,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        daily_budget: DailyBudget | None = None,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        _confirmed: bool = False,
    ) -> None:
        if mode in {"live", "autonomous"} and not _confirmed:
            raise LiveModeNotConfirmed(
                f"Construct Router for mode={mode!r} via Router.confirm_live(...) or "
                "Router.confirm_autonomous(...)."
            )
        self.mode: RouterMode = mode
        self.broker = broker
        self.journal = journal
        self.kill_switch = kill_switch
        self.heat = heat
        self.max_open_positions = max_open_positions
        self.min_ticket_usd = min_ticket_usd
        self.daily_budget = daily_budget
        # Landed 2026-09-08 to catch two failure modes seen on live paper sessions:
        # (a) a single boosted position taking 100% of equity, and (b) portfolio gross
        # notional exceeding cash. See docstrings on the gates + Router.check_forced_exits.
        self.max_gross_leverage = max_gross_leverage
        self.max_position_notional_pct = max_position_notional_pct
        self.force_exit_atr_mult = force_exit_atr_mult
        # 'trim' (default, 2026-09-09) = when the per-symbol notional cap or the
        # portfolio gross-leverage cap would be breached, RESIZE the intent's shares
        # down to fit the tighter of the two caps and accept. 'reject' = the pre-
        # 2026-09-09 behavior of rejecting the whole intent. Trim prevents the
        # observed failure mode where a boosted name (VDY.TO ts_momentum × 2.33x
        # allocator) fires ENTRY every poll, gets rejected every poll, floods
        # Telegram, and never opens even a fractional position — while a 50%-cap
        # position would have been perfectly valid.
        self.on_size_cap_breach: Literal["trim", "reject"] = on_size_cap_breach

    # ----- alternate constructors --------------------------------------------

    @classmethod
    def confirm_live(
        cls,
        *,
        confirmation: str,
        broker: Broker,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
    ) -> "Router":
        if confirmation.strip() != LIVE_CONFIRM_PHRASE:
            raise LiveModeNotConfirmed(
                f'Live mode requires confirmation phrase exactly: "{LIVE_CONFIRM_PHRASE}"'
            )
        return cls(
            mode="live",
            broker=broker,
            journal=journal,
            kill_switch=kill_switch,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            _confirmed=True,
        )

    @classmethod
    def confirm_autonomous(
        cls,
        *,
        broker: Broker,
        journal: OrderJournal,
        kill_switch: KillSwitch,
        heat: PortfolioHeat,
        daily_budget: DailyBudget,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
    ) -> "Router":
        """Build an autonomous-mode router.

        Refuses unless the AUTONOMOUS_ENABLED environment sentinel is set to a
        truthy value AT THE TIME OF CONSTRUCTION. We deliberately do *not*
        cache or shortcut this check; the env-var must be set for every new
        autonomous loop process.
        """
        flag = os.environ.get(AUTONOMOUS_ENV_VAR, "").strip().lower()
        if flag not in {"1", "true", "yes", "on"}:
            raise AutonomousNotEnabled(
                f"Autonomous mode requires environment variable {AUTONOMOUS_ENV_VAR}=true. "
                "Set it explicitly when launching the daemon; do not bake it into shell rc files."
            )
        return cls(
            mode="autonomous",
            broker=broker,
            journal=journal,
            kill_switch=kill_switch,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            daily_budget=daily_budget,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            _confirmed=True,
        )

    # ----- risk gates ---------------------------------------------------------

    def _gate(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float = 0.0,
    ) -> GateDecision:
        reasons: list[str] = []

        # 1. kill switch
        if self.kill_switch.state().halted:
            reasons.append("kill-switch tripped")

        # 2. equity available
        if equity <= 0:
            reasons.append(f"equity {equity} <= 0")

        # 3. share count > 0
        if intent.shares <= 0:
            reasons.append("intent.shares <= 0")

        # 4. min ticket
        notional = intent.shares * intent.entry
        if notional < self.min_ticket_usd:
            reasons.append(f"notional ${notional:,.2f} below min ${self.min_ticket_usd}")

        # 5. open positions cap (only for entries)
        if intent.action == OrderAction.BUY and open_positions >= self.max_open_positions:
            reasons.append(f"open positions {open_positions} >= cap {self.max_open_positions}")

        # 6. portfolio heat
        snap = self.heat.snapshot(equity=equity, open_risk_dollars=existing_risk + intent.risk_dollars)
        if snap.breached:
            reasons.append(f"portfolio heat {snap.heat_pct:.2%} > cap {self.heat.cap_pct:.2%}")

        # 7. stop side sanity
        if intent.action == OrderAction.BUY and intent.stop >= intent.entry:
            reasons.append(f"long stop {intent.stop} >= entry {intent.entry}")
        if intent.action == OrderAction.SELL and intent.stop <= intent.entry:
            reasons.append(f"short stop {intent.stop} <= entry {intent.entry}")

        # 8. daily budget (autonomous mode only)
        if self.daily_budget is not None:
            snap_b = self.daily_budget.snapshot()
            ok, reason = snap_b.admits(additional_notional_usd=notional)
            if not ok:
                reasons.append(reason)

        # 9 + 10. Size caps: per-symbol notional cap + portfolio gross-leverage cap
        # (2026-09-08 / trim mode 2026-09-09). Only apply to entries — exits reduce
        # exposure and are always allowed regardless of size.
        #
        # In 'trim' mode (default), when the intent's proposed notional exceeds the
        # tighter of the two caps, we RESIZE intent.shares down to what fits and let
        # the trade proceed. This closes the loop where a boosted name (allocator
        # 2.33x × ts_momentum × vol-target-at-leverage-cap) would size to 100% of
        # equity, get rejected 39x/session by the per-symbol cap, spam Telegram, and
        # never open even a fractional position — while a 50%-cap position would have
        # been perfectly valid research data.
        #
        # In 'reject' mode, the old behavior of appending a rejection reason. Kept as
        # an option for cases where any breach of the cap is a genuine "don't trade"
        # signal (e.g., misconfigured allocator) rather than a "trim to fit" one.
        if intent.action == OrderAction.BUY and equity > 0 and intent.entry > 0:
            symbol_pct = notional / equity
            gross_leverage = (current_open_notional + notional) / equity
            symbol_over = symbol_pct > self.max_position_notional_pct
            leverage_over = gross_leverage > self.max_gross_leverage

            if symbol_over or leverage_over:
                if self.on_size_cap_breach == "trim":
                    # Compute the max notional that fits BOTH caps. Take the tighter.
                    symbol_max_notional = equity * self.max_position_notional_pct
                    leverage_max_notional = max(
                        0.0, equity * self.max_gross_leverage - current_open_notional
                    )
                    fit_notional = min(symbol_max_notional, leverage_max_notional)
                    if fit_notional <= 0:
                        # No room left at all — sleeve is already at leverage cap and
                        # this new symbol can't fit. Fall through to reject rather
                        # than trim to 0 shares (which the min-ticket gate would flag).
                        reasons.append(
                            f"no room after size caps: symbol cap ${symbol_max_notional:,.0f}, "
                            f"leverage headroom ${leverage_max_notional:,.0f}"
                        )
                    else:
                        # Trim shares to fit. Recompute notional for downstream gates
                        # (min-ticket check must see the trimmed size, not the original).
                        trimmed_shares = int(fit_notional // intent.entry)
                        if trimmed_shares <= 0:
                            reasons.append(
                                f"trim would round to 0 shares (fit ${fit_notional:,.0f} / "
                                f"entry ${intent.entry:,.2f})"
                            )
                        else:
                            log.info("router.size_trimmed",
                                     symbol=intent.symbol,
                                     from_shares=intent.shares,
                                     to_shares=trimmed_shares,
                                     original_notional=notional,
                                     trimmed_notional=trimmed_shares * intent.entry,
                                     reason=(
                                         f"symbol_cap={symbol_pct:.1%}>{self.max_position_notional_pct:.1%}"
                                         if symbol_over else
                                         f"leverage={gross_leverage:.2f}x>{self.max_gross_leverage:.2f}x"
                                     ))
                            intent.shares = trimmed_shares
                            notional = trimmed_shares * intent.entry
                            # Re-check min-ticket on the trimmed size (must not fall through
                            # to a size below the min).
                            if notional < self.min_ticket_usd:
                                reasons.append(
                                    f"after trim: notional ${notional:,.2f} below min "
                                    f"${self.min_ticket_usd}"
                                )
                else:
                    # Reject mode — preserve the pre-2026-09-09 gate behavior.
                    if symbol_over:
                        reasons.append(
                            f"single-name notional {symbol_pct:.1%} > cap {self.max_position_notional_pct:.1%}"
                        )
                    if leverage_over:
                        reasons.append(
                            f"portfolio gross leverage {gross_leverage:.2f}x > cap "
                            f"{self.max_gross_leverage:.2f}x  (open ${current_open_notional:,.0f} + "
                            f"intent ${notional:,.0f} vs equity ${equity:,.0f})"
                        )

        return GateDecision(accepted=not reasons, rejected_reasons=reasons)

    def check_forced_exits(
        self,
        positions: list[dict],
        current_prices: dict[str, float],
        atr_at_entry: dict[str, float],
    ) -> list[dict]:
        """Router-level intra-day exit gate (2026-09-08).

        Runs on the monitor's poll cadence, independent of the strategy's bar cadence.
        Detects positions whose unrealized loss since entry exceeds
        ``force_exit_atr_mult × ATR_at_entry`` and returns close-intent dicts for the
        monitor to submit. Closes the gap where a daily-bar strategy (ts_momentum,
        rsi_meanrevert) can't see a mid-day drawdown until the next daily close.

        Inputs are dicts to keep the router broker-agnostic — the monitor already
        maintains its own position + ATR map and knows how to convert an intent dict
        into an OrderIntent for its broker.

        * ``positions`` — list of dicts with keys: symbol, entry_price, quantity, side
          ('long' | 'short'). Only 'long' currently supported; short-close semantics TBD.
        * ``current_prices`` — {symbol: latest mid quote}. Missing symbol = skip
          (fail-open so a quote hiccup can't spuriously force-close a position).
        * ``atr_at_entry`` — {symbol: ATR value at entry bar}. Missing = skip.

        Returns a list of exit-intent dicts. Empty when the mult is 0 (feature disabled)
        or when no position breaches. Never raises — a per-position exception is logged
        and the position is skipped, matching the fail-open discipline of the other
        intel-adjacent gates.
        """
        if self.force_exit_atr_mult <= 0.0:
            return []
        exits: list[dict] = []
        for pos in positions:
            try:
                sym = pos.get("symbol")
                if not sym:
                    continue
                side = pos.get("side", "long")
                if side != "long":
                    continue  # short semantics deferred
                entry = float(pos.get("entry_price", 0.0))
                qty = float(pos.get("quantity", 0.0))
                if entry <= 0 or qty <= 0:
                    continue
                price = current_prices.get(sym)
                atr = atr_at_entry.get(sym)
                if price is None or atr is None or atr <= 0:
                    continue
                loss_per_share = entry - float(price)
                if loss_per_share <= 0:
                    continue  # position is at or above entry — not a loss
                if loss_per_share > self.force_exit_atr_mult * atr:
                    exits.append({
                        "symbol": sym,
                        "action": "SELL",
                        "shares": qty,
                        "reason": (
                            f"forced-exit: loss ${loss_per_share:.2f}/sh > "
                            f"{self.force_exit_atr_mult:.1f}x ATR ${atr:.2f} since entry ${entry:.2f}"
                        ),
                    })
            except Exception as e:  # pragma: no cover — belt-and-suspenders
                log.warning("router.forced_exit.eval_failed", symbol=pos.get("symbol"), error=str(e))
        return exits

    # ----- main entrypoint ----------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float = 0.0,
    ) -> Order | None:
        decision = self._gate(
            intent, equity=equity, existing_risk=existing_risk, open_positions=open_positions,
            current_open_notional=current_open_notional,
        )

        self.journal.order_intent(
            {
                "mode": self.mode,
                "strategy": intent.strategy,
                "symbol": intent.symbol,
                "action": intent.action.value,
                "shares": intent.shares,
                "entry": intent.entry,
                "stop": intent.stop,
                "target": intent.target,
                "risk_dollars": intent.risk_dollars,
                "accepted": decision.accepted,
                "rejected_reasons": decision.rejected_reasons,
            }
        )

        if not decision.accepted:
            self.journal.rejected({"symbol": intent.symbol, "reasons": decision.rejected_reasons})
            log.warning("router.rejected", symbol=intent.symbol, reasons=decision.rejected_reasons)
            return None

        if self.mode == "dry-run":
            log.info("router.dry_run.skip_placement", symbol=intent.symbol, shares=intent.shares)
            return None

        order = Order(
            symbol=intent.symbol,
            symbolId=intent.symbolId,
            accountId=intent.account_number,
            action=intent.action,
            orderType=OrderType.MARKET,
            totalQuantity=intent.shares,
            intended_stop=intent.stop,
            intended_target=intent.target,
            risk_dollars=intent.risk_dollars,
            strategy=intent.strategy,
        )

        try:
            placed = self.broker.place_order(order)
            self.journal.fill(
                {
                    "mode": self.mode,
                    "broker": self.broker.name,
                    "order_id": placed.id,
                    "symbol": placed.symbol,
                    "shares": placed.totalQuantity,
                    "action": placed.action.value,
                }
            )
            return placed
        except OrderRejected as e:
            self.journal.rejected({"symbol": intent.symbol, "reasons": [f"broker: {e}"]})
            log.error("router.broker_rejected", symbol=intent.symbol, error=str(e))
            return None

    @classmethod
    def build_default(
        cls,
        *,
        mode: RouterMode,
        broker: Broker,
        state_dir: Path,
        cap_pct: float = 0.05,
        max_drawdown_pct: float = 0.03,
        daily_loss_limit_pct: float = 0.03,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        live_confirmation: str | None = None,
        daily_max_trades: int = 10,
        daily_max_notional_usd: float = 10_000.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
    ) -> "Router":
        journal = OrderJournal(state_dir)
        ks = KillSwitch(state_dir, max_drawdown_pct=max_drawdown_pct, daily_loss_limit_pct=daily_loss_limit_pct)
        heat = PortfolioHeat(cap_pct=cap_pct)
        gate_kwargs = dict(
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
        )
        if mode == "live":
            if not live_confirmation:
                raise LiveModeNotConfirmed("Pass live_confirmation when mode='live'.")
            return cls.confirm_live(
                confirmation=live_confirmation,
                broker=broker,
                journal=journal,
                kill_switch=ks,
                heat=heat,
                **gate_kwargs,
            )
        if mode == "autonomous":
            budget = DailyBudget(
                state_dir,
                max_trades_per_day=daily_max_trades,
                max_notional_per_day_usd=daily_max_notional_usd,
            )
            return cls.confirm_autonomous(
                broker=broker,
                journal=journal,
                kill_switch=ks,
                heat=heat,
                daily_budget=budget,
                **gate_kwargs,
            )
        return cls(
            mode=mode,
            broker=broker,
            journal=journal,
            kill_switch=ks,
            heat=heat,
            **gate_kwargs,
        )
