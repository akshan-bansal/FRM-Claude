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
    # 14 robust + 3 watch after wiring the widened-search survivors, CEW.TO, BTO.TO and ZEB.TO.
    assert len(validated_symbols("robust")) == 14
    assert len(WALK_FORWARD_VALIDATED) == 17
    watch = set(validated_symbols("watch"))
    assert {"VFV.TO", "WCP.TO", "KEY.TO"} == watch


def test_widened_search_robust_names_are_wired() -> None:
    """The 6 walk-forward survivors from the ~180-asset search are present and robust."""
    for sym in ("XLE", "XLB", "QQQ", "IWM", "RS", "DBA"):
        rec = validated_for(sym)
        assert rec is not None, sym
        assert rec.tier == "robust", sym
    # XLE (energy) and XLB (materials) are the standouts — OOS beats in-sample (WFE > 1).
    assert validated_for("XLE").wfe > 1.0
    assert validated_for("XLB").wfe > 1.0


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


def test_zeb_is_wired_robust_on_atr_channel() -> None:
    """ZEB.TO (bank-sector ETF) is the first atr_channel name; OOS beat in-sample (WFE > 1)."""
    rec = validated_for("ZEB.TO")
    assert rec is not None and rec.tier == "robust"
    assert rec.strategy == "atr_channel"
    assert rec.wfe > 1.0
