"""In-memory paper broker. Wraps a real broker for *quotes/candles* but routes
every order through an in-memory book that simulates fills at the current quote
mid + a configurable slippage.

Use a real Questrade broker as the ``feed`` so paper mode exactly mirrors live
data; the paper broker only short-circuits order placement.

Journals (all under ``journal_dir``, one row per event):

* ``paper_fills.jsonl`` — executed fills (compat with the existing writer).
* ``paper_orders.jsonl`` — every intent that reached this broker, accepted or rejected,
  so the intent→fill funnel is reconstructable.
* ``paper_equity.csv`` — equity, cash, positions_value, realized/unrealized P&L,
  peak_equity, and drawdown_pct on every fill. Feeds the go-live pre-check.

Every row carries a per-instance ``session_id`` (uuid4 hex) so overlapping paper
runs stay separable in the record — the go-live drawdown / trade-count asserts
against one session, not an accidental average of two.
"""
from __future__ import annotations

import csv
import itertools
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, Literal

from ..execution.venue_costs import VenueCostModel
from ..logging_setup import get_logger
from .base import Broker, OrderRejected, StaleQuote
from .models import Account, Candle, Fill, Order, OrderAction, Position, Quote

log = get_logger(__name__)


_EQUITY_COLUMNS = (
    "ts", "session_id", "equity", "cash", "positions_value",
    "realized_pnl", "unrealized_pnl", "peak_equity", "drawdown_pct",
)


class PaperBroker(Broker):
    name = "paper"
    # Class-level tag. NOT overwritten per instance — the resolved feed venue lives on
    # ``self._venue`` (journal rows, graph edges); ``.venue`` stays "paper" so anything reading it
    # off the class/instance sees the honest destination: the local simulator.
    venue = "paper"

    class RehydrationMismatch(RuntimeError):
        """The journals disagree with themselves; refusing to guess a book (see ``resume``)."""

    def __init__(
        self,
        feed: Broker,
        starting_equity: float = 100_000.0,
        slippage_bps: float = 5.0,
        commission_per_trade: float | None = None,
        journal_dir: Path | None = None,
        session_id: str | None = None,
        venue: str | None = None,
        fill_model: Literal["mid", "touch"] = "mid",
    ) -> None:
        self._feed = feed
        # "touch" fills buys at the ask and sells at the bid (then slippage), so a wide spread
        # costs what it would in a real book instead of being hidden by a mid-price fill.
        self._fill_model = fill_model
        # Venue tag on every journal row + intel-graph edge. Explicit ``venue`` wins; otherwise
        # inherit from the feed broker's declared ``.venue`` (QuestradeBroker→"questrade",
        # KrakenBroker→"kraken", IBBroker→"ib", IBWebBroker→"ib_web"); fall back to feed's ``name``
        # for any future broker that doesn't declare one.
        self._venue = venue or getattr(feed, "venue", None) or getattr(feed, "name", "unknown")
        self._starting_equity = starting_equity
        self._equity = starting_equity
        self._cash = starting_equity
        self._slippage_bps = slippage_bps
        # Commission: venue-priced unless the caller pins a number. A flat $4.95 is Questrade's
        # equity fee and was charged on every venue, which mispriced crypto by an order of magnitude
        # (2026-09-23: $4.95 on a $184.97 LINK fill = 2.68% of notional, where Kraken charges ~26 bps,
        # about $0.48). ``None`` resolves from the feed's venue; an explicit value still wins, so
        # tests and callers that want a fixed fee (or zero) are unaffected.
        self._cost_model = VenueCostModel.for_venue(self._venue)
        self._commission_override = commission_per_trade
        self._positions: dict[str, Position] = {}
        self._fills: list[Fill] = []
        self._journal_dir = journal_dir
        # Per-instance session id — kept short (uuid4 hex is 32 chars). Two paper runs against the
        # same journal_dir must never commingle in accounting queries, and this is the primary key
        # for that.
        self.session_id = session_id or uuid.uuid4().hex
        # Realized P&L is accrued on closing fills, tracked here so the equity CSV can carry it
        # without recomputing from the fills journal.
        self._realized_pnl = 0.0
        # Same figure computed the pre-2026-09-24 way (full closes only). Not accounting truth —
        # it exists so `resume` can recognise a journal written before partial sells were realized.
        self._realized_pnl_full_closes_only = 0.0
        # Peak equity tracked for the drawdown series feeding the max-drawdown kill-switch invariant.
        self._peak_equity = starting_equity
        # Kill-switch auto-halt wire-up (2026-09-08). Reuses the same file sentinel as the
        # Router's KillSwitch, so tripping here immediately blocks Router.submit() on the
        # next intent. Lazily constructed to avoid a hard config dependency; caller can
        # override via ``kill_switch_thresholds=(max_dd, daily_loss)`` if the paper session
        # needs different limits than the Router. Day-open equity is tracked per UTC date
        # so the daily-loss branch of KillSwitch.evaluate has a stable baseline.
        self._kill_switch = None                              # populated on first _journal_equity
        self._day_open_equity: float = starting_equity
        self._day_open_utc_date: str | None = None
        # Per-instance order ids (2026-09-18: this was a class-level counter shared by every
        # PaperBroker in the process). ``resume`` continues it from the journal's highest id.
        self._order_counter: Iterator[int] = itertools.count(1)

    @property
    def commission_per_trade(self) -> float:
        """Size-independent part of the per-fill commission, for callers that reserve against it.

        Exposed 2026-09-25: the value was only held as ``self._commission``, so
        ``Router._cost_reserve`` and ``monitor.live_loop``'s ``getattr(broker, "commission_per_trade")``
        both silently saw nothing and reserved zero.

        On a percentage venue (Kraken) the fee has no fixed part, so this is 0.0 and a reserve built
        on it understates the true cost — the leverage cap keeps slack for that. Use
        ``commission_for(shares, price)`` when the exact figure matters.
        """
        if self._commission_override is not None:
            return self._commission_override
        return self._cost_model.flat_per_fill + self._cost_model.min_per_order

    def commission_for(self, *, shares: float, price: float) -> float:
        """The commission this venue would charge for one fill of this size."""
        if self._commission_override is not None:
            return self._commission_override
        return self._cost_model.cost_for(shares=shares, price=price)

    # ----- read-only data passes through feed -----------------------------

    def accounts(self) -> list[Account]:
        return [Account(type="Paper", number="PAPER-001", status="Active", isPrimary=True)]

    def positions(self, account_number: str) -> list[Position]:
        return list(self._positions.values())

    def quote(self, symbol: str) -> Quote:
        return self._feed.quote(symbol)

    def quotes(self, symbols: list[str]) -> list[Quote]:
        return self._feed.quotes(symbols)

    def candles(self, symbol: str, start: datetime, end: datetime, interval: str = "OneDay") -> list[Candle]:
        return self._feed.candles(symbol, start, end, interval)

    def equity(self, account_number: str, currency: str = "CAD") -> float:
        """Paper broker is single-currency; `currency` arg accepted for protocol parity."""
        mtm = sum(p.openQuantity * p.currentPrice for p in self._positions.values())
        return self._cash + mtm

    # ----- order placement simulator --------------------------------------

    def place_order(self, order: Order) -> Order:
        # Journal the intent BEFORE trying to fill so a reject is on the record with the same
        # order id as the (eventual) attempt. The alternative — only journaling successes — makes
        # a poll where the strategy fired but the broker declined invisible.
        order.id = next(self._order_counter)
        try:
            quote = self._feed.quote(order.symbol)
        except StaleQuote as e:
            self._journal_order(order, ref_price=None, accepted=False,
                                rejected_reasons=[f"stale_quote: {'; '.join(e.reasons)}"])
            raise OrderRejected(str(e)) from e
        touch = quote.askPrice if order.action == OrderAction.BUY else quote.bidPrice
        if self._fill_model == "touch" and touch is not None and touch > 0:
            ref_price: float | None = touch
        else:
            ref_price = quote.mid or quote.lastTradePrice or order.limitPrice
        if ref_price is None or ref_price <= 0:
            self._journal_order(order, ref_price=None, accepted=False,
                                rejected_reasons=["no_reference_price"])
            raise OrderRejected(f"No reference price for {order.symbol}; cannot simulate fill")

        slippage = ref_price * (self._slippage_bps / 10_000.0)
        fill_price = ref_price + slippage if order.action == OrderAction.BUY else ref_price - slippage

        signed_qty = order.totalQuantity if order.action == OrderAction.BUY else -order.totalQuantity
        commission = self.commission_for(shares=order.totalQuantity, price=fill_price)
        self._apply_fill(order.symbol, signed_qty, fill_price, commission,
                         symbol_id=order.symbolId or 0)

        fill = Fill(
            order_id=order.id,
            symbol=order.symbol,
            side=OrderAction(order.action).value,  # type: ignore[arg-type]
            quantity=order.totalQuantity,
            price=fill_price,
            commission=commission,
            fill_time=datetime.now(UTC),
            # The resolved FEED venue, matching what _journal_fill writes. Was hardcoded "paper",
            # which disagreed with the journal row for every non-Questrade feed. Note this is
            # ``self._venue`` (resolved per instance), NOT the ``venue`` class attribute, which
            # stays "paper".
            venue=self._venue,    # type: ignore[arg-type]  # validated against models.Venue
        )
        self._fills.append(fill)
        log.info(
            "paper.order.filled",
            order_id=order.id,
            symbol=order.symbol,
            qty=order.totalQuantity,
            fill_price=fill_price,
            action=order.action.value,
            session_id=self.session_id,
        )
        self._journal_order(order, ref_price=ref_price, accepted=True, rejected_reasons=[])
        self._journal_fill(fill)
        self._journal_equity()
        return order

    def _apply_fill(self, symbol: str, signed_qty: float, fill_price: float, commission: float,
                    *, symbol_id: int = 0) -> None:
        """Book one fill: cash, position, average entry, realized P&L.

        The single accounting path for both live fills (``place_order``) and journal replay
        (``resume``), so a rehydrated book can't drift from one built fill by fill.
        """
        notional = abs(signed_qty) * fill_price
        self._cash -= signed_qty * fill_price + commission
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(
                symbol=symbol,
                symbolId=symbol_id,
                openQuantity=signed_qty,
                averageEntryPrice=fill_price,
                currentPrice=fill_price,
                totalCost=notional,
            )
            return
        new_qty = pos.openQuantity + signed_qty
        if (pos.openQuantity > 0) == (signed_qty > 0):
            # Adding to the existing direction -> recompute the weighted average entry. Nothing is
            # realized by a fill that only grows a position.
            pos.averageEntryPrice = (
                pos.averageEntryPrice * pos.openQuantity + fill_price * signed_qty
            ) / new_qty
            pos.openQuantity = new_qty
            pos.currentPrice = fill_price
            pos.totalCost = abs(new_qty) * pos.averageEntryPrice
            return

        # Reducing, closing, or crossing zero. Realize against the average entry on the shares
        # actually closed — `min` is what makes a PARTIAL sell book its P&L too. Fixed 2026-09-24:
        # previously only the full-close branch realized anything, so `trim_to_slots` and the V4
        # tranche stop moved proceeds into cash (equity stayed right) while `realized_pnl` in
        # paper_equity.csv silently understated. Measured miss on QT session fba831e3: -$39.92.
        closed_qty = min(abs(pos.openQuantity), abs(signed_qty))
        direction = 1.0 if pos.openQuantity > 0 else -1.0
        realized = direction * (fill_price - pos.averageEntryPrice) * closed_qty
        self._realized_pnl += realized
        if new_qty == 0:
            # Legacy accumulator: pre-2026-09-24 journals only ever booked full closes. `resume`
            # uses it to cross-check a session written by the old accounting (see resume()).
            self._realized_pnl_full_closes_only += realized
            self._positions.pop(symbol)
            return
        if (new_qty > 0) == (pos.openQuantity > 0):
            # Partial reduction: the average entry is unchanged — the remaining shares were bought
            # at the same average as the ones just sold.
            pos.openQuantity = new_qty
            pos.currentPrice = fill_price
            pos.totalCost = abs(new_qty) * pos.averageEntryPrice
            return
        # Crossed through zero: the old position is fully closed (realized above) and the surplus
        # opens a new one in the opposite direction at this fill price. Long-only today, so this is
        # unreachable in practice; it is here so a future short path cannot silently mis-average.
        pos.openQuantity = new_qty
        pos.averageEntryPrice = fill_price
        pos.currentPrice = fill_price
        pos.totalCost = abs(new_qty) * fill_price

    def resume(self, *, tolerance: float = 0.05) -> dict[str, object]:
        """Rebuild this session's book from its own journals, so a restart continues it.

        2026-09-18: every restart had to flatten and re-buy (25 same-name round trips in two days,
        $9.90 each plus ~0.1% in price), and a crashed session's book could never be recovered.
        ``state/`` is ground truth, so this replays the session's rows in ``paper_fills.jsonl``
        through the same accounting as live fills and **cross-checks** the result against the
        session's last ``paper_equity.csv`` row: cash and realized P&L must match within
        ``tolerance`` dollars, and the fills' venue must be this broker's feed venue. On any
        disagreement it raises ``RehydrationMismatch`` rather than guess. Construct the broker
        with the original ``session_id`` and ``starting_equity``.

        Also restores peak equity (so the drawdown kill-switch stays continuous) and continues
        order ids from the journal's highest. Positions are marked at their last fill price
        until the next ``mark_to_market``.
        """
        d = self._journal_dir
        if d is None:
            raise self.RehydrationMismatch("resume needs a journal_dir")
        if self._fills or self._positions or self._realized_pnl:
            raise self.RehydrationMismatch("resume must be called on a fresh broker")
        fills: list[dict[str, object]] = []
        fpath = d / "paper_fills.jsonl"
        if fpath.exists():
            for line in fpath.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("session_id") == self.session_id:
                    fills.append(row)
        last_eq: dict[str, str] | None = None
        epath = d / "paper_equity.csv"
        if epath.exists():
            with epath.open(encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    if r.get("session_id") == self.session_id:
                        last_eq = r
        if not fills and last_eq is None:
            raise self.RehydrationMismatch(f"no journal rows for session {self.session_id}")
        venues = {str(r.get("venue")) for r in fills if r.get("venue")}
        if venues and venues != {self._venue}:
            raise self.RehydrationMismatch(
                f"session {self.session_id} traded on {sorted(venues)}, this feed is {self._venue!r}")
        fills.sort(key=lambda r: (str(r.get("fill_time", "")), int(str(r.get("order_id") or 0))))
        for r in fills:
            qty = float(r["quantity"])                                        # type: ignore[arg-type]
            signed = qty if str(r.get("side")) == OrderAction.BUY.value else -qty
            self._apply_fill(str(r["symbol"]), signed, float(r["price"]),   # type: ignore[arg-type]
                             float(r.get("commission") or 0.0))            # type: ignore[arg-type]
        if last_eq is not None:
            j_cash, j_real = float(last_eq["cash"]), float(last_eq["realized_pnl"])
            if abs(self._cash - j_cash) > tolerance:
                raise self.RehydrationMismatch(
                    f"replayed cash {self._cash:.2f} disagrees with the journal's {j_cash:.2f}; "
                    f"check --paper-equity matches the original session's starting equity")
            # Realized P&L: accept either accounting. A session journalled before 2026-09-24 booked
            # only full closes, so its last row legitimately disagrees with the fixed figure by
            # exactly the partial sells' P&L. Rewriting those rows is not an option (state/ is
            # ground truth), and refusing to resume would strand a live book, so a legacy match is
            # accepted and reported. Cash is checked strictly above and the fix does not change it.
            if abs(self._realized_pnl - j_real) > tolerance:
                legacy = self._realized_pnl_full_closes_only
                if abs(legacy - j_real) <= tolerance:
                    log.warning("paper.resume.legacy_realized_pnl",
                                session_id=self.session_id, journal_realized=round(j_real, 2),
                                replayed_realized=round(self._realized_pnl, 2),
                                unbooked_partial_pnl=round(self._realized_pnl - legacy, 2),
                                note="journal predates the 2026-09-24 partial-sell fix; "
                                     "resuming with the corrected figure")
                else:
                    raise self.RehydrationMismatch(
                        f"replayed realized {self._realized_pnl:.2f} (legacy {legacy:.2f}) disagrees "
                        f"with the journal's {j_real:.2f}; check --paper-equity matches the original "
                        f"session's starting equity")
            self._peak_equity = max(self._peak_equity, float(last_eq["peak_equity"]))
        max_id = max((int(str(r.get("order_id") or 0)) for r in fills), default=0)
        self._order_counter = itertools.count(max_id + 1)
        summary: dict[str, object] = {
            "session_id": self.session_id, "fills_replayed": len(fills),
            "positions": {s: p.openQuantity for s, p in self._positions.items()},
            "cash": round(self._cash, 2), "realized_pnl": round(self._realized_pnl, 2),
            "peak_equity": round(self._peak_equity, 2), "next_order_id": max_id + 1,
        }
        log.info("paper.resumed", **summary)
        return summary

    def cancel_order(self, account_number: str, order_id: int) -> None:
        # Paper orders fill immediately; nothing to cancel.
        log.info("paper.order.cancel.noop", order_id=order_id, session_id=self.session_id)

    def mark_to_market(self, quotes: dict[str, float] | None = None) -> float:
        """Refresh position currentPrice from the feed's quotes, then journal a new equity row.

        Without live MTM the equity CSV only refreshes on fills, so ``unrealized_pnl`` is
        stuck at zero (positions are stamped with their fill price and never updated). Calling
        this on a poll cadence makes the equity, unrealized_pnl, peak_equity, and drawdown_pct
        columns reflect the current mid-market, which is what a P&L reader actually wants.

        ``quotes`` (optional) is a pre-fetched ``{symbol: price}`` map — pass it when the caller
        already has fresh quotes for the same symbols (e.g. LiveMonitor's per-poll quote fetch)
        so we don't hit the feed twice per poll. Without it, this fetches per-symbol from the
        feed. Failures on individual symbols are logged and skipped, not propagated.

        Returns the fresh equity for convenience.
        """
        for pos in list(self._positions.values()):
            px = None
            if quotes is not None:
                px = quotes.get(pos.symbol)
            if px is None:
                try:
                    q = self._feed.quote(pos.symbol)
                    px = q.mid or q.lastTradePrice
                except StaleQuote:
                    continue    # keep the last good mark; the feed wrapper logs the episode once
                except Exception as e:
                    log.warning("paper.mtm.quote_failed",
                                symbol=pos.symbol, error=str(e))
                    continue
            if px is not None and px > 0:
                pos.currentPrice = float(px)
        # Only journal a row when there are positions to mark; a bare cash-only account with no
        # positions produces no new information on MTM (equity == cash, unchanged since last fill).
        if self._positions:
            self._journal_equity()
        positions_value = sum(p.openQuantity * p.currentPrice for p in self._positions.values())
        return self._cash + positions_value

    # ----- helpers ---------------------------------------------------------

    def _ensure_journal_dir(self) -> Path | None:
        if not self._journal_dir:
            return None
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        return self._journal_dir

    def _journal_fill(self, fill: Fill) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        # Session id + venue live on the wrapper so the Fill model itself stays broker-neutral.
        # Venue tag is essential once more than one paper venue writes to the same journal_dir —
        # session_id disambiguates runs, venue disambiguates books.
        # ``venue`` last so it wins over the Fill model's own ``venue`` field, which is a Literal
        # narrowed to the pre-multi-venue set ("paper", "questrade-practice", "questrade-live") and
        # would otherwise clobber the true wrapper venue (kraken, ib_web, …).
        row = {"session_id": self.session_id,
               **fill.model_dump(mode="json"),
               "venue": self._venue}
        with (d / "paper_fills.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        # Close the feedback loop: every fill also lands in the intel graph as a ``traded`` edge
        # so downstream queries (dashboard, thesis calibration) can correlate trades with the news
        # bridges the same graph already carries. Failure to append here must never crash the
        # trade path — wrap and log-only.
        try:
            from ..intel.graph import append_edges, fill_edge
            edge = fill_edge(
                venue=self._venue, symbol=fill.symbol, action=str(fill.side),
                quantity=float(fill.quantity), price=float(fill.price),
                session_id=self.session_id,
                order_id=fill.order_id if fill.order_id is not None else "",
                as_of=fill.fill_time.isoformat(),
            )
            # Graph journal lives beside the fills journal, so a broker built on a tmp dir (tests)
            # can never write the real state/intel_graph.jsonl. In production journal_dir is
            # settings.state_dir (default "state"), i.e. the same file as DEFAULT_GRAPH_JOURNAL.
            append_edges([edge], path=d / "intel_graph.jsonl")
        except Exception as e:                                      # pragma: no cover
            log.warning("paper.fill.intel_graph_failed",
                        symbol=fill.symbol, venue=self._venue, error=str(e))

    def _journal_order(self, order: Order, *, ref_price: float | None, accepted: bool,
                       rejected_reasons: list[str]) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        row = {
            "session_id": self.session_id,
            "venue": self._venue,
            "order_id": order.id,
            "symbol": order.symbol,
            "action": order.action.value,
            "shares": order.totalQuantity,
            "ref_price": ref_price,
            "limit_price": order.limitPrice,
            "accepted": accepted,
            "rejected_reasons": rejected_reasons,
            "ts": datetime.now(UTC).isoformat(),
        }
        with (d / "paper_orders.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def _journal_equity(self) -> None:
        d = self._ensure_journal_dir()
        if d is None:
            return
        positions_value = sum(p.openQuantity * p.currentPrice for p in self._positions.values())
        equity = self._cash + positions_value
        # Unrealized P&L is the mark-to-market against average entry across all open positions.
        unrealized = sum(
            (p.currentPrice - p.averageEntryPrice) * p.openQuantity
            for p in self._positions.values()
        )
        self._peak_equity = max(self._peak_equity, equity)
        drawdown_pct = 0.0 if self._peak_equity <= 0 else (self._peak_equity - equity) / self._peak_equity

        # Daily loss baseline — reset day_open_equity on UTC date change. Both the initial
        # boot (day_open_utc_date is None) and every subsequent date rollover snapshot the
        # equity BEFORE the new day's losses accrue.
        now = datetime.now(UTC)
        today = now.strftime("%Y-%m-%d")
        if self._day_open_utc_date != today:
            self._day_open_utc_date = today
            self._day_open_equity = equity

        row = {
            "ts": now.isoformat(),
            "session_id": self.session_id,
            "equity": round(equity, 4),
            "cash": round(self._cash, 4),
            "positions_value": round(positions_value, 4),
            "realized_pnl": round(self._realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "peak_equity": round(self._peak_equity, 4),
            "drawdown_pct": round(drawdown_pct, 6),
        }
        path = d / "paper_equity.csv"
        # Header only on first create — a second run against the same file must not re-emit it.
        write_header = not path.exists()
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_EQUITY_COLUMNS)
            if write_header:
                w.writeheader()
            w.writerow(row)

        # Auto-halt (2026-09-08 — closes audit-gap Top-5 #5). Call KillSwitch.evaluate() with
        # the freshly-computed equity + peak + day_open. Trips the file sentinel if any
        # threshold is breached; the Router's next submit() will see kill-switch tripped and
        # reject. Lazy construction pattern: read thresholds from settings on first invocation
        # so we don't pin the settings object at PaperBroker construction time.
        try:
            if self._kill_switch is None:
                from ..config import get_settings
                from ..risk.kill_switch import KillSwitch
                _s = get_settings()
                self._kill_switch = KillSwitch(
                    d,
                    max_drawdown_pct=_s.max_drawdown_kill_switch,
                    daily_loss_limit_pct=_s.daily_loss_limit_pct,
                )
            self._kill_switch.evaluate(
                equity=equity,
                peak_equity=self._peak_equity,
                day_open_equity=self._day_open_equity,
            )
        except Exception:                                # noqa: BLE001 — never let telemetry crash a paper session
            pass

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)
