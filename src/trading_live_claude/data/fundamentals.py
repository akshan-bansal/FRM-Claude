"""Historical fundamentals ingestion — book value per share for the valuation strategies.

Questrade's securities API exposes **no** book value, and the fundamental fields it does
return (eps, pe, marketCap, outstandingShares) are current snapshots, not time series. A
*constant* book value makes price-to-book merely a scaled price, so the ``val_*`` strategies
collapse to their price twins. To give them a real signal, book value per share must arrive
as a **quarterly time series** — the standard way to backtest fundamentals.

``FundamentalsStore`` loads that series from ``data/fundamentals/{SYMBOL}.csv`` (columns
``date,bvps`` — dots in the ticker become underscores, e.g. ``RY_TO.csv``) and forward-fills
the quarterly book values onto the daily bars. When a symbol has no file (or the frame has
no dates to align to), ``bvps_series`` returns ``None`` and the caller falls back to the
price level — so every strategy stays runnable on plain OHLCV and lights up per-name as
files are dropped in.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


class FundamentalsStore:
    """Per-symbol quarterly book-value-per-share, loaded from CSV and daily-aligned."""

    def __init__(self, root: Path | str = "data/fundamentals") -> None:
        self.root = Path(root)

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{symbol.replace('.', '_')}.csv"

    def has(self, symbol: str) -> bool:
        return self.path_for(symbol).exists()

    def bvps_series(self, df: pd.DataFrame, symbol: str) -> pd.Series | None:
        """Daily book-value-per-share aligned to ``df`` (forward-filled from the quarterly
        file), or ``None`` when the symbol has no file / the frame carries no ``time`` dates."""
        path = self.path_for(symbol)
        if not path.exists() or "time" not in df.columns:
            return None
        try:
            fund = pd.read_csv(path)
        except Exception:
            return None
        if "date" not in fund.columns or "bvps" not in fund.columns:
            return None
        fund = fund.assign(date=pd.to_datetime(fund["date"])).dropna(subset=["date", "bvps"]).sort_values("date")
        if fund.empty:
            return None

        dates = pd.to_datetime(df["time"]).dt.tz_localize(None)
        quarterly = fund.set_index("date")["bvps"].astype(float)
        quarterly = quarterly[~quarterly.index.duplicated(keep="last")]
        # Forward-fill the quarterly book values onto each daily bar's date.
        combined = quarterly.reindex(quarterly.index.union(dates)).sort_index().ffill()
        aligned = combined.reindex(dates).to_numpy()
        return pd.Series(aligned, index=df.index, name="bvps")
