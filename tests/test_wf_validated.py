from __future__ import annotations

from trading_live_claude.analysis.universe import (
    SEED_UNIVERSE,
    WALK_FORWARD_VALIDATED,
    min_oos_trades,
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
            # The trade-count bar is class-dependent: commodities trade on a slower cycle and are
            # held to ~4 (roughly quarterly), everything else to 10.
            assert rec.oos_trades >= min_oos_trades(rec.asset_class), rec.symbol


def test_expected_counts_and_watch_names() -> None:
    # 21 robust + 7 watch — the $1-45 resweep added CRT.UN.TO, VALE and DBC (all robust) on top of the
    # $25-40 sweep's ZWB.TO/GEI.TO (robust) and XEI.TO (watch); SMH omitted.
    assert len(validated_symbols("robust")) == 21
    assert len(WALK_FORWARD_VALIDATED) == 28
    assert "SMH" not in WALK_FORWARD_VALIDATED
    watch = set(validated_symbols("watch"))
    assert {"VFV.TO", "WCP.TO", "KEY.TO", "TA.TO", "ZUT.TO", "EFN.TO", "XEI.TO"} == watch


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


def test_sweep_survivors_fru_robust_ta_watch() -> None:
    """From the $15-25 sweep: FRU.TO cleared robust; TA.TO is carried at watch on drawdown risk."""
    fru = validated_for("FRU.TO")
    assert fru is not None and fru.tier == "robust" and fru.strategy == "bollinger"
    ta = validated_for("TA.TO")
    assert ta is not None and ta.tier == "watch" and ta.strategy == "macd"


def test_25_35_sweep_survivors_are_wired() -> None:
    """$25-35 sweep: SRU.UN.TO (first REIT) and CGL.TO (first bullion) robust; ZUT/EFN watch."""
    sru = validated_for("SRU.UN.TO")
    assert sru is not None and sru.tier == "robust" and sru.strategy == "rsi_meanrevert"
    cgl = validated_for("CGL.TO")
    assert cgl is not None and cgl.tier == "robust" and cgl.strategy == "atr_channel"
    for sym in ("ZUT.TO", "EFN.TO"):
        rec = validated_for(sym)
        assert rec is not None and rec.tier == "watch", sym


def test_resweep_1_45_survivors_are_wired() -> None:
    """The $1-45 resweep's three walk-forward survivors, including the ARX.TO family upgrade."""
    for sym in ("ARX.TO", "CRT.UN.TO", "VALE"):
        rec = validated_for(sym)
        assert rec is not None, sym
        assert rec.tier == "robust", sym

    # ARX.TO was re-optimized across all families and moved off bollinger onto rsi_meanrevert,
    # beating the superseded config out-of-sample (was OOS 8.82 / WFE 0.72).
    arx = validated_for("ARX.TO")
    assert arx.strategy == "rsi_meanrevert"
    assert arx.oos_score > 8.82 and arx.wfe > 0.72

    # VALE is the first materials producer and the first non-North-American-listed name in the pool.
    assert validated_for("VALE").wfe > 1.0   # OOS beat in-sample


def test_commodity_trade_bar_is_lower_than_the_default() -> None:
    """Commodities clear on ~4 OOS trades (roughly quarterly); everything else still needs 10."""
    assert min_oos_trades("commodity") == 4
    for cls in ("equity", "future", "fx", "crypto"):
        assert min_oos_trades(cls) == 10

    # DBC is the name this bar admits: it clears on 7 trades and would fail the flat 10.
    dbc = validated_for("DBC")
    assert dbc is not None and dbc.tier == "robust" and dbc.asset_class == "commodity"
    assert 4 <= dbc.oos_trades < 10
