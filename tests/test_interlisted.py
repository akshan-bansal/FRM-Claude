from __future__ import annotations

from trading_live_claude.microstructure import InterlistedArb, InterlistedConfig, InterlistedQuote


def _quote(pair: str, tsx: float, us: float, usdcad: float, spread_bps: float = 2.0) -> InterlistedQuote:
    ts = tsx * spread_bps / 1e4 / 2
    uss = us * spread_bps / 1e4 / 2
    return InterlistedQuote(pair, tsx_bid=tsx - ts, tsx_ask=tsx + ts, us_bid=us - uss, us_ask=us + uss, usdcad=usdcad)


def test_fx_consistent_prices_have_no_edge() -> None:
    # TSX price exactly = US price * usdcad -> no dislocation; costs make edge negative.
    q = _quote("RY", tsx=100.0 * 1.36, us=100.0, usdcad=1.36)
    tick = InterlistedArb().evaluate(q)
    assert tick.action == "none" and tick.edge_bps <= InterlistedArb().cfg.min_edge_bps
    assert abs(tick.implied_usdcad - 1.36) < 1e-6


def test_detects_dislocation_and_direction() -> None:
    # TSX cheap relative to US (TSX implies a lower price) -> buy TSX, sell US.
    q = _quote("ENB", tsx=100.0 * 1.36 * 0.99, us=100.0, usdcad=1.36)  # TSX ~1% cheap
    tick = InterlistedArb(InterlistedConfig(equity_fee_bps=2, fx_fee_bps=3, max_shares=100)).evaluate(q)
    assert tick.action == "sell_us_buy_tsx"
    assert tick.edge_bps > 0 and tick.trade_pnl > 0 and tick.size == 100


def test_fx_cost_gates_marginal_edges() -> None:
    q = _quote("SU", tsx=100.0 * 1.36 * 0.995, us=100.0, usdcad=1.36)  # ~0.5% gap
    cheap = InterlistedArb(InterlistedConfig(fx_fee_bps=3, equity_fee_bps=2))
    retail = InterlistedArb(InterlistedConfig(fx_fee_bps=180, equity_fee_bps=2))  # retail FX conversion
    assert cheap.evaluate(q).action != "none"      # institutional FX -> the gap clears
    assert retail.evaluate(q).action == "none"     # retail FX -> conversion cost buries it


def test_both_directions_symmetric() -> None:
    cfg = InterlistedConfig(equity_fee_bps=2, fx_fee_bps=3)
    dear_tsx = InterlistedArb(cfg).evaluate(_quote("X", tsx=100 * 1.36 * 1.01, us=100.0, usdcad=1.36))
    cheap_tsx = InterlistedArb(cfg).evaluate(_quote("Y", tsx=100 * 1.36 * 0.99, us=100.0, usdcad=1.36))
    assert dear_tsx.action == "sell_tsx_buy_us"
    assert cheap_tsx.action == "sell_us_buy_tsx"
