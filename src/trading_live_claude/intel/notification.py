"""Notification formatters — deeper informational storytelling for push alerts.

Every notification the system pushes to Telegram/email/stdout comes through one of these
formatters so the reader sees a consistent shape across sources: entry, exit, thesis fire,
persistence hit, wash event.

Design goals, in order:

1. **What happened** first — a one-line header the phone will surface even before the body.
2. **Asset context** — for trading events, name the class + strategy + sizing chain so the reader
   sees WHY this trade was this size, not just that a trade occurred. For intel events, name the
   implicated exposures with tickers so the message is actionable, not academic.
3. **Interpretive gloss** — what the numbers *mean*, in the words interpret.py already uses. The
   overlay's mitigation scalars and the interpret theses that trimmed conviction both get a plain
   sentence, not a dict dump.
4. **Phone-readable** — sections separated by blank lines, bullets only when they help, no emoji
   (per the project style rule), no tables (Telegram / SMS lose column alignment anyway).

Every formatter returns ``(title, body)`` so the caller can hand them to ``Alerter.send`` unchanged.
Callers stay dumb: the storytelling logic lives here.
"""
from __future__ import annotations

from typing import Any


# ---- helpers ------------------------------------------------------------------------------------

def _pct(x: float, digits: int = 2) -> str:
    return f"{x * 100:+.{digits}f}%"


def _theme_label(theme: str) -> str:
    """Human-readable theme label for the thesis body."""
    return {
        "energy": "Energy",
        "materials": "Materials",
        "defense_geopolitical": "Defense / geopolitical",
        "safe_haven": "Safe-haven",
        "volatility_convexity": "Volatility / convexity",
        "dollar": "Dollar",
        "insurance": "Insurance",
        "emerging_markets": "Emerging markets",
    }.get(theme, theme.replace("_", " ").capitalize())


def _bullet(items: list[str]) -> str:
    return "\n".join(f"  · {item}" for item in items)


# ---- trading events -----------------------------------------------------------------------------


def format_entry(
    *, strategy_name: str, symbol: str, price: float,
    detail: dict[str, Any], is_transition: bool = True, poll_count: int = 1,
    wf_record: Any = None,
) -> tuple[str, str]:
    """Entry alert — sizing story + statistical evidence for the selection.

    ``wf_record`` is the :class:`analysis.universe.WFValidated` row for this symbol (or None if
    it isn't walk-forward validated). When present, the alert body carries the OOS score, WFE,
    OOS return, OOS max drawdown, and OOS trade count — the actual numbers that put this
    strategy+params on the pool. When absent, the body says so plainly rather than pretending
    the position has walk-forward evidence when it doesn't.
    """
    state = "NEW" if is_transition else f"persisting {poll_count} polls"
    sized = int(detail.get("sized", 0))
    title = f"ENTRY  {symbol}  {strategy_name}  ({state})"

    lines: list[str] = []
    lines.append(f"Signal: entry at {price:.4f}. State: {state}.")

    # --- Statistical evidence block — the WHY of the selection.
    if wf_record is not None:
        lines.append("")
        lines.append("Why this trade (walk-forward evidence):")
        wfe = float(getattr(wf_record, "wfe", 0.0))
        oos = float(getattr(wf_record, "oos_score", 0.0))
        oos_ret = float(getattr(wf_record, "oos_return", 0.0))
        oos_dd = float(getattr(wf_record, "oos_max_drawdown", 0.0))
        oos_tr = int(getattr(wf_record, "oos_trades", 0))
        oos_wr = getattr(wf_record, "oos_win_rate", None)
        tier = str(getattr(wf_record, "tier", "?"))
        cls = str(getattr(wf_record, "asset_class", "?"))
        strat_wf = str(getattr(wf_record, "strategy", "?"))
        params = getattr(wf_record, "params", {}) or {}
        param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "defaults"
        wfe_gloss = ("OOS beat in-sample" if wfe > 1.0 else
                       "OOS held ~in-sample" if wfe >= 0.75 else
                       "OOS fell below in-sample but still cleared the gate")
        # Win rate rendered adjacent to OOS trades so the reader sees the count AND its
        # quality on one line. Kept off the tier/strategy/score lines because those speak to
        # gate clearance, and win rate is a hit-rate qualifier, not a gate.
        oos_trade_line = f"OOS trades: {oos_tr}"
        if oos_wr is not None:
            oos_trade_line += f"  ·  win rate: {oos_wr * 100:.1f}% ({int(round(oos_wr * oos_tr))}/{oos_tr})"
        rows = [
            f"Tier: {tier} ({cls}) — cleared WF gate: WFE>=0.5, OOS>0, trades>=class-min",
            f"Strategy: {strat_wf} with {param_str}",
            f"OOS score: {oos:.2f}  ·  WFE: {wfe:.2f}  ({wfe_gloss})",
            f"OOS return: {_pct(oos_ret)}  ·  OOS max drawdown: {_pct(oos_dd)}",
            oos_trade_line,
        ]
        lines.append(_bullet(rows))
    else:
        lines.append("")
        lines.append("Why this trade:")
        lines.append(f"  Symbol is NOT in WALK_FORWARD_VALIDATED. This entry is a live-strategy "
                     f"signal without walk-forward evidence — treat with corresponding caution.")

    # --- Sizing chain — the WHAT of the sized position.
    mitig = detail.get("mitigation") or {}
    interp = detail.get("interpret") or {}
    if mitig or interp or sized:
        lines.append("")
        lines.append("Sizing chain:")
        rows = []
        rows.append(f"Position size: {sized} shares (notional ~${sized * price:,.0f})")
        if mitig:
            cls = mitig.get("class") or "-"
            scal = mitig.get("scalar")
            osint = mitig.get("osint")
            srisk = mitig.get("strategy")
            if osint is not None and osint < 1.0:
                rows.append(f"OSINT overlay ({cls}): x{osint:.3f} — live intel de-risking this class")
            if srisk is not None and srisk < 1.0:
                rows.append(f"Strategy-vol gate: x{srisk:.3f} — recent-return volatility trim")
            if scal is not None and scal < 1.0:
                rows.append(f"Combined mitigation scalar: x{scal:.3f}")
            if mitig.get("halt"):
                rows.append(f"HALT: {detail.get('halt_reason', 'overlay stood the class down')}")
        if interp and interp.get("bias", 1.0) < 1.0:
            theses = ", ".join(interp.get("theses", [])) or "(unnamed)"
            rows.append(f"Interpret bias: x{interp['bias']:.3f} — theses implicating this symbol: {theses}")
        lines.append(_bullet(rows))

    return title, "\n".join(lines)


def format_exit(
    *, strategy_name: str, symbol: str, price: float, shares: int,
) -> tuple[str, str]:
    title = f"EXIT  {symbol}  {strategy_name}"
    body = (f"Signal: exit at {price:.4f}. Closing {shares} shares.\n\n"
            f"Reason: strategy generated an exit signal on the latest bar. "
            f"Realized P&L will land in the session's paper_equity.csv row for this fill.")
    return title, body


def format_hold(
    *, strategy_name: str, symbol: str, price: float, poll_count: int,
) -> tuple[str, str]:
    title = f"HOLD  {symbol}  {strategy_name}"
    body = f"No entry/exit signal. Level-mode poll {poll_count}. Price {price:.4f}."
    return title, body


def format_hedge(*, symbol: str, detail: dict[str, Any]) -> tuple[str, str]:
    """Dynamic hedge rebalance alert."""
    weight = detail.get("weight", 0.0)
    delta = detail.get("delta", 0.0)
    drawdown = detail.get("drawdown", 0.0)
    action = "BUY" if delta > 0 else "SELL"
    title = f"HEDGE  {symbol}  {action}  x{abs(delta):.2f}"
    body = (f"Dollar-hedge overlay rebalance.\n\n"
            f"Target weight: {_pct(weight)}  ·  drawdown: {_pct(drawdown)}\n"
            f"Action: {action} {abs(delta):.2f} units of the hedge sleeve.")
    return title, body


# ---- intel events -------------------------------------------------------------------------------


def format_thesis(thesis: Any, *, theme_exemplars: dict[str, tuple[str, ...]] | None = None,
                    graph_context: dict[str, Any] | None = None) -> tuple[str, str]:
    """Thesis-fire alert — the interpretive story with exposures called out.

    ``thesis`` is a :class:`intel.interpret.Thesis` (duck-typed, so this stays testable without
    importing interpret at module load). ``theme_exemplars`` maps a theme to its exemplar tickers,
    used to render the "implicated tickers" block. ``graph_context`` is an optional dict for
    corroboration/persistence gloss (source count, consecutive-polls count).
    """
    title = f"THESIS  {thesis.name}  ({thesis.confidence})"
    lines: list[str] = []
    if thesis.evidence:
        lines.append("What the feed shows:")
        lines.append(_bullet(list(thesis.evidence)))
        lines.append("")
    if thesis.inference:
        lines.append("What it means:")
        lines.append(f"  {thesis.inference}")
        lines.append("")
    if thesis.action:
        lines.append("What to do:")
        lines.append(f"  {thesis.action}")
        lines.append("")
    if thesis.themes and theme_exemplars:
        lines.append("Implicated exposures:")
        rows: list[str] = []
        for theme in thesis.themes:
            tickers = theme_exemplars.get(theme, ())
            if tickers:
                rows.append(f"{_theme_label(theme)}: {', '.join(tickers)}")
        if rows:
            lines.append(_bullet(rows))
            lines.append("")
    if graph_context:
        lines.append("Graph context:")
        rows = []
        if "consecutive_polls" in graph_context:
            rows.append(f"Thesis-fire streak: {graph_context['consecutive_polls']} consecutive polls")
        if "source_count" in graph_context:
            rows.append(f"Distinct sources corroborating: {graph_context['source_count']}")
        if rows:
            lines.append(_bullet(rows))
    return title, "\n".join(lines).rstrip()


def format_persistence(
    *, domain: str, run_length: int, threshold: int,
    class_scalars: dict[str, float] | None = None,
) -> tuple[str, str]:
    """Persistence-hit alert — a regime-detected story with class-scalar impact."""
    title = f"PERSISTENCE  {domain}  elevated {run_length} polls"
    lines: list[str] = []
    lines.append(f"Signal: 'elevated_in' for the '{domain}' domain has held for "
                  f"{run_length} consecutive polls (threshold {threshold}).")
    lines.append("")
    lines.append("What it means:")
    lines.append("  A single high acceleration ratio is noise. This many consecutive polls at the "
                  "same level is a regime — the graph journal is treating this as an active "
                  "condition rather than a spike.")
    if class_scalars:
        lines.append("")
        lines.append("Current class scalars (multiplicative sizing gate):")
        rows = [f"{cls}: {scal:.3f}" for cls, scal in sorted(class_scalars.items())]
        lines.append(_bullet(rows))
    return title, "\n".join(lines)


def format_wash(*, before: int, after: int, pruned: int,
                 per_predicate: dict[str, int] | None = None) -> tuple[str, str]:
    pct = (pruned / before * 100.0) if before else 0.0
    title = f"WASH  {pruned} edges pruned ({pct:.1f}%)"
    lines: list[str] = []
    lines.append(f"Temporal-gate wash swept {before:,} -> {after:,} edges in "
                  f"state/intel_graph.jsonl.")
    lines.append("Backup at state/intel_graph.jsonl.bak (last wash undo-able).")
    if per_predicate:
        lines.append("")
        lines.append("Per-predicate pruning:")
        rows = [f"{pred}: -{n}" for pred, n in sorted(per_predicate.items(), key=lambda kv: -kv[1])]
        lines.append(_bullet(rows))
    lines.append("")
    lines.append("Next wash: at least 72h from now (--wash-min-hours default).")
    return title, "\n".join(lines)
