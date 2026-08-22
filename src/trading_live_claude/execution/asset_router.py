"""Asset-class → brokerage router for LEAN deployments.

Because a single LEAN live deployment binds to one brokerage, trading multiple
asset classes means routing each selected strategy to the deployment whose broker
handles its asset class. This router owns that mapping (config-driven, with sane
defaults) and the per-class LEAN subscription spec used by the algo generator.

It sits *above* LEAN execution and does not touch the Questrade `Router` or its risk
gates — it only decides *which venue* a class deploys to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AssetClass = Literal["equity", "future", "commodity", "crypto"]

ASSET_CLASSES: tuple[AssetClass, ...] = ("equity", "future", "commodity", "crypto")

# Default asset-class → LEAN brokerage id. Override per deployment via config.
DEFAULT_ASSET_BROKERAGE: dict[str, str] = {
    "equity": "InteractiveBrokersBrokerage",
    "future": "InteractiveBrokersBrokerage",
    "commodity": "InteractiveBrokersBrokerage",
    "crypto": "CoinbaseBrokerage",
}

# LEAN subscription spec per class: the `self.Add*` method + optional market arg
# the algo generator emits. Keeps generation and routing in one authoritative place.
ASSET_LEAN_SPEC: dict[str, dict[str, str]] = {
    "equity": {"add": "AddEquity", "market": ""},
    "future": {"add": "AddFuture", "market": ""},
    "commodity": {"add": "AddFuture", "market": ""},
    "crypto": {"add": "AddCrypto", "market": "Market.Coinbase"},
}


@dataclass(frozen=True)
class RouteDecision:
    asset_class: str
    brokerage: str
    add_method: str
    market: str


class AssetRouter:
    """Maps an asset class to the LEAN brokerage + subscription method for it."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        # Start from defaults, apply overrides; validate override keys.
        merged = dict(DEFAULT_ASSET_BROKERAGE)
        for k, v in (mapping or {}).items():
            if k not in ASSET_CLASSES:
                raise ValueError(f"Unknown asset class in brokerage map: {k!r}. Known: {ASSET_CLASSES}")
            merged[k] = v
        self.mapping = merged

    def brokerage_for(self, asset_class: str) -> str:
        if asset_class not in self.mapping:
            raise KeyError(
                f"No brokerage configured for asset class {asset_class!r}. Known: {sorted(self.mapping)}"
            )
        return self.mapping[asset_class]

    def route(self, asset_class: str) -> RouteDecision:
        """Resolve the full deployment target for an asset class."""
        if asset_class not in ASSET_LEAN_SPEC:
            raise KeyError(f"Unsupported asset class {asset_class!r}. Supported: {ASSET_CLASSES}")
        spec = ASSET_LEAN_SPEC[asset_class]
        return RouteDecision(
            asset_class=asset_class,
            brokerage=self.brokerage_for(asset_class),
            add_method=spec["add"],
            market=spec["market"],
        )
