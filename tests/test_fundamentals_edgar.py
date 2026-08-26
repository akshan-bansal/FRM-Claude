from __future__ import annotations

from pathlib import Path

import httpx
import respx

from trading_live_claude.data.fundamentals import FundamentalsStore
from trading_live_claude.data.fundamentals_edgar import edgar_bvps, fetch_to_store

_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_FACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"


def _mock_edgar() -> None:
    respx.get(_TICKERS).mock(
        return_value=httpx.Response(200, json={"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple"}})
    )
    respx.get(_FACTS).mock(return_value=httpx.Response(200, json={"facts": {"us-gaap": {
        "StockholdersEquity": {"units": {"USD": [
            {"end": "2023-12-31", "val": 60_000_000_000},
            {"end": "2024-03-31", "val": 66_000_000_000},
        ]}},
        "CommonStockSharesOutstanding": {"units": {"shares": [
            {"end": "2023-12-31", "val": 15_000_000_000},
            {"end": "2024-03-31", "val": 15_000_000_000},
        ]}},
    }}}))


@respx.mock
def test_edgar_bvps_is_equity_over_shares() -> None:
    _mock_edgar()
    df = edgar_bvps("AAPL", user_agent="test test@example.com")
    assert df is not None and len(df) == 2
    assert df.iloc[0]["bvps"] == 4.0   # 60e9 / 15e9
    assert df.iloc[1]["bvps"] == 4.4   # 66e9 / 15e9
    assert list(df.columns) == ["date", "bvps"]


@respx.mock
def test_edgar_unknown_ticker_returns_none() -> None:
    respx.get(_TICKERS).mock(
        return_value=httpx.Response(200, json={"0": {"cik_str": 1, "ticker": "AAPL", "title": "x"}})
    )
    assert edgar_bvps("NOTREAL", user_agent="t t@t.com") is None


@respx.mock
def test_fetch_to_store_writes_a_loadable_csv(tmp_path: Path) -> None:
    _mock_edgar()
    store = FundamentalsStore(tmp_path)
    n = fetch_to_store("AAPL", "AAPL", store, user_agent="test t@t.com")
    assert n == 2
    assert store.has("AAPL")
    # The store can read back exactly what was fetched.
    import pandas as pd

    df = pd.DataFrame({"time": pd.to_datetime(["2024-01-15", "2024-04-15"]).tz_localize("UTC")})
    s = store.bvps_series(df, "AAPL")
    assert s is not None and s.iloc[0] == 4.0  # forward-filled from the 2023-12-31 point
