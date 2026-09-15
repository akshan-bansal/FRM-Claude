"""LEAN algorithm mirroring the paper book's futures rules, for empirical backtests on QuantConnect.

It ports the local strategies bar-for-bar (``bollinger``: 20/2σ population std, fresh cross back
above the lower band, exit at the mid band, 15-bar time stop; ``ts_momentum``: 126-bar ROC sign) and
the router's sizing: 1% risk to a 2×ATR(14, Wilder) stop scaled by signal strength, vol-scaled
per-name cap, 1.0× gross leverage, 5% heat cap, max open positions. Continuous contracts roll on
open interest and never take delivery. No intel overlay and no allocator bias — pure rules.
"""
from __future__ import annotations

from collections.abc import Sequence


def render_futures_mirror(
    *,
    strategy: str,
    roots: Sequence[str],
    start: tuple[int, int, int],
    end: tuple[int, int, int],
    cash: int,
    max_open_positions: int = 3,
    risk_pct: float = 0.01,
    heat_cap: float = 0.05,
    gross_leverage: float = 1.0,
    cap_base: float = 0.50,
    cap_ref_vol: float = 0.20,
    cap_floor: float = 0.05,
    cap_ceiling: float = 0.75,
) -> str:
    if strategy not in ("bollinger", "ts_momentum"):
        raise ValueError(f"unsupported strategy {strategy!r}")
    sy, sm, sd = start
    ey, em, ed = end
    return f'''\
from AlgorithmImports import *
import math
from collections import defaultdict


class FrmFuturesMirror(QCAlgorithm):
    STRATEGY = "{strategy}"
    ROOTS = {list(roots)!r}

    def initialize(self):
        self.set_start_date({sy}, {sm}, {sd})
        self.set_end_date({ey}, {em}, {ed})
        self.set_cash({cash})
        self.set_brokerage_model(BrokerageName.INTERACTIVE_BROKERS_BROKERAGE, AccountType.MARGIN)
        self.futs = {{}}
        for root in self.ROOTS:
            self.futs[root] = self.add_future(
                root, Resolution.DAILY, extended_market_hours=True,
                data_mapping_mode=DataMappingMode.OPEN_INTEREST,
                data_normalization_mode=DataNormalizationMode.BACKWARDS_RATIO,
                contract_depth_offset=0)
        self.bars = {{r: [] for r in self.ROOTS}}
        self.atr = {{r: None for r in self.ROOTS}}
        self.held_bars = defaultdict(int)
        self.entries = defaultdict(int)
        self.skipped = defaultdict(int)
        self.year_equity = {{}}
        self.set_warm_up(300, Resolution.DAILY)

    # ---- indicators (ported from signals/indicators.py) ----
    def _update(self, root, bar):
        closes = self.bars[root]
        prev_close = closes[-1][3] if closes else None
        closes.append((bar.open, bar.high, bar.low, bar.close))
        if len(closes) > 400:
            del closes[0]
        tr = bar.high - bar.low if prev_close is None else max(
            bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        a = self.atr[root]
        self.atr[root] = tr if a is None else a + (tr - a) / 14.0

    def _closes(self, root):
        return [b[3] for b in self.bars[root]]

    def _vol(self, root):
        c = self._closes(root)
        rets = [c[i] / c[i - 1] - 1.0 for i in range(1, len(c)) if c[i - 1] > 0]
        vols = []
        for w in (20, 60):
            if len(rets) >= w:
                x = rets[-w:]
                m = sum(x) / w
                sd = math.sqrt(sum((r - m) ** 2 for r in x) / (w - 1))
                if sd > 0:
                    vols.append(sd * math.sqrt(252))
        return max(vols) if vols else None

    def _signal(self, root):
        c = self._closes(root)
        if self.STRATEGY == "bollinger":
            if len(c) < 22:
                return 0, 0, 0.0
            def band(xs):
                m = sum(xs) / 20.0
                sd = math.sqrt(sum((v - m) ** 2 for v in xs) / 20.0)
                return m, m - 2.0 * sd
            mid, lower = band(c[-20:])
            _, prev_lower = band(c[-21:-1])
            entry = int(c[-2] < prev_lower and c[-1] >= lower)
            exit_ = int(c[-1] >= mid)
            span = mid - lower
            strength = 0.0 if span <= 0 else max(0.0, min(1.0, (mid - c[-1]) / span))
            return entry, exit_, strength
        if len(c) < 128:
            return 0, 0, 0.0
        roc = c[-1] / c[-127] - 1.0
        return int(roc > 0.0), int(roc < 0.0), max(0.0, min(1.0, roc / 0.25))

    # ---- book ----
    def _contract_value(self, root):
        sec = self.securities[self.futs[root].mapped]
        return sec.price * sec.symbol_properties.contract_multiplier, sec.symbol_properties.contract_multiplier

    def _held(self, root):
        mapped = self.futs[root].mapped
        return self.portfolio[mapped].quantity if mapped in self.portfolio else 0

    def on_data(self, data: Slice):
        for changed in data.symbol_changed_events.values():
            qty = self.portfolio[changed.old_symbol].quantity
            if qty != 0 and not self.is_warming_up:
                self.liquidate(changed.old_symbol, tag="roll")
                self.market_order(changed.new_symbol, qty, tag="roll")

        for root, fut in self.futs.items():
            bar = data.bars.get(fut.symbol)
            if bar is not None:
                self._update(root, bar)

        year = self.time.year
        if year not in self.year_equity:
            self.year_equity[year] = self.portfolio.total_portfolio_value
        if self.is_warming_up:
            return

        equity = self.portfolio.total_portfolio_value
        open_roots = [r for r in self.ROOTS if self._held(r) != 0]
        open_notional = 0.0
        open_risk = 0.0
        for r in open_roots:
            value, mult = self._contract_value(r)
            open_notional += abs(self._held(r)) * value
            open_risk += abs(self._held(r)) * 2.0 * (self.atr[r] or 0.0) * mult

        for root, fut in self.futs.items():
            if data.bars.get(fut.symbol) is None or fut.mapped is None:
                continue
            entry, exit_, strength = self._signal(root)
            held = self._held(root)
            if held > 0:
                self.held_bars[root] += 1
                time_stop = self.STRATEGY == "bollinger" and self.held_bars[root] >= 15
                if exit_ or time_stop:
                    self.liquidate(fut.mapped, tag="time_stop" if time_stop and not exit_ else "exit")
                    self.held_bars[root] = 0
                continue
            if not entry or self.atr[root] is None:
                continue
            value, mult = self._contract_value(root)
            atr = self.atr[root]
            if value <= 0 or atr <= 0:
                continue
            n = math.floor(equity * {risk_pct} * strength / (2.0 * atr * mult))
            vol = self._vol(root)
            cap_pct = {cap_floor} if vol is None else min(max({cap_base} * {cap_ref_vol} / vol, {cap_floor}), {cap_ceiling})
            n = min(n, math.floor(equity * cap_pct / value),
                    math.floor(max(0.0, equity * {gross_leverage} - open_notional) / value))
            new_risk = n * 2.0 * atr * mult
            if n <= 0 or len(open_roots) >= {max_open_positions} or open_risk + new_risk > {heat_cap} * equity:
                self.skipped[root] += 1
                continue
            if n * value < 100:
                self.skipped[root] += 1
                continue
            self.market_order(fut.mapped, n, tag="entry")
            self.entries[root] += 1
            self.held_bars[root] = 0
            open_roots.append(root)
            open_notional += n * value
            open_risk += new_risk

    def on_end_of_algorithm(self):
        pnl = defaultdict(float)
        for kvp in self.portfolio:
            pnl[kvp.key.id.symbol] += kvp.value.net_profit
        for root in self.ROOTS:
            self.set_runtime_statistic(f"pnl_{{root}}", f"{{pnl.get(root, 0.0):.0f}}")
            self.set_runtime_statistic(f"entries_{{root}}", str(self.entries[root]))
            self.set_runtime_statistic(f"skipped_{{root}}", str(self.skipped[root]))
        years = sorted(self.year_equity)
        final = self.portfolio.total_portfolio_value
        for i, y in enumerate(years):
            end_value = self.year_equity[years[i + 1]] if i + 1 < len(years) else final
            start_value = self.year_equity[y]
            if start_value > 0:
                self.set_runtime_statistic(f"ret_{{y}}", f"{{(end_value / start_value - 1) * 100:.2f}}%")
'''
