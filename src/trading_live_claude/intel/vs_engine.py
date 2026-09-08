"""VS investment engine — turns a signal + market snapshot into (thesis, intel_ref).

The approval card's prompt carries two short fields sourced from here:

* ``thesis`` — a compact (<=140 char) natural-language reason the engine is
  proposing this trade *right now*. Rendered on the LCD next to the order.
* ``intel_ref`` — an opaque id that names a longer JSON writeup persisted to
  ``state/intel_writeups/{intel_ref}.json``. The card's CENTER button (or a
  phone bridge) can fetch it for the full context.

Deterministic and rule-based. No LLM. No live network. The inputs it wants
already exist in the framework:

* :class:`~trading_live_claude.execution.router.OrderIntent` — what the
  strategy wants to do.
* :class:`~trading_live_claude.intel.overlay.IntelSnapshot` (optional) — the
  live WorldMonitor snapshot the daemon already fetches for the risk overlay.
* :class:`~trading_live_claude.intel.overlay.OverlayDecision` list (optional)
  — the per-asset-class scalar / halt decisions the overlay produced.
* A tiny :class:`MarketContext` dataclass carrying strategy-name specifics
  (score, rank, indicator readings). Kept minimal so callers can populate it
  cheaply.

The engine is a *narrator*, not a decision-maker. It NEVER changes what the
strategy or router decided — it only explains it. The trade the card signs
is still whatever the router accepted; the thesis is metadata.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..logging_setup import get_logger

if TYPE_CHECKING:
    from ..execution.router import OrderIntent
    from .overlay import IntelSnapshot, OverlayClass, OverlayDecision

log = get_logger(__name__)


DEFAULT_WRITEUP_DIR = Path("state/intel_writeups")
THESIS_MAX = 140


# --------------------------------------------------------------------------- #
# inputs                                                                      #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MarketContext:
    """Strategy-side numbers the engine can talk about without re-computing them.

    Every field is optional; the engine skips a clause it has no data for.
    """
    strategy_score: float | None = None       # composite signal score, whatever the strategy uses
    strategy_rank: int | None = None          # rank in a cross-sectional universe
    universe_size: int | None = None          # denominator for rank
    r_multiple: float | None = None           # target/risk ratio
    atr_pct: float | None = None              # ATR as fraction of price
    trend_slope: float | None = None          # e.g. 20d EMA slope in %/day
    rsi_14: float | None = None
    days_since_signal: int | None = None      # 0 = fresh
    notes: tuple[str, ...] = ()               # additional short clauses to append

    @classmethod
    def from_signal_row(
        cls,
        row: object,
        *,
        universe_size: int | None = None,
        notes: tuple[str, ...] = (),
    ) -> "MarketContext":
        """Lift optional columns from a strategy signal row (pandas.Series or dict-like).

        Strategies MAY emit any of these columns from ``generate_signals``:
        ``score``, ``rank``, ``r_multiple``, ``atr_pct``, ``trend_slope``,
        ``rsi_14``, ``days_since_signal``. Missing / NaN / None values are
        skipped — the engine simply won't render a clause for them.
        """
        def _pick(key: str) -> object | None:
            # Prefer .get() (pandas Series, dicts) so missing keys never fall
            # through to getattr — which on a Series would return a method
            # object like Series.rank.
            get = getattr(row, "get", None)
            if callable(get):
                v = get(key)
            else:
                try:
                    v = row[key]  # type: ignore[index]
                except (KeyError, IndexError, TypeError):
                    v = None
            if v is None or callable(v):
                return None
            # NaN check without importing numpy
            try:
                if v != v:  # noqa: PLR0124  (NaN != NaN)
                    return None
            except TypeError:
                pass
            return v

        def _f(key: str) -> float | None:
            v = _pick(key)
            return float(v) if v is not None else None

        def _i(key: str) -> int | None:
            v = _pick(key)
            return int(v) if v is not None else None

        return cls(
            strategy_score=_f("score"),
            strategy_rank=_i("rank"),
            universe_size=universe_size,
            r_multiple=_f("r_multiple"),
            atr_pct=_f("atr_pct"),
            trend_slope=_f("trend_slope"),
            rsi_14=_f("rsi_14"),
            days_since_signal=_i("days_since_signal"),
            notes=notes,
        )


ASSET_CLASS_HINT: dict[str, str] = {
    "ib":        "future",
    "kraken":    "crypto",
    "questrade": "equity",
}


# --------------------------------------------------------------------------- #
# writeup                                                                     #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Writeup:
    """The full explanation persisted to disk under ``intel_ref``."""

    intel_ref: str
    generated_at: datetime
    thesis: str
    strategy: str
    symbol: str
    action: str
    shares: int
    broker: str
    reason_clauses: list[str]
    overlay_snapshot: dict[str, float]
    market_context: dict[str, object]
    warnings: list[str]

    def to_dict(self) -> dict[str, object]:
        d = asdict(self)
        d["generated_at"] = self.generated_at.isoformat()
        return d


# --------------------------------------------------------------------------- #
# engine                                                                      #
# --------------------------------------------------------------------------- #

class VSInvestmentEngine:
    """Build a thesis line + writeup for an approved order intent."""

    def __init__(self, writeup_dir: Path = DEFAULT_WRITEUP_DIR) -> None:
        self.writeup_dir = writeup_dir
        self.writeup_dir.mkdir(parents=True, exist_ok=True)

    # -- public ---------------------------------------------------------- #

    def explain(
        self,
        intent: "OrderIntent",
        *,
        broker: str,
        market: MarketContext | None = None,
        overlay_snapshot: "IntelSnapshot | None" = None,
        overlay_decisions: "list[OverlayDecision] | None" = None,
    ) -> tuple[str, str]:
        """Return ``(thesis, intel_ref)``. Persists a JSON writeup as a side effect."""
        market = market or MarketContext()
        core, notes = self._reason_clauses(intent, broker, market)
        overlay_clause, overlay_map, overlay_warnings = self._overlay_clauses(
            broker, overlay_snapshot, overlay_decisions
        )
        # Priority order (least droppable first): core -> overlay risk -> notes.
        # Risk signals must never be truncated in favor of user-supplied notes.
        clauses: list[str] = list(core)
        if overlay_clause:
            clauses.append(overlay_clause)
        clauses.extend(notes)

        thesis = self._compose_thesis(clauses)
        intel_ref = self._intel_ref(intent, broker)

        writeup = Writeup(
            intel_ref=intel_ref,
            generated_at=datetime.now(UTC),
            thesis=thesis,
            strategy=intent.strategy,
            symbol=intent.symbol,
            action=intent.action.value,
            shares=intent.shares,
            broker=broker,
            reason_clauses=clauses,
            overlay_snapshot=overlay_map,
            market_context=self._market_to_dict(market),
            warnings=overlay_warnings,
        )
        self._persist(writeup)
        return thesis, intel_ref

    # -- reason building ------------------------------------------------- #

    @staticmethod
    def _reason_clauses(
        intent: "OrderIntent", broker: str, m: MarketContext
    ) -> tuple[list[str], list[str]]:
        """Return ``(core_clauses, note_clauses)``. Notes are droppable."""
        parts: list[str] = []

        # what and where
        direction = "long" if intent.action.value.lower().startswith("b") else "short"
        parts.append(f"{intent.strategy} {direction} {intent.symbol} on {broker}")

        # strategy score / rank
        if m.strategy_rank is not None and m.universe_size:
            parts.append(f"rank {m.strategy_rank}/{m.universe_size}")
        elif m.strategy_score is not None:
            parts.append(f"score {m.strategy_score:+.2f}")

        # risk/reward
        if m.r_multiple is not None:
            parts.append(f"R={m.r_multiple:.1f}")
        elif intent.target is not None and intent.entry != intent.stop:
            r = abs(intent.target - intent.entry) / abs(intent.entry - intent.stop)
            parts.append(f"R={r:.1f}")

        # trend/vol tone
        if m.trend_slope is not None:
            arrow = "up" if m.trend_slope > 0 else "down"
            parts.append(f"trend {arrow} {abs(m.trend_slope):.2f}%/d")
        if m.rsi_14 is not None:
            if m.rsi_14 < 30:
                parts.append(f"RSI {m.rsi_14:.0f} oversold")
            elif m.rsi_14 > 70:
                parts.append(f"RSI {m.rsi_14:.0f} overbought")
        if m.atr_pct is not None:
            parts.append(f"ATR {m.atr_pct * 100:.1f}%")

        # freshness
        if m.days_since_signal is not None and m.days_since_signal > 0:
            parts.append(f"signal {m.days_since_signal}d old")

        return parts, list(m.notes)

    @staticmethod
    def _overlay_clauses(
        broker: str,
        snap: "IntelSnapshot | None",
        decisions: "list[OverlayDecision] | None",
    ) -> tuple[str, dict[str, float], list[str]]:
        warnings: list[str] = []
        overlay_map: dict[str, float] = {}
        if snap is None:
            return "", overlay_map, warnings

        overlay_map = {
            "global_alerts":     float(snap.global_alert_count),
            "conflict_active":   float(snap.conflict_events_active),
            "disasters_active":  float(snap.natural_disasters_active),
            "energy_stress":     float(snap.energy_stress),
            "strategic_risk":    float(snap.strategic_risk),
        }
        if snap.fear_greed is not None:
            overlay_map["fear_greed"] = float(snap.fear_greed)
        if snap.degraded:
            warnings.append("intel snapshot degraded — overlay capped conservatively")

        clauses: list[str] = []
        if snap.strategic_risk >= 75:
            clauses.append(f"geo-risk {snap.strategic_risk:.0f}")
        elif snap.strategic_risk >= 60:
            clauses.append("geo elevated")
        if snap.energy_stress >= 0.6:
            clauses.append("energy stress high")
        if snap.fear_greed is not None and snap.fear_greed <= 20:
            clauses.append(f"F&G {snap.fear_greed:.0f}")

        if decisions:
            klass: OverlayClass | None = ASSET_CLASS_HINT.get(broker)  # type: ignore[assignment]
            if klass is not None:
                for d in decisions:
                    if d.asset_class == klass and d.scalar < 0.9:
                        clauses.append(f"{klass} scalar {d.scalar:.2f}")
                        if d.halt_new_entries:
                            warnings.append(
                                f"overlay HALT on {klass} — router should have rejected"
                            )
                        break

        return "; ".join(clauses), overlay_map, warnings

    # -- assembly / persistence ------------------------------------------ #

    @staticmethod
    def _compose_thesis(clauses: list[str]) -> str:
        if not clauses:
            return "no context"
        joined = "; ".join(clauses)
        if len(joined) <= THESIS_MAX:
            return joined
        # Drop clauses from the end (least important first) until it fits,
        # then ellipsize the final clause if needed.
        picks = list(clauses)
        while picks and len("; ".join(picks)) > THESIS_MAX:
            picks.pop()
        if not picks:
            return clauses[0][: THESIS_MAX - 1] + "…"
        joined = "; ".join(picks)
        if len(joined) > THESIS_MAX:
            joined = joined[: THESIS_MAX - 1] + "…"
        return joined

    @staticmethod
    def _intel_ref(intent: "OrderIntent", broker: str) -> str:
        """Stable-ish id so a re-run of the same intent picks the same writeup file."""
        h = hashlib.sha256(
            f"{intent.strategy}|{broker}|{intent.symbol}|{intent.action.value}|"
            f"{intent.shares}|{intent.entry:.4f}|{intent.timestamp.isoformat()}"
            .encode("utf-8")
        ).hexdigest()[:16]
        return f"vs_{h}"

    @staticmethod
    def _market_to_dict(m: MarketContext) -> dict[str, object]:
        return {k: v for k, v in asdict(m).items() if v not in (None, ())}

    def _persist(self, w: Writeup) -> None:
        path = self.writeup_dir / f"{w.intel_ref}.json"
        try:
            path.write_text(json.dumps(w.to_dict(), indent=2, default=str), encoding="utf-8")
        except OSError as e:
            log.warning("vs_engine.persist_failed", intel_ref=w.intel_ref, error=str(e))

    # -- reads ------------------------------------------------------------ #

    def load(self, intel_ref: str) -> Writeup | None:
        path = self.writeup_dir / f"{intel_ref}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Writeup(
            intel_ref=data["intel_ref"],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            thesis=data["thesis"],
            strategy=data["strategy"],
            symbol=data["symbol"],
            action=data["action"],
            shares=int(data["shares"]),
            broker=data["broker"],
            reason_clauses=list(data.get("reason_clauses", [])),
            overlay_snapshot=dict(data.get("overlay_snapshot", {})),
            market_context=dict(data.get("market_context", {})),
            warnings=list(data.get("warnings", [])),
        )
