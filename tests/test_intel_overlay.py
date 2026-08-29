from __future__ import annotations

from trading_live_claude.intel import (
    IntelSnapshot,
    OverlayConfig,
    OverlayProvider,
    RiskOverlay,
    apply_overlay,
    classify_symbol,
)
from trading_live_claude.intel.worldmonitor import WorldMonitorClient, _build_snapshot
from trading_live_claude.portfolio.allocator import AllocationResult


def test_calm_world_leaves_full_exposure() -> None:
    dec = RiskOverlay().evaluate(IntelSnapshot())
    assert set(dec) == {"equity", "future", "commodity", "fx", "crypto"}
    for d in dec.values():
        assert d.scalar == 1.0 and not d.halt_new_entries and d.reasons == []


def test_overlay_only_ever_reduces() -> None:
    stressed = IntelSnapshot(global_alert_count=9, conflict_events_active=5,
                             energy_stress=0.6, market={"equity_vol": 30.0})
    for d in RiskOverlay().evaluate(stressed).values():
        assert 0.0 < d.scalar <= 1.0


def test_more_alerts_monotonically_lower_scalar() -> None:
    ov = RiskOverlay()
    light = ov.evaluate(IntelSnapshot(global_alert_count=3))["equity"].scalar
    heavy = ov.evaluate(IntelSnapshot(global_alert_count=10))["equity"].scalar
    assert heavy < light < 1.0


def test_crypto_has_higher_beta_to_global_risk_off() -> None:
    # a world with ONLY global alerts isolates the beta: crypto raises the global gate to a power.
    dec = RiskOverlay().evaluate(IntelSnapshot(global_alert_count=6))
    assert dec["crypto"].scalar < dec["equity"].scalar


def test_severe_world_halts_new_entries() -> None:
    severe = IntelSnapshot(global_alert_count=12, conflict_events_active=8,
                           category_alert_counts={"economy": 6}, market={"equity_vol": 40.0})
    dec = RiskOverlay().evaluate(severe)
    assert dec["equity"].halt_new_entries and dec["crypto"].halt_new_entries
    assert dec["equity"].scalar <= OverlayConfig().halt_below


def test_commodity_reads_energy_stress() -> None:
    dec = RiskOverlay().evaluate(IntelSnapshot(energy_stress=1.0))
    assert dec["commodity"].scalar < dec["fx"].scalar   # fx ignores energy
    assert any("energy" in r for r in dec["commodity"].reasons)


def test_degraded_feed_caps_conservatively() -> None:
    # otherwise-calm world, but the fetch was incomplete -> capped, and flagged.
    dec = RiskOverlay().evaluate(IntelSnapshot(degraded=True))
    d = dec["equity"]
    assert d.scalar == OverlayConfig().degraded_cap
    assert any("degraded" in r for r in d.reasons)


def test_apply_overlay_only_de_risks_and_frees_cash() -> None:
    alloc = AllocationResult(weights={"AAPL": 0.4, "BTC": 0.4}, gross_exposure=0.8, cash=0.2,
                             sleeve_weights={"default": 0.8}, effective_positions=2.0)
    dec = RiskOverlay().evaluate(IntelSnapshot(global_alert_count=12,
                                               market={"equity_vol": 40.0, "crypto_chg": 9.0}))
    out = apply_overlay(alloc, {"AAPL": "equity", "BTC": "crypto"}, dec)
    assert out.weights["AAPL"] < 0.4 and out.weights["BTC"] < 0.4   # both scaled down
    assert out.gross_exposure < alloc.gross_exposure                # book shrank
    assert out.cash > alloc.cash                                    # freed weight is cash
    assert abs(out.gross_exposure + out.cash - 1.0) < 1e-9


def test_client_decode_handles_sse_frames() -> None:
    import httpx
    sse = "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":3,\"result\":{\"ok\":true}}\n\n"
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    assert WorldMonitorClient._decode(resp) == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}


def test_build_snapshot_extracts_features_from_real_payloads() -> None:
    # shapes mirror the live WorldMonitor tools: content nested under `data`, indices calibrated.
    news = {"data": {
        "insights": {"topStories": [{"upstreamImportanceScore": 40.0}, {"upstreamImportanceScore": 12.0}]},
        "cross-source-signals": {"signals": [
            {"severityScore": 9}, {"severityScore": 7}, {"severityScore": 3}]},
        "advisories-bootstrap": {"advisories": [{"country": "AE"}, {"country": "US"}]},
    }}
    conflict = {"data": {"scores": {"strategicRisks": {"sample": [
        {"region": "global", "score": 72}]}}}}
    disasters = {"data": {"earthquakes": 40, "fires": 8, "events": 5}}
    energy = {"data": {"fuel-shortages": {"shortages": {"count": 30}}}}
    market = {"data": {
        "fear-greed": {"composite": {"score": 34.0}},
        "commodities-bootstrap": {"quotes": [{"symbol": "^VIX", "price": 27.0, "change": -0.5}]},
    }}
    snap = _build_snapshot(news, conflict, disasters, energy, market, (), degraded=False)
    assert snap.global_alert_count == 2          # two signals with severity >= 7
    assert snap.conflict_events_active == 1       # one critical (severity >= 8)
    assert abs(snap.global_max_importance - 0.40) < 1e-9   # 40/100 normalized
    assert snap.strategic_risk == 72.0
    assert snap.fear_greed == 34.0
    assert abs(snap.energy_stress - 0.30) < 1e-9  # 30/100, capped at 0.4
    assert snap.market["equity_vol"] == 27.0
    assert snap.country_alert_counts == {"AE": 1, "US": 1}


def test_classify_symbol_routes_to_overlay_classes() -> None:
    assert classify_symbol("XBT/USD") == "crypto"
    assert classify_symbol("BTC-USD") == "crypto"
    assert classify_symbol("USDCAD") == "fx"
    assert classify_symbol("CGL.TO") == "commodity"
    assert classify_symbol("/ES") == "future"
    assert classify_symbol("XIC.TO") == "equity"   # the common default
    assert classify_symbol("XIC.TO", {"XIC.TO": "commodity"}) == "commodity"  # override wins


def test_overlay_provider_caches_and_routes_by_class() -> None:
    calls = {"n": 0}
    stressed = IntelSnapshot(global_alert_count=12, market={"crypto_chg": 9.0})

    def _snap() -> IntelSnapshot:
        calls["n"] += 1
        return stressed

    prov = OverlayProvider(_snap, refresh_seconds=1000.0)
    d_crypto = prov("BTC-USD")
    d_equity = prov("AAPL")
    assert d_crypto is not None and d_equity is not None
    assert d_crypto.asset_class == "crypto" and d_equity.asset_class == "equity"
    assert calls["n"] == 1   # second lookup reused the cached snapshot


def test_overlay_provider_is_fail_safe() -> None:
    # never a good read -> returns None (monitor behaves as if no overlay configured)
    def _boom() -> IntelSnapshot:
        raise RuntimeError("network down")

    assert OverlayProvider(_boom, refresh_seconds=0.0)("AAPL") is None

    # one good read then failures -> keeps the last good decisions
    state = {"ok": True}

    def _flaky() -> IntelSnapshot:
        if state["ok"]:
            return IntelSnapshot(global_alert_count=12)
        raise RuntimeError("later failure")

    prov = OverlayProvider(_flaky, refresh_seconds=0.0)  # always attempts a refresh
    first = prov("AAPL")
    state["ok"] = False
    second = prov("AAPL")
    assert first is not None and second is not None and second.scalar == first.scalar
