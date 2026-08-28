"""Paper (dry-run) Avellaneda-Stoikov market maker driven by the live Kraken L2 book.

This is the bridge between the live feed (:mod:`.kraken_l2`) and the A-S quoting theory
(:mod:`.avellaneda_stoikov`). On every book update it:

1. centers fair value on the **microprice** (which already leans toward the thin side of the book);
2. estimates volatility ``sigma`` live from a rolling window of microprice changes;
3. sets the A-S optimal half-spread and skews the reservation price against current **inventory**
   (sell-down when long) and along **order-flow imbalance** (lean with the flow to avoid being
   run over);
4. models fills with the Poisson intensity ``A*exp(-k*delta)`` on each quote's distance from the
   microprice — we do *not* post real orders, so our own fills aren't observable; everything that
   drives the quotes (book, microprice, sigma, OFI) is live, the fill is the one modeled piece.

**Paper only.** Nothing here places an order or touches a broker. Live quoting stays behind the
project's human go-live gate; this loop just shows what the policy would do and tracks paper P&L
and inventory. Fill randomness is seedable for reproducible tests.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .kraken_l2 import BookUpdate
from .simulator import FillModel


@dataclass(frozen=True)
class MMConfig:
    gamma: float = 0.1              # inventory risk aversion
    k: float = 1.5                  # order-book fill decay (A-S spread term + fill model)
    horizon: float = 1.0            # effective A-S horizon (steady-state proxy)
    sigma_window: int = 60          # rolling window for the live volatility estimate
    quote_size: float = 0.001       # size quoted per side (base units, e.g. BTC)
    inventory_limit: float = 0.02   # stop quoting a side past +/- this inventory
    ofi_gain: float = 0.3           # how strongly OFI skews the reservation price (x half-spread)
    min_half_spread: float = 0.0    # floor on the quoted half-spread
    fill_a: float = 140.0           # Poisson base intensity for the modeled fills
    fill_dt: float = 0.05           # time step for the fill intensity (tunes fill frequency)


@dataclass(frozen=True)
class MMState:
    step: int
    microprice: float
    sigma: float
    ofi: float
    reservation: float
    bid: float
    ask: float
    half_spread: float
    inventory: float
    cash: float
    pnl: float           # marked to microprice
    fills: int


@dataclass
class PaperMarketMaker:
    """Stateful paper A-S maker. Feed it :class:`BookUpdate`s; it returns an :class:`MMState`."""

    cfg: MMConfig = field(default_factory=MMConfig)
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))
    inventory: float = 0.0
    cash: float = 0.0
    fills: int = 0
    step: int = 0
    _mids: deque[float] = field(default_factory=lambda: deque(maxlen=60))
    _ofi_scale: float = 1.0

    def __post_init__(self) -> None:
        self._mids = deque(maxlen=self.cfg.sigma_window)
        self._fill = FillModel(a=self.cfg.fill_a, k=self.cfg.k)

    def _sigma(self) -> float:
        if len(self._mids) < 3:
            return 0.0
        return float(np.std(np.diff(np.asarray(self._mids))))

    def quotes(self, microprice: float, ofi: float) -> tuple[float, float, float, float]:
        """Return ``(bid, ask, reservation, half_spread)`` for a microprice, inventory and OFI."""
        sigma = self._sigma()
        c = self.cfg
        half = 0.5 * (c.gamma * sigma ** 2 * c.horizon + (2.0 / c.gamma) * np.log1p(c.gamma / c.k))
        half = max(half, c.min_half_spread)
        reservation = microprice - self.inventory / c.quote_size * c.gamma * sigma ** 2 * c.horizon
        # lean the reservation with order flow (normalized by a slow-moving scale of |OFI|)
        self._ofi_scale = max(1e-9, 0.99 * self._ofi_scale + 0.01 * abs(ofi))
        reservation += c.ofi_gain * float(np.clip(ofi / self._ofi_scale, -1.0, 1.0)) * half
        return reservation - half, reservation + half, reservation, half

    def on_book(self, update: BookUpdate) -> MMState:
        c = self.cfg
        micro = update.microprice
        self._mids.append(micro)
        bid, ask, reservation, half = self.quotes(micro, update.ofi)

        # Modeled fills: Poisson intensity on each quote's distance from the microprice.
        if self.inventory < c.inventory_limit and self._fill.fills(micro - bid, c.fill_dt, self.rng):
            self.inventory += c.quote_size
            self.cash -= bid * c.quote_size
            self.fills += 1
        if self.inventory > -c.inventory_limit and self._fill.fills(ask - micro, c.fill_dt, self.rng):
            self.inventory -= c.quote_size
            self.cash += ask * c.quote_size
            self.fills += 1

        self.step += 1
        pnl = self.cash + self.inventory * micro
        return MMState(step=self.step, microprice=micro, sigma=self._sigma(), ofi=update.ofi,
                       reservation=reservation, bid=bid, ask=ask, half_spread=half,
                       inventory=self.inventory, cash=self.cash, pnl=pnl, fills=self.fills)


async def run_paper_market_maker(symbol: str, *, on_state: Callable[[MMState], None],
                                 cfg: MMConfig | None = None, depth: int = 10,
                                 max_messages: int | None = None) -> PaperMarketMaker:
    """Drive a :class:`PaperMarketMaker` off the live Kraken book, calling ``on_state`` each update.

    Paper only — subscribes to public L2 data and never posts an order. Returns the maker so the
    caller can read final inventory / P&L. Needs the optional ``l2`` extra.
    """
    from .kraken_l2 import stream_order_book

    mm = PaperMarketMaker(cfg=cfg or MMConfig())
    await stream_order_book(symbol, depth=depth, max_messages=max_messages,
                            on_update=lambda u: on_state(mm.on_book(u)))
    return mm
