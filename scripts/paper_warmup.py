"""Run a quick paper-trading sanity sweep across all bundled strategies.

Useful as a pre-flight before going live: confirms every strategy can
generate signals, the paper router fills them, and journals write.

Usage:
    uv run python scripts/paper_warmup.py
"""
from __future__ import annotations

import sys

from trading_live_claude.brokers import PaperBroker, QuestradeBroker
from trading_live_claude.config import get_settings
from trading_live_claude.data import CandleCache, MarketData
from trading_live_claude.execution import Router
from trading_live_claude.monitor import LiveMonitor
from trading_live_claude.risk import PositionSizer
from trading_live_claude.strategies import STRATEGIES


def main() -> int:
    s = get_settings()
    feed = QuestradeBroker.from_settings(
        refresh_token=s.questrade_refresh_token,
        encryption_key=s.token_encryption_key,
        state_dir=s.state_dir,
    )
    paper = PaperBroker(feed=feed, starting_equity=100_000, journal_dir=s.state_dir)
    market = MarketData(feed, cache=CandleCache(s.data_cache_dir))
    sizer = PositionSizer(risk_pct=s.risk_pct_per_trade)

    for name in STRATEGIES:
        if name == "pairs":
            continue
        print(f"\n=== {name} ===")
        strat = STRATEGIES[name]()
        router = Router.build_default(mode="paper", broker=paper, state_dir=s.state_dir)
        monitor = LiveMonitor(
            broker=paper,
            market=market,
            strategy=strat,
            sizer=sizer,
            router=router,
            account_number="PAPER-001",
            symbols=s.symbols_list[:2],
            interval_seconds=1,
        )
        monitor.run_forever(max_iterations=1)
    print("\nWarmup complete. Inspect state/orders.jsonl and state/paper_fills.jsonl.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
