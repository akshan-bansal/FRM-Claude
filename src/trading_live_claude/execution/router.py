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

        return GateDecision(accepted=not reasons, rejected_reasons=reasons)

    # ----- main entrypoint ----------------------------------------------------

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
    ) -> Order | None:
        decision = self._gate(
            intent, equity=equity, existing_risk=existing_risk, open_positions=open_positions
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
        max_drawdown_pct: float = 0.10,
        daily_loss_limit_pct: float = 0.03,
        max_open_positions: int = 5,
        min_ticket_usd: float = 100.0,
        live_confirmation: str | None = None,
        daily_max_trades: int = 10,
        daily_max_notional_usd: float = 10_000.0,
    ) -> "Router":
        journal = OrderJournal(state_dir)
        ks = KillSwitch(state_dir, max_drawdown_pct=max_drawdown_pct, daily_loss_limit_pct=daily_loss_limit_pct)
        heat = PortfolioHeat(cap_pct=cap_pct)
        if mode == "live":
            if not live_confirmation:
                raise LiveModeNotConfirmed("Pass live_confirmation when mode='live'.")
            return cls.confirm_live(
                confirmation=live_confirmation,
                broker=broker,
                journal=journal,
                kill_switch=ks,
                heat=heat,
                max_open_positions=max_open_positions,
                min_ticket_usd=min_ticket_usd,
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
                max_open_positions=max_open_positions,
                min_ticket_usd=min_ticket_usd,
            )
        return cls(
            mode=mode,
            broker=broker,
            journal=journal,
            kill_switch=ks,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
        )
