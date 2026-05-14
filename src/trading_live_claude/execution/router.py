"""Execution router. The ONLY thing that places orders.

A strategy emits a signal -> ``Router.submit(intent)`` checks every risk gate
and either dispatches to the chosen broker (paper or live) or rejects+logs.

Live mode is only enabled via the explicit class-method constructor
``Router.confirm_live(...)`` which requires a typed confirmation phrase.
Constructing ``Router(mode="live", ...)`` directly raises ``LiveModeNotConfirmed``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ..brokers.base import Broker, OrderRejected
from ..brokers.models import Order, OrderAction, OrderType
from ..logging_setup import get_logger
from ..risk.heat import PortfolioHeat
from ..risk.kill_switch import KillSwitch
from .journal import OrderJournal

RouterMode = Literal["paper", "dry-run", "live"]
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THE RISK"

log = get_logger(__name__)


class LiveModeNotConfirmed(RuntimeError):
    """Raised when live mode is requested without the explicit confirmation token."""


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
        _confirmed: bool = False,
    ) -> None:
        if mode == "live" and not _confirmed:
            raise LiveModeNotConfirmed(
                "Construct Router via Router.confirm_live(...) and supply the confirmation phrase."
            )
        self.mode: RouterMode = mode
        self.broker = broker
        self.journal = journal
        self.kill_switch = kill_switch
        self.heat = heat
        self.max_open_positions = max_open_positions
        self.min_ticket_usd = min_ticket_usd

    # ----- alternate constructor: live mode ------------------------------------

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

    # ----- risk gates ---------------------------------------------------------

    def _gate(self, intent: OrderIntent, *, equity: float, existing_risk: float, open_positions: int) -> GateDecision:
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
        decision = self._gate(intent, equity=equity, existing_risk=existing_risk, open_positions=open_positions)

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

        # build broker order; market for v1 (use stop-loss as a separate child order)
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
        return cls(
            mode=mode,
            broker=broker,
            journal=journal,
            kill_switch=ks,
            heat=heat,
            max_open_positions=max_open_positions,
            min_ticket_usd=min_ticket_usd,
        )
