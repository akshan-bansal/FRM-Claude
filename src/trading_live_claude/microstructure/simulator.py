"""A minimal market simulator for testing market-making policies.

Two pieces, both from Avellaneda & Stoikov (2008):

* :class:`MidPriceProcess` — the efficient mid price as arithmetic Brownian motion
  ``dS = sigma * sqrt(dt) * Z``. Arithmetic (not geometric) matches the A-S derivation.
* :class:`FillModel` — a market order arrives and lifts a resting quote at distance ``delta``
  from the mid with probability ``1 - exp(-lambda * dt)`` where the intensity ``lambda = A*exp(-k*delta)``
  decays with distance. Tighter quotes fill more often; that trade-off is the whole game.

Deterministic given a seeded ``numpy`` Generator, so simulations are reproducible in tests.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class MidPriceProcess:
    """Arithmetic Brownian mid price. ``sigma`` is per-sqrt-time volatility, ``dt`` the step."""

    s0: float = 100.0
    sigma: float = 2.0
    dt: float = 1.0 / 200.0

    def path(self, steps: int, rng: np.random.Generator) -> np.ndarray:
        shocks = self.sigma * np.sqrt(self.dt) * rng.standard_normal(steps)
        return self.s0 + np.concatenate([[0.0], np.cumsum(shocks)])


@dataclass(frozen=True)
class FillModel:
    """Poisson fill intensity ``lambda = A*exp(-k*delta)`` for a quote ``delta`` from the mid."""

    a: float = 140.0   # base arrival intensity
    k: float = 1.5     # how fast fill probability decays with distance

    def fill_probability(self, delta: float, dt: float) -> float:
        intensity = self.a * np.exp(-self.k * max(delta, 0.0))
        return float(1.0 - np.exp(-intensity * dt))

    def fills(self, delta: float, dt: float, rng: np.random.Generator) -> bool:
        return bool(rng.random() < self.fill_probability(delta, dt))
