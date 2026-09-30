"""Shared response curves.

``log_ramp`` is the bounded, positive, concave log curve used wherever the project grades "how
far along a range" with diminishing sensitivity: thesis intensity (``intel.thesis_intensity``)
and the profit-lock giveback (``signals.profit_lock``).
"""
from __future__ import annotations

import math

LOG_CURVATURE: float = 9.0      # ln(1 + 9u) / ln(10) == log10(1 + 9u)


def log_ramp(x: float, lo: float, hi: float, k: float = LOG_CURVATURE) -> float:
    """Bounded positive log curve: 0 at ``lo``, 1 at ``hi``, concave and increasing in between.

    ``ln(1 + k*u) / ln(1 + k)`` with ``u = clamp((x - lo) / (hi - lo), 0, 1)``. With the default
    ``k = 9`` this is ``log10(1 + 9u)``; ``k`` sets the curvature (k -> 0 is a straight line).
    """
    if hi <= lo:
        raise ValueError(f"log_ramp needs hi > lo, got lo={lo} hi={hi}")
    if k <= 0:
        raise ValueError(f"log_ramp needs k > 0, got {k}")
    u = min(max((x - lo) / (hi - lo), 0.0), 1.0)
    return math.log1p(k * u) / math.log1p(k)
