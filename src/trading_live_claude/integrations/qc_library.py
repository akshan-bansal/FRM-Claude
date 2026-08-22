"""Normalize the user's own QuantConnect projects into strategy records.

'Searching the QC library' resolves to reading the projects you have in your QC
account (strategies cloned from the Strategy Library, Boot Camp, your own work),
since Alpha Streams is retired and there is no global-library search API. This
module turns raw ``/projects/read`` payloads into tidy ``QcStrategy`` records and
pulls LEAN source on demand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .quantconnect import QuantConnectClient

# Heuristic name→category keywords, so a pulled library slots into the same family
# taxonomy the scoring layer uses.
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "mean_reversion": ("mean revers", "reversion", "rsi", "bollinger", "zscore", "z-score", "pairs"),
    "momentum": ("momentum", "trend", "breakout", "macd", "crossover", "moving average"),
    "volatility": ("volatilit", "atr", "keltner", "squeeze", "vix", "garch"),
    "seasonality": ("season", "calendar", "turn of month", "day of week", "month of year"),
}

# Indicator detection from LEAN source. LEAN exposes both snake_case helpers
# (self.ema(...)) and PascalCase constructors (ExponentialMovingAverage(...)); each
# pattern matches both and is searched case-insensitively.
_INDICATOR_PATTERNS: dict[str, str] = {
    "ema": r"\bema\s*\(|ExponentialMovingAverage\s*\(",
    "sma": r"\bsma\s*\(|SimpleMovingAverage\s*\(",
    "rsi": r"\brsi\s*\(|RelativeStrengthIndex\s*\(",
    "macd": r"\bmacd\s*\(|MovingAverageConvergenceDivergence\s*\(",
    "bollinger": r"BollingerBands\s*\(|\bbb\s*\(",
    "atr": r"\batr\s*\(|AverageTrueRange\s*\(",
    "momentum": r"\bmomentum\s*\(|MomentumPercent\s*\(|\bmom\s*\(",
    "donchian": r"Donchian\s*\(|Maximum\s*\(|Minimum\s*\(",
    "stddev": r"StandardDeviation\s*\(",
    "stochastic": r"Stochastic\s*\(|\bsto\s*\(",
}

# Asset-class detection from Add* subscription calls.
_ASSET_PATTERNS: dict[str, str] = {
    "equity": r"add_equity\s*\(",
    "crypto": r"add_crypto\s*\(",
    "future": r"add_future\s*\(",
    "forex": r"add_forex\s*\(",
    "option": r"add_option\s*\(",
}

_SYMBOL_RE = re.compile(
    r"add_(?:equity|crypto|future|forex|option)\s*\(\s*[\"']([A-Za-z0-9.\-]+)[\"']",
    re.IGNORECASE,
)


def detect_indicators(source: str) -> set[str]:
    """Return the set of indicators referenced in LEAN source."""
    return {name for name, pat in _INDICATOR_PATTERNS.items() if re.search(pat, source, re.IGNORECASE)}


def detect_asset_classes(source: str) -> set[str]:
    return {name for name, pat in _ASSET_PATTERNS.items() if re.search(pat, source, re.IGNORECASE)}


def detect_symbols(source: str) -> set[str]:
    return {m.upper() for m in _SYMBOL_RE.findall(source)}


def family_from_indicators(indicators: set[str]) -> str:
    """Map a detected indicator set to a strategy family (heuristic, order matters)."""
    if indicators & {"bollinger", "rsi", "stochastic"}:
        return "mean_reversion"
    if indicators & {"ema", "sma", "macd", "momentum", "donchian"}:
        return "momentum"
    if indicators & {"atr", "stddev"}:
        return "volatility"
    return "uncategorized"


def categorize_source(source: str, name: str = "") -> str:
    """Categorize a strategy from its CODE first, falling back to its name.

    Code-first because QC auto-names projects ('Adaptable Light Brown Crocodile'),
    so the source is the reliable signal of what the strategy actually does.
    """
    fam = family_from_indicators(detect_indicators(source))
    if fam != "uncategorized":
        return fam
    return categorize(name)


def _to_int(v: object) -> int:
    """Coerce a JSON value (object-typed) to int, defaulting to 0."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v)
    return 0


def categorize(name: str) -> str:
    """Best-effort family from a project name; 'uncategorized' if nothing matches."""
    low = name.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in low for k in keywords):
            return category
    return "uncategorized"


@dataclass(frozen=True)
class QcStrategy:
    project_id: int
    name: str
    language: str
    category: str
    modified: str


def list_library(client: QuantConnectClient) -> list[QcStrategy]:
    """List the account's projects as categorized ``QcStrategy`` records."""
    out: list[QcStrategy] = []
    for p in client.list_projects():
        name = str(p.get("name", ""))
        out.append(
            QcStrategy(
                project_id=_to_int(p.get("projectId", 0)),
                name=name,
                language=str(p.get("language", "")),
                category=categorize(name),
                modified=str(p.get("modified", "")),
            )
        )
    return out


def pull_algorithm(client: QuantConnectClient, project_id: int) -> dict[str, str]:
    """Return {filename: source} for every file in a project (LEAN source)."""
    files = client.list_files(project_id)
    return {
        str(f.get("name", "")): str(f.get("content", ""))
        for f in files
        if isinstance(f, dict)
    }


@dataclass(frozen=True)
class QcStrategyAnalysis:
    """A QC project enriched with signals detected from its source code."""

    project_id: int
    name: str
    family: str
    indicators: tuple[str, ...] = field(default_factory=tuple)
    asset_classes: tuple[str, ...] = field(default_factory=tuple)
    symbols: tuple[str, ...] = field(default_factory=tuple)


def analyze_source(project_id: int, name: str, files: dict[str, str]) -> QcStrategyAnalysis:
    """Detect indicators/asset-classes/symbols/family from a project's source files.

    Only the algorithm ``.py`` files are analyzed — QC seeds every project with a
    ``research.ipynb`` boilerplate that contains a ``BollingerBands`` example, which
    would otherwise inject a false 'bollinger' signal into every project.
    """
    py_files = {n: c for n, c in files.items() if n.endswith(".py")}
    source = "\n".join((py_files or files).values())
    return QcStrategyAnalysis(
        project_id=project_id,
        name=name,
        family=categorize_source(source, name),
        indicators=tuple(sorted(detect_indicators(source))),
        asset_classes=tuple(sorted(detect_asset_classes(source))),
        symbols=tuple(sorted(detect_symbols(source))),
    )


def analyze_library(client: QuantConnectClient) -> list[QcStrategyAnalysis]:
    """Pull every project's source and return code-analyzed strategy records.

    This is the 'expand from the QC library' path: read what each cloned strategy
    actually computes (indicators, asset class, symbols) rather than trusting names.
    """
    out: list[QcStrategyAnalysis] = []
    for p in client.list_projects():
        pid = _to_int(p.get("projectId", 0))
        name = str(p.get("name", ""))
        out.append(analyze_source(pid, name, pull_algorithm(client, pid)))
    return out
