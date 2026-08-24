from __future__ import annotations

from trading_live_claude.analysis.universe import (
    SEED_UNIVERSE,
    WALK_FORWARD_VALIDATED,
    validated_for,
    validated_symbols,
)
from trading_live_claude.strategies import STRATEGIES


def test_records_are_well_formed() -> None:
    for sym, rec in WALK_FORWARD_VALIDATED.items():
        assert rec.symbol == sym
        assert rec.tier in {"robust", "watch"}
        assert rec.strategy in STRATEGIES, rec.strategy
        assert rec.params, f"{sym} has empty params"
        assert rec.oos_trades >= 1
        assert -1.0 <= rec.oos_max_drawdown <= 0.0


def test_robust_tier_meets_all_three_bars() -> None:
    """Robust = WFE>=0.5 AND positive OOS score AND >=10 out-of-sample trades."""
    for rec in WALK_FORWARD_VALIDATED.values():
        if rec.tier == "robust":
            assert rec.wfe >= 0.5, rec.symbol
            assert rec.oos_score > 0.0, rec.symbol
            assert rec.oos_trades >= 10, rec.symbol


def test_expected_counts_and_watch_names() -> None:
    # Pool ordered by OOS score and truncated at KEY.TO: 6 robust + 3 watch.
    assert len(validated_symbols("robust")) == 6
    assert len(WALK_FORWARD_VALIDATED) == 9
    watch = set(validated_symbols("watch"))
    assert {"VFV.TO", "WCP.TO", "KEY.TO"} == watch


def test_truncated_at_key_to() -> None:
    """KEY.TO is the lowest-scoring wired name; nothing below its OOS score is kept."""
    key = validated_for("KEY.TO")
    assert key is not None
    assert min(v.oos_score for v in WALK_FORWARD_VALIDATED.values()) == key.oos_score
    # Names dropped by the truncation must be gone.
    for dropped in ("HBM.TO", "AVGO", "NVDA", "AZO", "AMD", "META", "EFN.TO", "LNR.TO"):
        assert dropped not in WALK_FORWARD_VALIDATED


def test_all_validated_symbols_are_in_the_equity_seed() -> None:
    seed = set(SEED_UNIVERSE["equity"])
    for sym in validated_symbols():
        assert sym in seed, f"{sym} missing from SEED_UNIVERSE['equity']"


def test_validated_for_lookup() -> None:
    assert validated_for("XIC.TO") is not None
    assert validated_for("XIC.TO").strategy == "rsi_meanrevert"
    assert validated_for("NOT_A_TICKER") is None


def test_confirm_bollinger_watch_name_uses_the_overlay() -> None:
    assert validated_for("WCP.TO").strategy == "confirm_bollinger"
