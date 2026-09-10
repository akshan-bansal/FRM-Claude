from __future__ import annotations

import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_live_claude.brokers.models import OrderAction, Quote
from trading_live_claude.execution.approval import (
    ApprovalRouter,
    CardRegistry,
    InMemoryApprovalStore,
)
from trading_live_claude.execution.router import OrderIntent, Router
from trading_live_claude.intel.overlay import IntelSnapshot
from trading_live_claude.intel.vs_engine import (
    MarketContext,
    VSInvestmentEngine,
)


# --------------------------------------------------------------------------- #
# stubs                                                                       #
# --------------------------------------------------------------------------- #

class _StubBroker:
    name = "ib"

    def accounts(self): return []
    def positions(self, _): return []
    def quote(self, s): return Quote(symbol=s, symbolId=1, bidPrice=99.5, askPrice=100.5, lastTradePrice=100.0)
    def quotes(self, ss): return [self.quote(s) for s in ss]
    def candles(self, *a, **k): return []
    def equity(self, _): return 100_000.0
    def place_order(self, o): o.id = 1; return o
    def cancel_order(self, *_, **__): pass


def _intent() -> OrderIntent:
    return OrderIntent(
        symbol="XIC.TO", action=OrderAction.BUY, shares=12, entry=31.05,
        stop=30.40, target=32.10, strategy="momentum_breakout",
        risk_dollars=7.80, account_number="paper-001", symbolId=1,
    )


# --------------------------------------------------------------------------- #
# engine tests                                                                #
# --------------------------------------------------------------------------- #

def test_explain_returns_thesis_and_persists_writeup(tmp_path: Path):
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    intent = _intent()
    market = MarketContext(strategy_rank=3, universe_size=42, r_multiple=1.6,
                           trend_slope=0.35, rsi_14=58.0, atr_pct=0.012)

    thesis, ref = engine.explain(intent, broker="ib", market=market)
    assert thesis
    assert len(thesis) <= 140
    assert ref.startswith("vs_")
    assert (tmp_path / f"{ref}.json").exists()

    w = engine.load(ref)
    assert w is not None
    assert w.symbol == "XIC.TO"
    assert w.broker == "ib"
    assert "rank 3/42" in "; ".join(w.reason_clauses)


def test_overlay_risk_survives_truncation_over_notes(tmp_path: Path):
    """Risk signals must not be dropped in favor of user-supplied notes."""
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    market = MarketContext(
        strategy_rank=3, universe_size=42, r_multiple=1.6,
        trend_slope=0.35, rsi_14=58, atr_pct=0.012,
        notes=("BoC on pause", "TSX cyclicals lagging", "extra padding note"),
    )
    snap = IntelSnapshot(strategic_risk=78.0, energy_stress=0.65)
    thesis, ref = engine.explain(_intent(), broker="ib",
                                 market=market, overlay_snapshot=snap)
    assert len(thesis) <= 140
    # The overlay clause must be present even after truncation.
    assert "geo-risk 78" in thesis or "energy stress high" in thesis, thesis


def test_thesis_truncates_at_140(tmp_path: Path):
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    market = MarketContext(
        strategy_rank=1, universe_size=100, r_multiple=2.5,
        trend_slope=1.5, rsi_14=25, atr_pct=0.04, days_since_signal=3,
        notes=("earnings beat", "guidance raised", "sector rotating in", "insider buying"),
    )
    thesis, _ = engine.explain(_intent(), broker="ib", market=market)
    assert len(thesis) <= 140


def test_overlay_appends_geo_clause_when_stressed(tmp_path: Path):
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    snap = IntelSnapshot(strategic_risk=82.0, energy_stress=0.7,
                         fear_greed=15.0, degraded=False)
    thesis, ref = engine.explain(_intent(), broker="ib", overlay_snapshot=snap)
    assert "geo-risk 82" in thesis
    assert "energy stress high" in thesis
    assert "F&G 15" in thesis
    w = engine.load(ref)
    assert w.overlay_snapshot["strategic_risk"] == 82.0
    assert w.warnings == []


def test_overlay_degraded_records_warning(tmp_path: Path):
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    snap = IntelSnapshot(degraded=True)
    _, ref = engine.explain(_intent(), broker="kraken", overlay_snapshot=snap)
    w = engine.load(ref)
    assert any("degraded" in x for x in w.warnings)


def test_broker_selects_asset_class(tmp_path: Path):
    engine = VSInvestmentEngine(writeup_dir=tmp_path)
    # No overlay snapshot -> no overlay clause at all; smoke test the
    # broker->asset-class mapping is at least documented in the writeup.
    _, ref_ib = engine.explain(_intent(), broker="ib")
    _, ref_kr = engine.explain(_intent(), broker="kraken")
    assert engine.load(ref_ib).broker == "ib"
    assert engine.load(ref_kr).broker == "kraken"
    # Different intel_refs for different brokers
    assert ref_ib != ref_kr


# --------------------------------------------------------------------------- #
# ApprovalRouter integration                                                  #
# --------------------------------------------------------------------------- #

def test_approval_router_uses_thesis_fn(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    registry = CardRegistry()
    registry.register("c1", pem)
    store = InMemoryApprovalStore(registry)

    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)
    engine = VSInvestmentEngine(writeup_dir=tmp_path / "writeups")

    def _thesis(intent, broker):
        return engine.explain(
            intent, broker=broker,
            market=MarketContext(strategy_rank=1, universe_size=10, r_multiple=1.8),
        )

    approval = ApprovalRouter(inner, store=store, ttl_seconds=5, thesis_fn=_thesis)

    holder: dict = {}
    t = threading.Thread(
        target=lambda: holder.__setitem__(
            "order",
            approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0),
        )
    )
    t.start()
    for _ in range(200):
        pending = store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    assert pending
    prompt = pending[0]
    assert prompt.broker == "ib"
    assert prompt.thesis
    assert prompt.intel_ref.startswith("vs_")

    sig = key.sign(prompt.canonical.encode("utf-8"))
    store.respond(prompt.intent_id, decision="ACCEPT", card_id="c1", signature=sig)
    t.join(timeout=2)
    assert holder["order"] is not None


def test_from_signal_row_lifts_optional_columns():
    import pandas as pd
    row = pd.Series({
        "entry": 1, "exit": 0,
        "score": 1.42, "rank": 3, "r_multiple": 1.6,
        "atr_pct": 0.011, "trend_slope": 0.35, "rsi_14": 58, "days_since_signal": 0,
    })
    mc = MarketContext.from_signal_row(row, universe_size=42, notes=("BoC on pause",))
    assert mc.strategy_score == pytest.approx(1.42)
    assert mc.strategy_rank == 3
    assert mc.universe_size == 42
    assert mc.r_multiple == pytest.approx(1.6)
    assert mc.atr_pct == pytest.approx(0.011)
    assert mc.rsi_14 == pytest.approx(58.0)
    assert mc.notes == ("BoC on pause",)


def test_from_signal_row_skips_missing_and_nan():
    import numpy as np
    import pandas as pd
    row = pd.Series({"entry": 1, "exit": 0, "score": np.nan, "r_multiple": 2.0})
    mc = MarketContext.from_signal_row(row)
    assert mc.strategy_score is None      # NaN skipped
    assert mc.strategy_rank is None       # missing skipped
    assert mc.r_multiple == pytest.approx(2.0)


def test_from_signal_row_accepts_plain_dict():
    row = {"score": 0.5, "rank": 7}
    mc = MarketContext.from_signal_row(row, universe_size=20)
    assert mc.strategy_score == 0.5
    assert mc.strategy_rank == 7
    assert mc.universe_size == 20


def test_approval_router_survives_thesis_fn_error(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    registry = CardRegistry()
    registry.register("c1", pem)
    store = InMemoryApprovalStore(registry)
    inner = Router.build_default(mode="paper", broker=_StubBroker(), state_dir=tmp_path)

    def _boom(intent, broker):
        raise RuntimeError("engine offline")

    approval = ApprovalRouter(inner, store=store, ttl_seconds=5, thesis_fn=_boom)

    t = threading.Thread(
        target=lambda: approval.submit(_intent(), equity=100_000, existing_risk=0, open_positions=0)
    )
    t.start()
    for _ in range(200):
        pending = store.pending()
        if pending:
            break
        threading.Event().wait(0.01)
    prompt = pending[0]
    assert prompt.thesis == ""     # narrator failed silently
    assert prompt.intel_ref == ""
    sig = key.sign(prompt.canonical.encode("utf-8"))
    store.respond(prompt.intent_id, decision="DECLINE", card_id="c1", signature=sig)
    t.join(timeout=2)
