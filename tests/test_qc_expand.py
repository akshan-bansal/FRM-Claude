from __future__ import annotations

import httpx
import respx

from trading_live_claude.integrations.qc_library import (
    analyze_library,
    analyze_source,
    categorize_source,
    detect_asset_classes,
    detect_indicators,
    detect_symbols,
    family_from_indicators,
)
from trading_live_claude.integrations.quantconnect import QC_API_BASE, QuantConnectClient
from trading_live_claude.scoring.qc_bridge import (
    _parse_float,
    rank_qc_library,
    stats_to_objective_input,
)

# Real-ish LEAN snippets (mirroring the user's actual projects).
BOLLINGER_SRC = """
class A(QCAlgorithm):
    def initialize(self):
        self.TSLA = self.add_equity("TSLA", Resolution.DAILY)
        self.nma = self.sma(self.TSLA.symbol, 30)
        bb = BollingerBands(30, 2)
"""
BUYHOLD_SRC = """
class B(QCAlgorithm):
    def initialize(self):
        self.add_equity("SPY", Resolution.DAILY)
    def on_data(self, data):
        self.market_order("SPY", 100)
"""
CRYPTO_EMA_SRC = """
class C(QCAlgorithm):
    def initialize(self):
        self.add_crypto("BTCUSD", Resolution.DAILY, Market.Coinbase)
        self.fast = self.ema("BTCUSD", 20)
"""


def _client() -> QuantConnectClient:
    return QuantConnectClient("528370", "tok")


# ----- detector units --------------------------------------------------------


def test_detect_indicators() -> None:
    assert detect_indicators(BOLLINGER_SRC) == {"bollinger", "sma"}
    assert detect_indicators(CRYPTO_EMA_SRC) == {"ema"}
    assert detect_indicators(BUYHOLD_SRC) == set()


def test_detect_asset_classes_and_symbols() -> None:
    assert detect_asset_classes(BOLLINGER_SRC) == {"equity"}
    assert detect_asset_classes(CRYPTO_EMA_SRC) == {"crypto"}
    assert detect_symbols(BOLLINGER_SRC) == {"TSLA"}
    assert detect_symbols(CRYPTO_EMA_SRC) == {"BTCUSD"}


def test_family_from_indicators() -> None:
    assert family_from_indicators({"bollinger"}) == "mean_reversion"
    assert family_from_indicators({"ema"}) == "momentum"
    assert family_from_indicators({"atr"}) == "volatility"
    assert family_from_indicators(set()) == "uncategorized"


def test_candlestick_and_std_detection() -> None:
    assert categorize_source("x = self.CandlestickPatterns.Hammer(sym)", "Foo") == "candlestick"
    assert detect_indicators("self.STD(sym, 20)") == {"stddev"}


def test_gap_templates_categorize_to_their_family() -> None:
    from trading_live_claude.integrations.lean_algorithm import gap_family_algorithms
    from trading_live_claude.integrations.qc_library import analyze_source

    for fam, (name, src) in gap_family_algorithms().items():
        a = analyze_source(1, name, {"main.py": src})
        assert a.family == fam, f"{fam} template categorized as {a.family}"


def test_comprehensive_templates_categorize_and_are_tunable() -> None:
    from trading_live_claude.integrations.lean_algorithm import comprehensive_lean_algorithms
    from trading_live_claude.integrations.qc_library import analyze_source

    algos = comprehensive_lean_algorithms()
    families = {fam for _, (fam, _) in algos.items()}
    assert {"momentum", "mean_reversion", "volatility", "seasonality", "candlestick"} <= families
    for name, (fam, src) in algos.items():
        a = analyze_source(1, name, {"main.py": src})
        assert a.family == fam, f"{name}: {a.family} != {fam}"
        assert "GetParameter" in src, f"{name} exposes no tunable knobs"


def test_categorize_source_is_code_first() -> None:
    # Auto-named project, but the CODE reveals mean-reversion.
    assert categorize_source(BOLLINGER_SRC, "Adaptable Light Brown Crocodile") == "mean_reversion"
    # No indicators → fall back to name keywords.
    assert categorize_source(BUYHOLD_SRC, "My Momentum Trend Bot") == "momentum"


def test_analyze_source_record() -> None:
    a = analyze_source(1, "Crypto Bot", {"main.py": CRYPTO_EMA_SRC})
    assert a.family == "momentum"
    assert a.indicators == ("ema",)
    assert a.asset_classes == ("crypto",)
    assert a.symbols == ("BTCUSD",)


# ----- analyze_library (respx) ----------------------------------------------


@respx.mock
def test_analyze_library_pulls_and_detects() -> None:
    respx.post(f"{QC_API_BASE}/projects/read").mock(
        return_value=httpx.Response(
            200, json={"success": True, "projects": [{"projectId": 7, "name": "Random Name"}]}
        )
    )
    respx.post(f"{QC_API_BASE}/files/read").mock(
        return_value=httpx.Response(
            200, json={"success": True, "files": [{"name": "main.py", "content": BOLLINGER_SRC}]}
        )
    )
    lib = analyze_library(_client())
    assert len(lib) == 1
    assert lib[0].family == "mean_reversion"
    assert "bollinger" in lib[0].indicators


# ----- scoring bridge --------------------------------------------------------


def test_parse_float_handles_qc_formats() -> None:
    assert _parse_float("-0.472") == -0.472
    assert _parse_float("24.400%") == 24.4
    assert _parse_float("$1,000.50") == 1000.5
    assert _parse_float("n/a") == 0.0


def test_stats_to_objective_input() -> None:
    oi = stats_to_objective_input({"Sharpe Ratio": "1.5", "Drawdown": "20.000%"})
    assert oi.sharpe == 1.5
    assert oi.max_drawdown == -0.20  # % → negative fraction


@respx.mock
def test_rank_qc_library_scores_by_backtest() -> None:
    respx.post(f"{QC_API_BASE}/projects/read").mock(
        return_value=httpx.Response(
            200, json={"success": True, "projects": [{"projectId": 9, "name": "Bollinger Bot"}]}
        )
    )
    respx.post(f"{QC_API_BASE}/backtests/list").mock(
        return_value=httpx.Response(
            200, json={"success": True, "backtests": [{"backtestId": "b1", "completed": True}]}
        )
    )
    respx.post(f"{QC_API_BASE}/backtests/read").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "backtest": {"statistics": {"Sharpe Ratio": "2.0", "Drawdown": "10.000%"}},
            },
        )
    )
    respx.post(f"{QC_API_BASE}/files/read").mock(
        return_value=httpx.Response(
            200, json={"success": True, "files": [{"name": "main.py", "content": BOLLINGER_SRC}]}
        )
    )
    scores = rank_qc_library(_client(), objective="sharpe_over_dd")
    assert len(scores) == 1
    assert scores[0].sharpe == 2.0
    assert scores[0].family == "mean_reversion"
    assert scores[0].objective_value == 2.0 / 0.10  # sharpe / |dd|
