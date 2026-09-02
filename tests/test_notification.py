"""Tests for intel/notification.py — the shared push-notification formatter surface."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_live_claude.intel.notification import (
    format_entry,
    format_exit,
    format_hedge,
    format_persistence,
    format_thesis,
    format_wash,
)


@dataclass
class _StubWF:
    """Duck-typed stand-in for analysis.universe.WFValidated."""
    tier: str = "robust"
    asset_class: str = "equity"
    strategy: str = "ts_momentum"
    params: dict[str, Any] = None                                    # type: ignore[assignment]
    oos_score: float = 8.17
    wfe: float = 1.36
    oos_return: float = 0.234
    oos_max_drawdown: float = -0.081
    oos_trades: int = 15
    oos_win_rate: float | None = None


@dataclass
class _StubThesis:
    name: str
    confidence: str
    evidence: list[str]
    inference: str
    action: str
    themes: list[str]


# ---- entry -------------------------------------------------------------------------------------


def test_entry_alert_carries_wf_statistics_in_a_dedicated_block() -> None:
    """The 'Why this trade' block must name tier, class, strategy, OOS/WFE/return/DD/trades."""
    wf = _StubWF(params={"lookback": 127, "threshold": 0.02})
    title, body = format_entry(
        strategy_name="ts_momentum", symbol="EQB.TO", price=127.79,
        detail={"sized": 65, "mitigation": {"scalar": 0.5187, "osint": 0.5187,
                                              "strategy": 1.0, "halt": False, "class": "equity"}},
        wf_record=wf,
    )
    assert "ENTRY" in title and "EQB.TO" in title and "ts_momentum" in title
    assert "Why this trade" in body
    assert "robust" in body and "equity" in body
    assert "ts_momentum" in body and "lookback=127" in body
    assert "8.17" in body                        # OOS score
    assert "1.36" in body                        # WFE
    assert "+23.40%" in body                     # OOS return (formatted %)
    assert "-8.10%" in body                      # OOS max drawdown
    assert "OOS trades: 15" in body


def test_entry_alert_shows_win_rate_adjacent_to_oos_trades_when_present() -> None:
    """Win rate is rendered on the same line as the OOS trade count."""
    wf = _StubWF(oos_trades=20, oos_win_rate=0.6, params={"lookback": 63})
    _, body = format_entry(
        strategy_name="ts_momentum", symbol="EQB.TO", price=100.0,
        detail={"sized": 50}, wf_record=wf,
    )
    # Two-column layout on one line: OOS trades AND win rate together.
    line = next(l for l in body.splitlines() if "OOS trades: 20" in l)
    assert "60.0%" in line                          # rendered percentage
    assert "12/20" in line                          # 0.6 * 20 = 12 wins


def test_entry_alert_omits_win_rate_line_when_absent() -> None:
    """A pre-existing entry with no oos_win_rate must not fabricate a 0% line."""
    wf = _StubWF(oos_win_rate=None, oos_trades=15)
    _, body = format_entry(
        strategy_name="ts_momentum", symbol="EQB.TO", price=100.0,
        detail={"sized": 60}, wf_record=wf,
    )
    line = next(l for l in body.splitlines() if "OOS trades:" in l)
    assert "OOS trades: 15" in line
    # Explicitly assert the win-rate qualifier is NOT there — honest omission, not zero.
    assert "win rate" not in line


def test_entry_alert_wfe_gloss_is_class_specific() -> None:
    """WFE > 1 gets 'OOS beat in-sample'; ~1 gets 'held ~in-sample'; <0.75 gets 'fell below'."""
    def _msg(wfe: float) -> str:
        return format_entry(strategy_name="s", symbol="X", price=1.0,
                             detail={"sized": 1}, wf_record=_StubWF(wfe=wfe))[1]
    assert "OOS beat in-sample" in _msg(1.5)
    assert "OOS held ~in-sample" in _msg(0.8)
    assert "OOS fell below in-sample" in _msg(0.6)


def test_entry_alert_disclaims_when_symbol_is_not_wf_validated() -> None:
    """Honesty: an entry on a non-validated symbol must NOT pretend to have WF evidence."""
    title, body = format_entry(
        strategy_name="bollinger", symbol="NOTPOOL", price=42.0,
        detail={"sized": 10},
        wf_record=None,
    )
    assert "NOT in WALK_FORWARD_VALIDATED" in body
    assert "walk-forward evidence" in body.lower()


def test_entry_alert_sizing_chain_shows_the_gates_that_actually_bit() -> None:
    """Only trims that actually fired belong in the block — a scalar of 1.0 must not appear."""
    _, body = format_entry(
        strategy_name="ts_momentum", symbol="EQB.TO", price=100.0,
        detail={
            "sized": 60,
            "mitigation": {"scalar": 0.5, "osint": 0.5, "strategy": 1.0,
                             "halt": False, "class": "equity"},
            "interpret": {"bias": 0.75, "theses": ["Complacency divergence"]},
        },
        wf_record=None,
    )
    assert "OSINT overlay (equity): x0.500" in body
    assert "Interpret bias: x0.750" in body
    assert "Complacency divergence" in body
    # strategy scalar was 1.0 — must NOT be rendered as a gate that bit
    assert "Strategy-vol gate" not in body


def test_entry_alert_halt_reason_is_surfaced() -> None:
    _, body = format_entry(
        strategy_name="ts_momentum", symbol="EQB.TO", price=100.0,
        detail={
            "sized": 0,
            "mitigation": {"scalar": 0.2, "osint": 0.2, "strategy": 1.0,
                             "halt": True, "class": "equity"},
            "halt_reason": "severe geopolitical strategic-risk index 92",
        },
        wf_record=None,
    )
    assert "HALT" in body
    assert "geopolitical" in body


# ---- exit / hedge --------------------------------------------------------------------------------


def test_exit_alert_names_symbol_price_and_share_count() -> None:
    title, body = format_exit(strategy_name="ts_momentum", symbol="EQB.TO",
                                price=131.20, shares=65)
    assert "EXIT" in title and "EQB.TO" in title
    assert "131.2" in body and "65" in body


def test_hedge_alert_calls_out_direction_and_drawdown() -> None:
    title, body = format_hedge(symbol="UUP",
                                 detail={"weight": 0.08, "delta": 0.5, "drawdown": -0.12})
    assert "HEDGE" in title and "UUP" in title and "BUY" in title
    assert "-12.00%" in body           # drawdown as %
    assert "+8.00%" in body            # weight as %


# ---- intel events --------------------------------------------------------------------------------


def test_thesis_alert_renders_evidence_inference_action_and_theme_tickers() -> None:
    thesis = _StubThesis(
        name="Complacency divergence", confidence="moderate",
        evidence=["geo-risk 70/100", "VIX 14 (low)", "energy accel 3.1x"],
        inference="Exogenous risk is building while the market prices calm.",
        action="Favour cheap convexity over outright de-risking.",
        themes=["volatility_convexity", "safe_haven"],
    )
    exemplars = {"volatility_convexity": ("VIXY", "UVXY"),
                 "safe_haven": ("GLD", "CGL.TO")}
    title, body = format_thesis(thesis, theme_exemplars=exemplars)
    assert "THESIS" in title and "Complacency divergence" in title and "moderate" in title
    for evidence_line in thesis.evidence:
        assert evidence_line in body
    assert "Exogenous risk" in body
    assert "cheap convexity" in body
    assert "VIXY" in body and "CGL.TO" in body
    assert "Safe-haven" in body                 # human-readable theme label


def test_thesis_alert_omits_empty_sections_cleanly() -> None:
    """A thesis with no themes / no exemplars must not render an empty block."""
    bare = _StubThesis(name="Bare", confidence="tentative", evidence=[],
                        inference="", action="", themes=[])
    _, body = format_thesis(bare, theme_exemplars={})
    assert "Implicated exposures" not in body
    assert "What the feed shows" not in body


def test_persistence_alert_renders_domain_run_and_regime_gloss() -> None:
    title, body = format_persistence(domain="energy", run_length=6, threshold=5)
    assert "PERSISTENCE" in title and "energy" in title and "6 polls" in title
    assert "regime" in body.lower()
    assert "consecutive polls" in body


def test_persistence_alert_optionally_includes_class_scalars() -> None:
    _, body = format_persistence(
        domain="energy", run_length=6, threshold=5,
        class_scalars={"commodity": 0.5, "equity": 0.7},
    )
    assert "commodity: 0.500" in body
    assert "equity: 0.700" in body


def test_wash_alert_reports_before_after_and_percentage() -> None:
    title, body = format_wash(before=1000, after=950, pruned=50)
    assert "WASH" in title
    assert "5.0%" in title
    assert "1,000" in body and "950" in body
    assert "undo-able" in body
    assert "72h" in body                        # next-wash cadence hint
