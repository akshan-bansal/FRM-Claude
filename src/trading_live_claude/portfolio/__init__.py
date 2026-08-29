"""Portfolio construction — turn a ranked, scored list of names into a weighted book.

Everything upstream scores names one at a time; this is the layer that decides *how much* of each
to hold, budgeting risk across them with awareness of correlation (so the two banks ETFs don't
count as two independent bets) and scaling gross exposure by the market-regime scalar.
"""
from __future__ import annotations

from .allocator import AllocationResult, PortfolioAllocator
from .pipeline import build_book, ranker_scores

__all__ = ["AllocationResult", "PortfolioAllocator", "build_book", "ranker_scores"]
