"""Interlisted (cross-listing) equity arbitrage — TSX ⇄ NYSE, FX-adjusted.

The equity analog of cross-exchange arb. Dozens of Canadian names trade on *both* the TSX (in CAD)
and a US exchange (in USD): RY.TO/RY, ENB.TO/ENB, SHOP.TO/SHOP, … The same share is fungible across
the border, so the two prices should agree once converted through the CAD/USD rate — and when they
don't by more than costs, you buy the cheap listing and sell the dear one.

The twist over crypto is the **FX leg**: comparing the two quotes means converting one through
``usdcad`` (CAD per 1 USD), and a real trade pays an FX-conversion cost on top of the two equity
commissions. That FX cost is the whole story — at institutional rates (~1-5 bps) transient
dislocations are capturable; at retail conversion (~150-200 bps) they never are, which is why this
is a pro trade. **Paper only** — this measures the opportunity; it places no orders.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InterlistedConfig:
    equity_fee_bps: float = 5.0   # per equity leg
    fx_fee_bps: float = 5.0       # FX conversion cost (institutional ~1-5; retail Questrade ~150-200)
    min_edge_bps: float = 1.0     # require net edge beyond this to "trade"
    max_shares: float = 100.0


@dataclass(frozen=True)
class InterlistedQuote:
    pair: str
    tsx_bid: float
    tsx_ask: float                # CAD
    us_bid: float
    us_ask: float                 # USD
    usdcad: float                 # CAD per 1 USD


@dataclass(frozen=True)
class InterlistedTick:
    pair: str
    edge_bps: float               # best net cross-listing edge, bps (<= 0 means none)
    action: str                   # "sell_tsx_buy_us" | "sell_us_buy_tsx" | "none"
    implied_usdcad: float         # FX implied by the two mids (tsx_mid / us_mid)
    trade_pnl: float              # per-share USD net, if action != none
    size: float


class InterlistedArb:
    """Detect FX-adjusted dislocations between a name's TSX and US listings."""

    def __init__(self, cfg: InterlistedConfig | None = None) -> None:
        self.cfg = cfg or InterlistedConfig()

    def evaluate(self, q: InterlistedQuote) -> InterlistedTick:
        eq = self.cfg.equity_fee_bps / 1e4
        fx = self.cfg.fx_fee_bps / 1e4
        tsx_bid_usd = q.tsx_bid / q.usdcad          # TSX quotes converted to USD
        tsx_ask_usd = q.tsx_ask / q.usdcad
        mid = 0.25 * (tsx_bid_usd + tsx_ask_usd + q.us_bid + q.us_ask)
        implied = (0.5 * (q.tsx_bid + q.tsx_ask)) / (0.5 * (q.us_bid + q.us_ask)) if (q.us_bid + q.us_ask) > 0 else 0.0

        # A: sell TSX bid (CAD->USD, pay FX), buy US ask (USD)
        net_a = tsx_bid_usd * (1 - fx) - q.us_ask - eq * (tsx_bid_usd + q.us_ask)
        # B: sell US bid (USD), buy TSX ask (USD-equivalent, pay FX)
        net_b = q.us_bid - tsx_ask_usd * (1 + fx) - eq * (q.us_bid + tsx_ask_usd)

        if net_a >= net_b:
            net, action = net_a, "sell_tsx_buy_us"
        else:
            net, action = net_b, "sell_us_buy_tsx"
        edge_bps = net / mid * 1e4 if mid > 0 else 0.0

        if edge_bps > self.cfg.min_edge_bps:
            size = self.cfg.max_shares
            return InterlistedTick(q.pair, edge_bps, action, implied, net * size, size)
        return InterlistedTick(q.pair, edge_bps, "none", implied, 0.0, 0.0)
