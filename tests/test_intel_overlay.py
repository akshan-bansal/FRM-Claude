from __future__ import annotations

from dataclasses import replace

from trading_live_claude.intel import (
    IntelSnapshot,
    OverlayConfig,
    OverlayProvider,
    RiskOverlay,
    apply_overlay,
    classify_symbol,
)
from trading_live_claude.intel.worldmonitor import (
    MAX_SOURCE_AGE_H,
    WorldMonitorClient,
    _build_snapshot,
)
from trading_live_claude.portfolio.allocator import AllocationResult


def test_calm_world_leaves_full_exposure() -> None:
    dec = RiskOverlay().evaluate(IntelSnapshot())
    assert set(dec) == {"equity", "future", "commodity", "fx", "crypto",
                        "fixed_income", "precious_metals"}
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


def test_severe_world_bottoms_out_at_the_floor() -> None:
    """A severe world drives every class to the floor — the deepest de-risk available."""
    severe = IntelSnapshot(global_alert_count=12, conflict_events_active=8,
                           category_alert_counts={"economy": 6}, market={"equity_vol": 40.0})
    dec = RiskOverlay().evaluate(severe)
    cfg = OverlayConfig()
    assert dec["equity"].scalar == cfg.floor
    assert dec["crypto"].scalar == cfg.floor


def test_halt_threshold_below_the_floor_disables_halting() -> None:
    """Documents a live configuration consequence, so it can never surprise anyone.

    A class scalar is clamped to ``>= floor``. With ``halt_below`` (0.20) at or under ``floor``
    (0.25), ``scalar <= halt_below`` is unreachable, so the overlay trims but never stands a class
    down. Halting is currently OFF by configuration; drop the floor below halt_below to re-enable it.
    """
    cfg = OverlayConfig()
    assert cfg.halt_below <= cfg.floor, "halt is only reachable when halt_below > floor"

    worst = IntelSnapshot(global_alert_count=30, conflict_events_active=20, strategic_risk=100.0,
                          energy_stress=1.0, fear_greed=0.0, natural_disasters_active=20,
                          category_alert_counts={"economy": 20},
                          market={"equity_vol": 80.0, "crypto_chg": 30.0},
                          event_acceleration={"energy": 10.0, "conflict": 10.0})
    dec = RiskOverlay().evaluate(worst)
    # Risk-off classes (equity, future, commodity, fx, crypto) trim all the way to the floor on a
    # worst-case snapshot. Safe-haven classes (fixed_income, precious_metals) deliberately use
    # dilution weights on the same risk gates because a global squeeze is a WEAKER de-risking
    # signal for duration and gold than it is for stocks — the whole point of splitting them out.
    _RISK_OFF = ("equity", "future", "commodity", "fx", "crypto")
    _SAFE_HAVEN = ("fixed_income", "precious_metals")
    for c in _RISK_OFF:
        assert dec[c].scalar == cfg.floor, f"{c} must floor on worst-case, got {dec[c].scalar}"
    for c in _SAFE_HAVEN:
        # Safe-haven classes stay above the hard floor but still receive real trimming.
        assert cfg.floor < dec[c].scalar < 1.0, (
            f"{c} should trim but not to the hard floor, got {dec[c].scalar}")
    for d in dec.values():
        assert not d.halt_new_entries          # nothing halts at this configuration

    # ...and halting comes back the moment the floor is lowered under the threshold. The safe-
    # haven classes may or may not fall past halt_below (their dilution keeps them higher), so
    # only assert that at least ONE class trips halt — which the risk-off cohort guarantees.
    reachable = OverlayConfig(floor=0.10, halt_below=0.20)
    assert any(d.halt_new_entries for d in RiskOverlay(reachable).evaluate(worst).values())


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
    # CGL.TO (physical gold ETF) is now precious_metals — split out from broad commodity so bond-
    # like safe-haven scalars apply instead of the oil/agriculture stress model.
    assert classify_symbol("CGL.TO") == "precious_metals"
    assert classify_symbol("USO") == "commodity"           # oil ETF stays in the broad bucket
    assert classify_symbol("TLT") == "fixed_income"        # bond ETF routes to fixed_income
    assert classify_symbol("/ES") == "future"
    assert classify_symbol("XIC.TO") == "equity"   # the common default
    assert classify_symbol("XIC.TO", {"XIC.TO": "commodity"}) == "commodity"  # override wins


def test_overlay_provider_caches_and_routes_by_class() -> None:
    calls = {"n": 0}
    stressed = IntelSnapshot(global_alert_count=12, market={"crypto_chg": 9.0})

    def _snap() -> IntelSnapshot:
        calls["n"] += 1
        return stressed

    prov = OverlayProvider(_snap, refresh_seconds=1000.0, journal=False)
    d_crypto = prov("BTC-USD")
    d_equity = prov("AAPL")
    assert d_crypto is not None and d_equity is not None
    assert d_crypto.asset_class == "crypto" and d_equity.asset_class == "equity"
    assert calls["n"] == 1   # second lookup reused the cached snapshot


def test_overlay_provider_is_fail_safe() -> None:
    # never a good read -> returns None (monitor behaves as if no overlay configured)
    def _boom() -> IntelSnapshot:
        raise RuntimeError("network down")

    assert OverlayProvider(_boom, refresh_seconds=0.0, journal=False)("AAPL") is None

    # one good read then failures -> keeps the last good decisions
    state = {"ok": True}

    def _flaky() -> IntelSnapshot:
        if state["ok"]:
            return IntelSnapshot(global_alert_count=12)
        raise RuntimeError("later failure")

    prov = OverlayProvider(_flaky, refresh_seconds=0.0, journal=False)  # always attempts a refresh
    first = prov("AAPL")
    state["ok"] = False
    second = prov("AAPL")
    assert first is not None and second is not None and second.scalar == first.scalar


def test_fear_gate_floors_at_075() -> None:
    """Sentiment is a tilt, not a stop: extreme fear trims at most 25%."""
    ov = RiskOverlay()
    calm = ov._fear_gate(IntelSnapshot(fear_greed=70.0))       # greed -> no de-risk
    panic = ov._fear_gate(IntelSnapshot(fear_greed=5.0))       # extreme fear -> floored
    assert calm == 1.0
    assert abs(panic - OverlayConfig().fear_floor) < 1e-9
    assert OverlayConfig().fear_floor == 0.75
    assert ov._fear_gate(IntelSnapshot(fear_greed=None)) == 1.0   # missing -> no effect


def test_stale_sources_lose_authority_over_the_decision() -> None:
    """Time is a first-class feature: an old payload should not drive a strong de-risk.

    The vendor serves CACHED data (observed ages ran from minutes to ~4 days). A gate's deviation
    from neutral decays with the age of the payload behind it, so a four-day-old energy reading
    de-risks commodities far less than a live one claiming the same thing.
    """
    # Isolate the energy gate: with conflict and event-flow also maxed the class bottoms out at the
    # floor regardless, which would mask the decay this test is about.
    stressed = IntelSnapshot(energy_stress=1.0)
    ov = RiskOverlay()
    fresh = ov.evaluate(stressed)["commodity"].scalar
    day_old = ov.evaluate(replace(stressed, source_age_hours={"energy": 24.0}))["commodity"].scalar
    four_days = ov.evaluate(replace(stressed, source_age_hours={"energy": 96.0}))["commodity"].scalar

    assert fresh < day_old < four_days      # older evidence -> weaker de-risk
    assert four_days <= 1.0


def test_unknown_or_zero_age_is_treated_as_fresh() -> None:
    """A missing stamp usually means a live-computed field, so it must not be silently discounted."""
    s = IntelSnapshot(energy_stress=1.0)
    ov = RiskOverlay()
    assert ov.evaluate(s)["commodity"].scalar == \
        ov.evaluate(replace(s, source_age_hours={"energy": 0.0}))["commodity"].scalar


def test_staleness_only_affects_gates_fed_by_that_source() -> None:
    """A stale energy payload must not soften equity, which reads no energy gate."""
    s = IntelSnapshot(energy_stress=1.0, global_alert_count=8)
    ov = RiskOverlay()
    base = ov.evaluate(s)
    aged = ov.evaluate(replace(s, source_age_hours={"energy": 96.0}))
    assert aged["commodity"].scalar > base["commodity"].scalar   # energy gate discounted
    assert aged["equity"].scalar == base["equity"].scalar        # equity untouched


# --- inverse-weighted freshness on market-driven gates ---------------------------------------

def test_market_derived_gates_are_now_discounted_by_market_freshness() -> None:
    """Previously VIX/DXY/crypto_vol/fear silently returned freshness=1 no matter the age."""
    # High VIX drives the equity_vol gate. A very stale market payload should soften the gate.
    stressed = IntelSnapshot(market={"equity_vol": 40.0})
    ov = RiskOverlay()
    fresh = ov.evaluate(stressed)["equity"].scalar
    aged = ov.evaluate(replace(stressed, source_age_hours={"market": 96.0}))["equity"].scalar
    assert aged > fresh                                # 96h-old VIX de-risks equity less


def test_inverse_weighted_freshness_lets_fresh_dominate_stale() -> None:
    """A hypothetical multi-source gate must weight sources by their OWN freshness.

    Directly exercises RiskOverlay._freshness on a two-source configuration constructed at test
    time — realistic for future gates that blend news + market or news + energy.
    """
    ov = RiskOverlay()
    # Save + restore the class-var so this test's mutation is local.
    original = ov._GATE_SOURCES
    try:
        ov._GATE_SOURCES = {                                # type: ignore[misc]
            **original, "blended": (("news", 1.0), ("market", 1.0)),
        }
        # Case A: news very stale (96h), market fresh (0h)
        s_a = IntelSnapshot(source_age_hours={"news": 96.0, "market": 0.0})
        weighted = ov._freshness(s_a, "blended")
        # naive average would be (0.06 + 1.0) / 2 = 0.53. Inverse-weighted must be much higher,
        # because the news source's tiny effective weight also drags its own contribution down.
        assert weighted > 0.90
        # Case B: both stale — no fresh side to lean on, the blend really is low.
        s_b = IntelSnapshot(source_age_hours={"news": 96.0, "market": 96.0})
        assert ov._freshness(s_b, "blended") < 0.10
    finally:
        ov._GATE_SOURCES = original                        # type: ignore[misc]


def test_single_source_freshness_is_unchanged_from_the_previous_formula() -> None:
    """Regression pin: the refactor must not shift single-source gates' discount at all."""
    ov = RiskOverlay()
    s = IntelSnapshot(source_age_hours={"energy": 24.0})     # exactly one half-life
    assert abs(ov._freshness(s, "energy") - 0.5) < 1e-12


# --- overlay refresh off the trading loop (2026-09-18) ----------------------
# The e2e latency diagnostic found the overlay refresh stalling the Kraken loop for 3.2 s: the
# WorldMonitor snapshot ran synchronously inside the first entry evaluation after each TTL.
# OverlayProvider(background=True) refreshes on a daemon thread instead. (Fetching the tools
# concurrently was tried and rejected: the vendor answers concurrent calls with HTTP 429, which
# _try turns into a degraded snapshot.)


def test_background_provider_serves_stale_decisions_while_refreshing() -> None:
    import threading
    import time

    release = threading.Event()
    calls = {"n": 0}

    def _snap() -> IntelSnapshot:
        calls["n"] += 1
        if calls["n"] > 1:
            release.wait(5)          # the TTL refresh is slow
            return IntelSnapshot(global_alert_count=12, market={"crypto_chg": 9.0})
        return IntelSnapshot()

    prov = OverlayProvider(_snap, refresh_seconds=0.0, journal=False, background=True)
    first = prov("BTC/USD")          # first fetch blocks — nothing to serve yet
    assert first is not None and calls["n"] == 1

    t0 = time.perf_counter()
    stale = prov("BTC/USD")          # TTL expired; refresh goes to the background
    assert time.perf_counter() - t0 < 0.1, "stale lookup blocked on the refresh"
    assert stale is not None and stale.scalar == first.scalar
    prov("BTC/USD")                  # a second stale lookup must not start another fetch
    assert calls["n"] == 2

    release.set()
    for _ in range(100):
        if prov._ts and prov("BTC/USD").scalar < first.scalar:
            break
        time.sleep(0.02)
    assert prov("BTC/USD").scalar < first.scalar   # the stressed snapshot landed


def test_background_provider_keeps_last_good_on_failure() -> None:
    import time

    state = {"ok": True}

    def _flaky() -> IntelSnapshot:
        if state["ok"]:
            return IntelSnapshot(global_alert_count=12)
        raise RuntimeError("later failure")

    prov = OverlayProvider(_flaky, refresh_seconds=0.0, journal=False, background=True)
    first = prov("AAPL")
    state["ok"] = False
    for _ in range(5):
        d = prov("AAPL")
        assert d is not None and d.scalar == first.scalar
        time.sleep(0.02)


def test_provider_default_is_still_synchronous() -> None:
    calls = {"n": 0}

    def _snap() -> IntelSnapshot:
        calls["n"] += 1
        return IntelSnapshot()

    prov = OverlayProvider(_snap, refresh_seconds=0.0, journal=False)
    prov("AAPL")
    prov("AAPL")
    assert calls["n"] == 2          # refreshed inline, both times


def test_overlaid_bias_follows_the_current_overlay_and_scales_by_class() -> None:
    from trading_live_claude.intel.apply import OverlaidBias
    from trading_live_claude.intel.overlay import OverlayDecision
    alloc = AllocationResult(weights={"AAPL": 0.5, "BTC": 0.5}, gross_exposure=1.0, cash=0.0,
                             sleeve_weights={}, effective_positions=2.0)
    scalars = {"equity": 1.0, "crypto": 1.0}

    def ov(sym: str) -> OverlayDecision:
        cls = "crypto" if sym in ("BTC", "ETH") else "equity"
        return OverlayDecision(asset_class=cls, scalar=scalars[cls], halt_new_entries=False,  # type: ignore[arg-type]
                               reasons=[], components={})

    bias = OverlaidBias(alloc, 0.5, ["AAPL", "BTC"], ov)
    assert bias("AAPL") == 1.0 and bias("BTC") == 1.0
    scalars["crypto"] = 0.4                                        # overlay refreshes mid-session
    assert bias("BTC") == 0.4 and bias("AAPL") == 1.0              # only the crypto class shrinks
    assert bias.last is not None and abs(bias.last.cash - 0.3) < 1e-9   # freed weight is cash
    assert bias("ETH") == 0.4                                      # outside the book: default x scalar
    assert getattr(bias, "applies_overlay", False) is True


# --------------------------------------------------------------------------- #
# source staleness -> degraded (fixed 2026-09-24)                              #
# --------------------------------------------------------------------------- #

def _stub_client(ages_h: dict[str, float]) -> WorldMonitorClient:
    """A client whose tools all SUCCEED, handing back payloads cached `ages_h` hours ago.

    Models the real failure this guards: the vendor answers normally, so no transport error is
    raised, but the payload it returns was cached days earlier.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta

    tool_to_key = {
        "get_news_intelligence": "news", "get_conflict_events": "conflict",
        "get_natural_disasters": "disasters", "get_energy_intelligence": "energy",
        "get_market_data": "market",
    }
    client = WorldMonitorClient.__new__(WorldMonitorClient)

    async def call_tool(name: str, args: dict | None = None) -> dict:
        key = tool_to_key.get(name)
        age = ages_h.get(key, 0.0) if key else 0.0
        stamp = (datetime.now(UTC) - timedelta(hours=age)).isoformat()
        return {"cached_at": stamp, "data": {}}

    client.call_tool = call_tool           # type: ignore[method-assign]
    client._asyncio = asyncio
    return client


def test_fresh_sources_are_not_degraded() -> None:
    import asyncio
    c = _stub_client({"news": 0.2, "conflict": 2.0, "energy": 3.0, "market": 0.1})
    snap = asyncio.run(c.snapshot())
    assert snap.degraded is False
    assert max(snap.source_age_hours.values()) < MAX_SOURCE_AGE_H


def test_stale_source_degrades_even_though_every_tool_succeeded() -> None:
    """The 2026-09-24 bug: energy 135h old, every call OK, snapshot published degraded=False."""
    import asyncio
    c = _stub_client({"news": 0.2, "conflict": 2.1, "energy": 135.07, "market": 0.03})
    snap = asyncio.run(c.snapshot())
    assert snap.degraded is True, "a 135h-old source must degrade the snapshot"
    assert snap.source_age_hours["energy"] > MAX_SOURCE_AGE_H
    assert snap.source_age_hours["news"] < MAX_SOURCE_AGE_H


def test_staleness_threshold_is_the_boundary() -> None:
    import asyncio
    assert asyncio.run(_stub_client({"energy": MAX_SOURCE_AGE_H - 1.0}).snapshot()).degraded is False
    assert asyncio.run(_stub_client({"energy": MAX_SOURCE_AGE_H + 1.0}).snapshot()).degraded is True
