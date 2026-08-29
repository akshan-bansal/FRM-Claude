"""Live OSINT intelligence overlay (WorldMonitor).

WorldMonitor's tools are live-only — no point-in-time history — so this layer never produces a
backtestable signal. It is a **live risk overlay** that can only de-risk the book: intelligence
artifacts (news alerts, conflict events, natural disasters, energy stress, market-vol proxies) are
normalized into an :class:`IntelSnapshot`, scored by :class:`RiskOverlay` into a per-asset-class
gross-exposure scalar (equity / future / commodity / fx / crypto), and applied on top of an existing
allocation with :func:`apply_overlay`. Every read is journaled so we accumulate our own point-in-time
history over time.
"""
from __future__ import annotations

from .apply import apply_overlay
from .overlay import (
    OVERLAY_CLASSES,
    IntelSnapshot,
    OverlayClass,
    OverlayConfig,
    OverlayDecision,
    RiskOverlay,
)
from .routing import OverlayProvider, classify_symbol

__all__ = [
    "OVERLAY_CLASSES",
    "IntelSnapshot",
    "OverlayClass",
    "OverlayConfig",
    "OverlayDecision",
    "OverlayProvider",
    "RiskOverlay",
    "apply_overlay",
    "classify_symbol",
]
