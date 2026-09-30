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
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

from ..audit.ledger import Ledger
from ..brokers.base import Broker, OrderRejected
from ..brokers.models import Order, OrderAction, OrderType
from .venue_costs import VenueCostModel
from ..logging_setup import get_logger
from ..risk.heat import PortfolioHeat
from ..risk.kill_switch import KillSwitch
from ..risk.quantity import QuantityRule, whole_units
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
    """High-level order request emitted by a strategy.

    ``intent_id`` is minted here, at the moment the intent exists, so every downstream record —
    gate decision, rejection, broker order, card approval — can be joined back to one identity.
    Before 2026-09-24 the id was minted inside the approval layer, so ``orders.jsonl`` /
    ``fills.jsonl`` / ``approval.db`` could not be correlated at all, and a run without
    ``--require-card`` had no intent identity whatsoever.
    """

    symbol: str
    action: OrderAction
    shares: float
    entry: float
    stop: float
    target: float | None
    strategy: str
    risk_dollars: float
    account_number: str
    symbolId: int | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    intent_id: str = field(default_factory=lambda: mint_intent_id())
    # Content hash of the strategy instance that produced this intent (audit phase 4). Set by the
    # caller that holds the instance; the Router falls back to its ``strategy_version_for``
    # resolver, and records nothing rather than guessing when neither is available.
    strategy_version: str = ""


def mint_intent_id() -> str:
    """Time-prefixed hex id: sorts naturally by creation, unique per intent.

    Same shape the approval layer has always used for its own ids, so existing card/passbook
    records stay visually consistent with the ones now minted upstream.
    """
    return f"{int(time.time_ns()):016x}-{secrets.token_hex(6)}"


class GateKwargs(TypedDict):
    """The gate knobs every ``Router`` builder forwards, so ``**kwargs`` is checkable.

    ``build_default`` assembles one of these and splats it into ``confirm_live``,
    ``confirm_autonomous`` or the constructor. As a bare dict, mypy inferred the value type as a
    union and rejected — or rather, could not verify — every argument: a misspelled key or a float
    where an int belongs would have reached the risk gate unchallenged.
    """

    ledger: "Ledger | None"
    max_open_positions: int
    min_ticket_usd: float
    max_round_trip_cost_ratio: float
    cost_model: "VenueCostModel | None"
    max_gross_leverage: float
    max_position_notional_pct: float
    force_exit_atr_mult: float
    on_size_cap_breach: Literal["trim", "reject"]


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
        max_round_trip_cost_ratio: float = 0.0,
        cost_model: VenueCostModel | None = None,
        daily_budget: DailyBudget | None = None,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        position_cap_pct_for: Callable[[str], float] | None = None,
        quantity_rule_for: Callable[[str], QuantityRule] | None = None,
        ledger: Ledger | None = None,
        _confirmed: bool = False,
    ) -> None:
        # Audit ledger (AUDIT_LEDGER_SCOPE.md phase 3). Optional and dual-written: the existing
        # journals are unchanged, so a caller that passes nothing behaves exactly as before. The
        # ledger is the richer record (chained, one identity per intent); the journals stay the
        # operational read path until the projection in phase 7 replaces them.
        self.ledger = ledger
        # Resolver name -> strategy version, for intents that don't carry their own. LiveMonitor
        # installs one built from its actual strategy instances (so parameters are reflected).
        self.strategy_version_for: Callable[[str], str] | None = None
        # Computed lazily and cached: the gate source doesn't change within a process, and the
        # thresholds are fixed at construction.
        self._risk_check_version: str | None = None
        # Intent ids already announced as INTENT_CREATED. The card path gates an intent, waits for a
        # verdict, then calls submit() again on the same intent, so without this the creation event
        # would be written twice — the second time after APPROVED, which reads as a second intent.
        # Bounded because a long-running book sees thousands of intents and this is only a dedupe.
        self._announced: deque[str] = deque(maxlen=512)
        # Tradeable quantity per symbol for size-cap trims; whole units unless supplied.
        self.quantity_rule_for = quantity_rule_for or whole_units
        # Per-name notional cap as a function of the symbol (e.g. volatility-scaled); falls back
        # to the flat max_position_notional_pct when not supplied.
        self.position_cap_pct_for = position_cap_pct_for
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
        # Cost-aware size floor (2026-09-25). ``min_ticket_usd`` is venue-blind: at a flat $4.95 a
        # fill it passed a $184.97 Kraken position (2.68% of notional on entry, ~5.4% round trip —
        # it needed a 5.4% move to break even). This rejects an ENTRY whose round-trip cost exceeds
        # the ratio, priced with the venue's real cost shape (flat / bps / per-share). 0.0 = off,
        # which is the historical behaviour. Exits are never gated on cost: getting out must never
        # depend on the trade having been worth it.
        self.max_round_trip_cost_ratio = max_round_trip_cost_ratio
        self.cost_model = cost_model or VenueCostModel.for_venue(getattr(broker, "venue", None))
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
        max_round_trip_cost_ratio: float = 0.0,
        cost_model: VenueCostModel | None = None,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
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
            max_round_trip_cost_ratio=max_round_trip_cost_ratio,
            cost_model=cost_model,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            ledger=ledger,
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
        max_round_trip_cost_ratio: float = 0.0,
        cost_model: VenueCostModel | None = None,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        ledger: Ledger | None = None,
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
            max_round_trip_cost_ratio=max_round_trip_cost_ratio,
            cost_model=cost_model,
            daily_budget=daily_budget,
            max_gross_leverage=max_gross_leverage,
            max_position_notional_pct=max_position_notional_pct,
            force_exit_atr_mult=force_exit_atr_mult,
            on_size_cap_breach=on_size_cap_breach,
            ledger=ledger,
            _confirmed=True,
        )

    # ----- risk gates ---------------------------------------------------------

    def _cost_reserve(self) -> float:
        """Per-order cost the leverage headroom must leave room for, in dollars.

        The gross-leverage cap is expressed in notional, but a fill also debits commission, so a
        trade sized to exactly fill the headroom overdraws cash by the fee. Read off the broker —
        fee structure is broker-specific and the gate must not hardcode one — mirroring how
        ``monitor.live_loop`` already sources ``commission_per_trade``. Any broker that does not
        expose it reserves nothing, which is the pre-2026-09-25 behaviour.
        """
        value = getattr(self.broker, "commission_per_trade", 0.0)
        # Must be a REAL number, not merely float()-able. A broker wrapper that delegates through
        # __getattr__ (or a MagicMock in a test) answers every attribute with a truthy object whose
        # __float__ is 1.0 — that would silently reserve a dollar the broker never charges and
        # tighten a risk gate on fabricated input. Anything that is not plainly numeric reserves
        # nothing, which is the pre-2026-09-25 behaviour.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return max(0.0, float(value))

    def _position_cap_pct(self, symbol: str) -> float:
        """Per-name notional cap, never above the flat ``max_position_notional_pct``.

        A dynamic rule (e.g. ``VolScaledPositionCap``) sets the cap per symbol, but
        ``max_position_notional_pct`` is the GLOBAL ceiling and binds regardless: a low-volatility
        name earns a bigger slice than a jumpy one, and none of them escapes the house limit.
        Before 2026-09-23 the vol-scaled ceiling (0.75) sat above the flat cap (0.50), so a calm
        ETF could take 62% of a paper book through the gate untrimmed (QT session 94ea903d,
        VDY.TO) — vol scaling is a view on one name's risk, not a licence to concentrate.
        Clamped here rather than in the rule so it also covers callables set directly on the
        Router (paper_ib.py / paper_global.py).
        """
        if self.position_cap_pct_for is None:
            return self.max_position_notional_pct
        return min(float(self.position_cap_pct_for(symbol)), self.max_position_notional_pct)

    def _book_notional(self, account: str, symbol: str) -> tuple[float, float] | None:
        """``(open notional, notional already held in symbol)`` from the broker's positions."""
        try:
            positions = self.broker.positions(account)
        except Exception as e:
            log.warning("router.positions_unavailable", error=str(e))
            return None
        total = held = 0.0
        for p in positions:
            price = float(getattr(p, "currentPrice", 0.0) or getattr(p, "averageEntryPrice", 0.0) or 0.0)
            n = abs(float(p.openQuantity)) * price
            total += n
            if p.symbol == symbol:
                held += n
        return total, held

    def _gate(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float | None = None,
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
        book = self._book_notional(intent.account_number, intent.symbol)
        if intent.action == OrderAction.BUY and book is None and current_open_notional is None:
            reasons.append("open notional unavailable: positions could not be read")
            book = (0.0, 0.0)
        book_open, held_in_symbol = book or (0.0, 0.0)
        if current_open_notional is None:
            current_open_notional = book_open
        if intent.action == OrderAction.BUY and equity > 0 and intent.entry > 0:
            cap_pct = self._position_cap_pct(intent.symbol)
            symbol_pct = (held_in_symbol + notional) / equity
            # Cost reserve (2026-09-25). The cap is on NOTIONAL, but the fill also debits the
            # broker's commission, so an order sized to exactly fill the leverage headroom leaves
            # cash short by the fee. With the cap otherwise binding at 1.000x, that is how cash
            # still went negative on 2026-09-17/18 (-$5.24, -$16.36, -$29.79). Reserving the fee
            # only ever tightens the gate, never loosens it. Read off the broker because fee
            # structure is broker-specific and the Router must not hardcode one.
            cost_reserve = self._cost_reserve()
            gross_leverage = (current_open_notional + notional + cost_reserve) / equity
            symbol_over = symbol_pct > cap_pct
            leverage_over = gross_leverage > self.max_gross_leverage

            if symbol_over or leverage_over:
                if self.on_size_cap_breach == "trim":
                    # Compute the max notional that fits BOTH caps. Take the tighter.
                    symbol_max_notional = max(0.0, equity * cap_pct - held_in_symbol)
                    leverage_max_notional = max(
                        0.0,
                        equity * self.max_gross_leverage - current_open_notional - cost_reserve,
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
                        trimmed_shares = self.quantity_rule_for(intent.symbol).floor(
                            fit_notional / intent.entry, intent.entry)
                        if trimmed_shares <= 0:
                            reasons.append(
                                f"trim would round to 0 shares (fit ${fit_notional:,.0f} / "
                                f"entry ${intent.entry:,.2f})"
                            )
                        else:
                            trim_reason = (
                                f"symbol_cap={symbol_pct:.1%}>{cap_pct:.1%}"
                                if symbol_over else
                                f"leverage={gross_leverage:.2f}x>{self.max_gross_leverage:.2f}x"
                            )
                            self._ledger_event("RISK_TRIMMED", {
                                "symbol": intent.symbol,
                                "from_shares": intent.shares,
                                "to_shares": trimmed_shares,
                                "original_notional": notional,
                                "trimmed_notional": trimmed_shares * intent.entry,
                                "reason": trim_reason,
                            }, intent=intent)
                            log.info("router.size_trimmed",
                                     symbol=intent.symbol,
                                     from_shares=intent.shares,
                                     to_shares=trimmed_shares,
                                     original_notional=notional,
                                     trimmed_notional=trimmed_shares * intent.entry,
                                     reason=trim_reason)
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
                            f"single-name notional {symbol_pct:.1%} > cap {cap_pct:.1%}"
                        )
                    if leverage_over:
                        reasons.append(
                            f"portfolio gross leverage {gross_leverage:.2f}x > cap "
                            f"{self.max_gross_leverage:.2f}x  (open ${current_open_notional:,.0f} + "
                            f"intent ${notional:,.0f} vs equity ${equity:,.0f})"
                        )

        # 11. Cost-aware size floor. Last, because a size-cap trim can turn a viable ticket into
        # one that cannot pay for itself, and the floor must judge what would actually be sent.
        if (self.max_round_trip_cost_ratio > 0.0 and intent.action == OrderAction.BUY
                and intent.shares > 0 and intent.entry > 0):
            ratio = self.cost_model.round_trip_cost_ratio(shares=intent.shares, price=intent.entry)
            if ratio > self.max_round_trip_cost_ratio:
                floor = self.cost_model.min_notional_for(
                    max_cost_ratio=self.max_round_trip_cost_ratio, price=intent.entry,
                    shares=intent.shares)
                need = ("no size fits on this venue" if floor == float("inf")
                        else f"needs >= ${floor:,.0f}")
                reasons.append(
                    f"round-trip cost {ratio:.2%} of ${notional:,.2f} notional > cap "
                    f"{self.max_round_trip_cost_ratio:.2%} on {self.cost_model.venue} ({need})"
                )

        return GateDecision(accepted=not reasons, rejected_reasons=reasons)

    def check_forced_exits(
        self,
        # Any, not object: these rows come from a broker payload or a journal replay and mix strings
        # with numbers, and the body reads them defensively (`float(pos.get(...))`). Claiming a
        # precise shape here would be a lie about data this method deliberately does not trust.
        positions: list[dict[str, Any]],
        current_prices: dict[str, float],
        atr_at_entry: dict[str, float],
    ) -> list[dict[str, object]]:
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
        exits: list[dict[str, object]] = []
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

    def announce_intent(self, intent: OrderIntent) -> None:
        """Write INTENT_CREATED the first time this router sees an intent, and only once.

        Called by ``submit`` and by any wrapper that gates before submitting — ``ApprovalRouter``
        runs ``_gate`` itself, waits for the card, and only then calls ``submit``, so if the
        announcement lived solely in ``submit`` the creation event would land in the middle of that
        intent's chain, after SIGNED, reading as though the intent were created twice.

        The intent's own ``timestamp`` is carried in the payload alongside the gap to now: the
        strategy built it before any router saw it, and that pre-gate delay is what this event
        exists to make visible.
        """
        if intent.intent_id in self._announced:
            return
        self._announced.append(intent.intent_id)
        self._ledger_event("INTENT_CREATED", {
            "symbol": intent.symbol,
            "action": intent.action.value,
            "shares": intent.shares,
            "entry": intent.entry,
            "stop": intent.stop,
            "target": intent.target,
            "risk_dollars": intent.risk_dollars,
            "created_at": intent.timestamp.isoformat(),
            "pre_gate_ms": round((datetime.now(UTC) - intent.timestamp).total_seconds() * 1000, 1),
        }, intent=intent)

    def risk_check_version(self) -> str:
        """Content hash of this router's gate code + thresholds (audit phase 4), cached."""
        if self._risk_check_version is None:
            from ..audit.versioning import risk_check_version
            self._risk_check_version = risk_check_version(self)
        return self._risk_check_version

    def _strategy_version(self, intent: OrderIntent) -> str | None:
        if intent.strategy_version:
            return intent.strategy_version
        if self.strategy_version_for is None:
            return None
        try:
            return self.strategy_version_for(intent.strategy) or None
        except Exception as e:                                  # a resolver must never break a trade
            log.warning("router.strategy_version_failed", strategy=intent.strategy, error=str(e))
            return None

    def _ledger_event(self, event: str, payload: dict[str, object], *, intent: OrderIntent,
                      broker_order_id: int | str | None = None,
                      execution: dict[str, object] | None = None) -> None:
        """Dual-write one audit event. Never raises into the order path unless the ledger is strict.

        A ledger that can break a trade is a new failure mode; ``Ledger.strict`` is the explicit
        opt-in for deployments that would rather stop trading than trade unrecorded.
        """
        if self.ledger is None:
            return
        self.ledger.append(event, payload, intent_id=intent.intent_id,
                           broker_order_id=broker_order_id, execution=execution,
                           strategy_id=intent.strategy,
                           strategy_version=self._strategy_version(intent),
                           risk_check_version=self.risk_check_version())

    def submit(
        self,
        intent: OrderIntent,
        *,
        equity: float,
        existing_risk: float,
        open_positions: int,
        current_open_notional: float | None = None,
    ) -> Order | None:
        self.announce_intent(intent)
        decision = self._gate(
            intent, equity=equity, existing_risk=existing_risk, open_positions=open_positions,
            current_open_notional=current_open_notional,
        )

        self._ledger_event(
            "RISK_CHECK" if decision.accepted else "RISK_REJECTED",
            {
                "accepted": decision.accepted,
                "rejected_reasons": decision.rejected_reasons,
                "symbol": intent.symbol,
                "action": intent.action.value,
                "shares": intent.shares,
                "entry": intent.entry,
                "stop": intent.stop,
                "target": intent.target,
                "risk_dollars": intent.risk_dollars,
                "equity": equity,
                "existing_risk": existing_risk,
                "open_positions": open_positions,
            },
            intent=intent,
        )
        self.journal.order_intent(
            {
                "intent_id": intent.intent_id,
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
            self.journal.rejected({"intent_id": intent.intent_id, "symbol": intent.symbol,
                                   "reasons": decision.rejected_reasons})
            log.warning("router.rejected", symbol=intent.symbol, reasons=decision.rejected_reasons)
            return None

        if self.mode == "dry-run":
            log.info("router.dry_run.skip_placement", symbol=intent.symbol, shares=intent.shares)
            return None

        self._ledger_event("BROKER_SUBMITTED", {
            "broker": self.broker.name,
            "venue": getattr(self.broker, "venue", None),
            "symbol": intent.symbol,
            "action": intent.action.value,
            "shares": intent.shares,
            "entry": intent.entry,
        }, intent=intent)

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

        # Read before dispatch: a broker may mutate the order it is handed (PaperBroker assigns
        # the id; a real one can overwrite the quantity), and then "how much did we ask for" is
        # unrecoverable.
        requested_qty = float(order.totalQuantity or 0.0)
        try:
            placed = self.broker.place_order(order)
            # PARTIAL when the broker filled less than was asked for. Paper fills are all-or-
            # nothing, so this is for a real broker; recording it as FILLED would overstate the
            # position on the audit record.
            filled_qty = float(placed.totalQuantity or 0.0)
            fill_event = "PARTIAL" if 0 < filled_qty < requested_qty else "FILLED"
            self._ledger_event(fill_event, {
                "requested_shares": requested_qty,
                "symbol": placed.symbol,
                "action": placed.action.value,
                "shares": placed.totalQuantity,
            }, intent=intent, broker_order_id=placed.id, execution={
                "requested_quantity": requested_qty,
                "broker": self.broker.name,
                "order_id": placed.id,
                "quantity": placed.totalQuantity,
                "price": getattr(placed, "avgExecPrice", None) or getattr(placed, "limitPrice", None),
            })
            self.journal.fill(
                {
                    "intent_id": intent.intent_id,
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
            self._ledger_event("BROKER_REJECTED", {
                "broker": self.broker.name, "symbol": intent.symbol, "error": str(e),
            }, intent=intent)
            self.journal.rejected({"intent_id": intent.intent_id, "symbol": intent.symbol,
                                   "reasons": [f"broker: {e}"]})
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
        max_round_trip_cost_ratio: float = 0.0,
        cost_model: VenueCostModel | None = None,
        live_confirmation: str | None = None,
        daily_max_trades: int = 10,
        daily_max_notional_usd: float = 10_000.0,
        max_gross_leverage: float = 1.0,
        max_position_notional_pct: float = 0.50,
        force_exit_atr_mult: float = 3.0,
        on_size_cap_breach: Literal["trim", "reject"] = "trim",
        position_cap_pct_for: Callable[[str], float] | None = None,
        ledger: Ledger | None = None,
    ) -> "Router":
        journal = OrderJournal(state_dir)
        ks = KillSwitch(state_dir, max_drawdown_pct=max_drawdown_pct, daily_loss_limit_pct=daily_loss_limit_pct)
        heat = PortfolioHeat(cap_pct=cap_pct)
        gate_kwargs: GateKwargs = {
            "ledger": ledger,
            "max_open_positions": max_open_positions,
            "min_ticket_usd": min_ticket_usd,
            "max_round_trip_cost_ratio": max_round_trip_cost_ratio,
            "cost_model": cost_model,
            "max_gross_leverage": max_gross_leverage,
            "max_position_notional_pct": max_position_notional_pct,
            "force_exit_atr_mult": force_exit_atr_mult,
            "on_size_cap_breach": on_size_cap_breach,
        }
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
            position_cap_pct_for=position_cap_pct_for,
            **gate_kwargs,
        )
