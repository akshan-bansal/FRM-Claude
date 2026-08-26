"""Pull historical book value per share from SEC EDGAR's XBRL company-facts API.

EDGAR exposes structured, historical quarterly financials as JSON — no HTML/PDF scraping.
Book value per share is a *derived* figure (rarely a labelled line item), so this computes
it per reporting period as ``StockholdersEquity / shares outstanding``, yielding exactly the
quarterly ``(date, bvps)`` series ``FundamentalsStore`` consumes. Works for US filers and
SEC-cross-listed foreign filers (many Canadian large-caps file a 40-F). Uses ``httpx`` per
the project's HTTP convention.

SEC fair-access requires a descriptive ``User-Agent`` with a contact — set ``EDGAR_USER_AGENT``
in the environment, or pass ``user_agent=``. Pure-Canadian names not on EDGAR have no reliable
programmatic source (SEDAR+ is HTML/PDF only); for those, drop a CSV into the store by hand.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_EDGAR = "https://data.sec.gov"
_DEFAULT_UA = "FRM-Claude/1.0 (+https://localhost; research; set EDGAR_USER_AGENT)"


def _user_agent(explicit: str | None) -> str:
    return explicit or os.environ.get("EDGAR_USER_AGENT") or _DEFAULT_UA


def _cik_for(ticker: str, client: httpx.Client) -> str | None:
    data = client.get(_TICKERS_URL).json()
    t = ticker.lower()
    for row in data.values():
        if str(row.get("ticker", "")).lower() == t:
            return str(row["cik_str"]).zfill(10)
    return None


def _series(facts: dict[str, Any], taxonomy: str, tags: tuple[str, ...]) -> pd.Series:
    """First non-empty XBRL concept among ``tags``, as a date-indexed Series (latest-filed per
    period-end). Reads the one-shot ``companyfacts`` payload — the per-``companyconcept`` endpoint
    returns empty ``units`` for some filers (e.g. RELIANCE) even when companyfacts has the data."""
    node = facts.get(taxonomy, {})
    for tag in tags:
        vals: dict[str, float] = {}
        for rows in node.get(tag, {}).get("units", {}).values():
            for row in rows:
                end, val = row.get("end"), row.get("val")
                if end is not None and val is not None:
                    vals[end] = float(val)  # later rows are more-recently filed → keep last
        if vals:
            s = pd.Series(vals)
            s.index = pd.to_datetime(s.index)
            return s.sort_index()
    return pd.Series(dtype=float)


def edgar_bvps(ticker: str, *, user_agent: str | None = None, timeout: float = 30.0) -> pd.DataFrame | None:
    """Quarterly ``(date, bvps)`` book value per share from EDGAR, or ``None`` if unavailable."""
    headers = {"User-Agent": _user_agent(user_agent), "Accept-Encoding": "gzip, deflate"}
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        cik = _cik_for(ticker, client)
        if cik is None:
            return None
        r = client.get(f"{_EDGAR}/api/xbrl/companyfacts/CIK{cik}.json")
        if r.status_code != 200:
            return None
        facts = r.json().get("facts", {})
        equity = _series(facts, "us-gaap",
                         ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"))
        shares = _series(facts, "us-gaap", ("CommonStockSharesOutstanding",))
        if shares.empty:
            shares = _series(facts, "dei", ("EntityCommonStockSharesOutstanding",))
        if equity.empty or shares.empty:
            return None
        # Align shares to each equity period-end by nearest date (shares' "as of" can differ).
        aligned = shares.reindex(equity.index, method="nearest", tolerance=pd.Timedelta("100D"))
        bvps = (equity / aligned).dropna()
        bvps = bvps[bvps > 0]
        if bvps.empty:
            return None
        out = bvps.reset_index()
        out.columns = ["date", "bvps"]
        return out


def fetch_to_store(ticker: str, symbol: str, store: object, *, user_agent: str | None = None) -> int:
    """Fetch ``ticker`` from EDGAR and write the store's CSV for ``symbol``; returns row count."""
    df = edgar_bvps(ticker, user_agent=user_agent)
    if df is None or df.empty:
        return 0
    path = store.path_for(symbol)  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return len(df)
