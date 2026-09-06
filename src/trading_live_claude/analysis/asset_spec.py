"""Per-asset-class type hierarchy — the research-layer equivalent of a broker Contract.

``intel/routing.py::classify_symbol`` returns a string ("equity", "fixed_income", …); that's
enough for the overlay taxonomy but not for anything that needs a bond's duration, a metal's
physical-backed flag, or a future's exchange group + tick size. This module carries those
per-class fields as first-class dataclass types so the sweep / dashboard / risk math can read
``spec.duration_years`` without a lookup and a fresh contributor knows what actually belongs
on each class.

Not a duplicate of :class:`~trading_live_claude.brokers.ib.IBContract` — that's IB-specific
(secType / exchange / expiry / strike) and belongs at the broker adapter layer. AssetSpec is
research-facing: portable across brokers, populated from a canonical registry, and cheap to
query.

Contract:

* :class:`AssetSpec` is the base — every subclass shares ``symbol``, ``asset_class``,
  ``exchange``, ``currency``. All fields are keyword-only (``kw_only=True``) so subclasses
  can add required fields without hitting the "non-default field after default field" trap.
* One subclass per overlay class — :class:`EquitySpec`, :class:`FixedIncomeSpec`,
  :class:`PreciousMetalsSpec`, :class:`CommoditySpec`, :class:`FutureSpec`, :class:`CryptoSpec`,
  :class:`FXSpec`. Each carries the fields that matter for that class.
* :func:`spec_for` is the resolver: symbol → AssetSpec of the right concrete type. Falls back
  to a bare :class:`EquitySpec` when the symbol is unknown, matching ``classify_symbol``'s
  default-to-equity behaviour so a fresh ticker never breaks the caller.

Registries at the bottom of this module carry the known instances. They're intentionally
small — only the symbols the sweep + monitors actually touch. Adding a symbol is a one-line
append; missing symbols fall through to sensible defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from trading_live_claude.intel.overlay import OverlayClass
from trading_live_claude.intel.routing import classify_symbol


CreditTier = Literal["govt", "ig", "hy", "em", "muni", "mbs", "mixed"]
FutureExchangeGroup = Literal["CME", "NYMEX", "COMEX", "CBOT", "CFE", "ICE", "unknown"]
CommoditySubclass = Literal["broad", "energy", "agricultural", "industrial", "livestock"]
Metal = Literal["gold", "silver", "platinum", "palladium", "mixed"]


@dataclass(frozen=True, kw_only=True)
class AssetSpec:
    """Base — what every asset carries.

    ``exchange`` is the venue's own identifier (TSE, LSE, ARCA, GLOBEX, XETRA…), not IB's
    routing symbol. ``currency`` is the trading currency, not the account currency — a Canadian
    ETF trades in CAD even if the account settles USD.
    """
    symbol: str
    asset_class: OverlayClass
    exchange: str = ""
    currency: str = "USD"


@dataclass(frozen=True, kw_only=True)
class EquitySpec(AssetSpec):
    asset_class: OverlayClass = "equity"
    is_etf: bool = False
    sector: str = ""                       # GICS-ish string, informational; empty when unknown


@dataclass(frozen=True, kw_only=True)
class FixedIncomeSpec(AssetSpec):
    """Bond ETF / fund.  ``duration_years`` matters more than any other axis for a bond ETF's
    rate sensitivity — TLT (~17y) and SHY (~1.9y) sit in the same class but move on different
    order-of-magnitude rate shocks.
    """
    asset_class: OverlayClass = "fixed_income"
    duration_years: float = 0.0            # 0 = unknown / broad-aggregate
    credit_tier: CreditTier = "govt"
    hedged: bool = False                   # currency-hedged variant (e.g. ZDB.TO)


@dataclass(frozen=True, kw_only=True)
class PreciousMetalsSpec(AssetSpec):
    asset_class: OverlayClass = "precious_metals"
    metal: Metal = "gold"
    is_physical_backed: bool = True        # SLV/GLD/PSLV yes; miners no (miners are equity anyway)
    hedged: bool = False                   # CGL-C.TO is CAD-hedged; CGL.TO is not


@dataclass(frozen=True, kw_only=True)
class CommoditySpec(AssetSpec):
    """Broad-basket or single-commodity ETFs. Precious metals live in their own class."""
    asset_class: OverlayClass = "commodity"
    subclass: CommoditySubclass = "broad"


@dataclass(frozen=True, kw_only=True)
class FutureSpec(AssetSpec):
    """Futures roots. ``root`` is the IB-style symbol (ES, CL); ``exchange_group`` disambiguates
    the venue (CME vs NYMEX vs COMEX). ``contract_size`` × ``tick_size`` = tick dollar-value.
    """
    asset_class: OverlayClass = "future"
    root: str = ""
    exchange_group: FutureExchangeGroup = "unknown"
    contract_size: float = 0.0
    tick_size: float = 0.0
    tick_value: float = 0.0                # dollars per tick (contract_size × tick_size × mult)


@dataclass(frozen=True, kw_only=True)
class CryptoSpec(AssetSpec):
    """Digital asset. ``base`` and ``quote`` are the pair's legs. Kraken's BASE/QUOTE convention
    is what the sleeve uses; symbol strings elsewhere may be dash-separated or bare base tokens.
    """
    asset_class: OverlayClass = "crypto"
    base: str = ""
    quote: str = "USD"


@dataclass(frozen=True, kw_only=True)
class FXSpec(AssetSpec):
    asset_class: OverlayClass = "fx"
    base: str = ""                         # left leg (buy currency)
    quote: str = ""                        # right leg (sell currency)


# ---- registries -------------------------------------------------------------
# Only the symbols the sweeps + monitors actually touch today. Add rows as new names enter
# the universe; missing symbols fall through to spec_for's defaults.

# Bond ETFs with published effective-duration estimates (rounded to the nearest tenth of a year;
# figures from prospectus / iShares / BMO fund pages, updated for the 2026-09 rate environment).
_BOND_REGISTRY: dict[str, FixedIncomeSpec] = {
    # US treasuries by duration (approximate effective duration)
    "SHY":   FixedIncomeSpec(symbol="SHY",   duration_years=1.9,  credit_tier="govt",  currency="USD", exchange="NASDAQ"),
    "IEI":   FixedIncomeSpec(symbol="IEI",   duration_years=4.4,  credit_tier="govt",  currency="USD", exchange="NASDAQ"),
    "IEF":   FixedIncomeSpec(symbol="IEF",   duration_years=7.6,  credit_tier="govt",  currency="USD", exchange="NASDAQ"),
    "TLH":   FixedIncomeSpec(symbol="TLH",   duration_years=13.7, credit_tier="govt",  currency="USD", exchange="NYSEARCA"),
    "TLT":   FixedIncomeSpec(symbol="TLT",   duration_years=17.0, credit_tier="govt",  currency="USD", exchange="NASDAQ"),
    "GOVT":  FixedIncomeSpec(symbol="GOVT",  duration_years=6.1,  credit_tier="govt",  currency="USD", exchange="NYSEARCA"),
    # US aggregate + mortgage
    "BND":   FixedIncomeSpec(symbol="BND",   duration_years=6.3,  credit_tier="mixed", currency="USD", exchange="NASDAQ"),
    "AGG":   FixedIncomeSpec(symbol="AGG",   duration_years=6.1,  credit_tier="mixed", currency="USD", exchange="NYSEARCA"),
    "MBB":   FixedIncomeSpec(symbol="MBB",   duration_years=5.9,  credit_tier="mbs",   currency="USD", exchange="NYSEARCA"),
    # Corporate + EM
    "LQD":   FixedIncomeSpec(symbol="LQD",   duration_years=8.2,  credit_tier="ig",    currency="USD", exchange="NYSEARCA"),
    "HYG":   FixedIncomeSpec(symbol="HYG",   duration_years=3.5,  credit_tier="hy",    currency="USD", exchange="NYSEARCA"),
    "JNK":   FixedIncomeSpec(symbol="JNK",   duration_years=3.4,  credit_tier="hy",    currency="USD", exchange="NYSEARCA"),
    "EMB":   FixedIncomeSpec(symbol="EMB",   duration_years=7.2,  credit_tier="em",    currency="USD", exchange="NASDAQ"),
    # Municipal
    "MUB":   FixedIncomeSpec(symbol="MUB",   duration_years=6.8,  credit_tier="muni",  currency="USD", exchange="NYSEARCA"),
    "TFI":   FixedIncomeSpec(symbol="TFI",   duration_years=7.0,  credit_tier="muni",  currency="USD", exchange="NYSEARCA"),
    # Canadian bond ETFs — currency is CAD
    "XBB.TO": FixedIncomeSpec(symbol="XBB.TO", duration_years=7.7, credit_tier="mixed", currency="CAD", exchange="TSE"),
    "ZAG.TO": FixedIncomeSpec(symbol="ZAG.TO", duration_years=7.4, credit_tier="mixed", currency="CAD", exchange="TSE"),
    "VAB.TO": FixedIncomeSpec(symbol="VAB.TO", duration_years=7.6, credit_tier="mixed", currency="CAD", exchange="TSE"),
    "ZFL.TO": FixedIncomeSpec(symbol="ZFL.TO", duration_years=17.5, credit_tier="govt", currency="CAD", exchange="TSE"),
    "ZDB.TO": FixedIncomeSpec(symbol="ZDB.TO", duration_years=7.1, credit_tier="mixed", currency="CAD", exchange="TSE", hedged=True),
}

_METALS_REGISTRY: dict[str, PreciousMetalsSpec] = {
    "GLD":   PreciousMetalsSpec(symbol="GLD",   metal="gold",    currency="USD", exchange="NYSEARCA"),
    "IAU":   PreciousMetalsSpec(symbol="IAU",   metal="gold",    currency="USD", exchange="NYSEARCA"),
    "SGOL":  PreciousMetalsSpec(symbol="SGOL",  metal="gold",    currency="USD", exchange="NYSEARCA"),
    "SLV":   PreciousMetalsSpec(symbol="SLV",   metal="silver",  currency="USD", exchange="NYSEARCA"),
    "SIVR":  PreciousMetalsSpec(symbol="SIVR",  metal="silver",  currency="USD", exchange="NYSEARCA"),
    "PSLV":  PreciousMetalsSpec(symbol="PSLV",  metal="silver",  currency="USD", exchange="NYSEARCA"),
    "PPLT":  PreciousMetalsSpec(symbol="PPLT",  metal="platinum", currency="USD", exchange="NYSEARCA"),
    "PALL":  PreciousMetalsSpec(symbol="PALL",  metal="palladium", currency="USD", exchange="NYSEARCA"),
    "CGL.TO":   PreciousMetalsSpec(symbol="CGL.TO",   metal="gold", currency="CAD", exchange="TSE", hedged=False),
    "CGL-C.TO": PreciousMetalsSpec(symbol="CGL-C.TO", metal="gold", currency="CAD", exchange="TSE", hedged=True),
}

_COMMODITY_REGISTRY: dict[str, CommoditySpec] = {
    "USO":   CommoditySpec(symbol="USO",   subclass="energy",       currency="USD", exchange="NYSEARCA"),
    "UNG":   CommoditySpec(symbol="UNG",   subclass="energy",       currency="USD", exchange="NYSEARCA"),
    "DBC":   CommoditySpec(symbol="DBC",   subclass="broad",        currency="USD", exchange="NYSEARCA"),
    "GSG":   CommoditySpec(symbol="GSG",   subclass="broad",        currency="USD", exchange="NYSEARCA"),
    "DBA":   CommoditySpec(symbol="DBA",   subclass="agricultural", currency="USD", exchange="NYSEARCA"),
}

# Futures contract specs — root -> tick+size+value.
_FUTURES_REGISTRY: dict[str, FutureSpec] = {
    # CME equity index
    "ES":  FutureSpec(symbol="ES",  root="ES",  exchange_group="CME",  contract_size=50.0,    tick_size=0.25,   tick_value=12.50, currency="USD"),
    "NQ":  FutureSpec(symbol="NQ",  root="NQ",  exchange_group="CME",  contract_size=20.0,    tick_size=0.25,   tick_value=5.00,  currency="USD"),
    "RTY": FutureSpec(symbol="RTY", root="RTY", exchange_group="CME",  contract_size=50.0,    tick_size=0.10,   tick_value=5.00,  currency="USD"),
    "YM":  FutureSpec(symbol="YM",  root="YM",  exchange_group="CME",  contract_size=5.0,     tick_size=1.0,    tick_value=5.00,  currency="USD"),
    # NYMEX energy
    "CL":  FutureSpec(symbol="CL",  root="CL",  exchange_group="NYMEX", contract_size=1000.0,  tick_size=0.01,   tick_value=10.00, currency="USD"),
    "NG":  FutureSpec(symbol="NG",  root="NG",  exchange_group="NYMEX", contract_size=10000.0, tick_size=0.001,  tick_value=10.00, currency="USD"),
    "HO":  FutureSpec(symbol="HO",  root="HO",  exchange_group="NYMEX", contract_size=42000.0, tick_size=0.0001, tick_value=4.20,  currency="USD"),
    # COMEX metals
    "GC":  FutureSpec(symbol="GC",  root="GC",  exchange_group="COMEX", contract_size=100.0,   tick_size=0.10,   tick_value=10.00, currency="USD"),
    "SI":  FutureSpec(symbol="SI",  root="SI",  exchange_group="COMEX", contract_size=5000.0,  tick_size=0.005,  tick_value=25.00, currency="USD"),
    "HG":  FutureSpec(symbol="HG",  root="HG",  exchange_group="COMEX", contract_size=25000.0, tick_size=0.0005, tick_value=12.50, currency="USD"),
    # CBOT rates (quoted in points and 32nds; tick_value here is dollar-per-tick)
    "ZN":  FutureSpec(symbol="ZN",  root="ZN",  exchange_group="CBOT",  contract_size=100000.0, tick_size=0.015625, tick_value=15.625, currency="USD"),
    "ZB":  FutureSpec(symbol="ZB",  root="ZB",  exchange_group="CBOT",  contract_size=100000.0, tick_size=0.03125,  tick_value=31.25,  currency="USD"),
    "ZF":  FutureSpec(symbol="ZF",  root="ZF",  exchange_group="CBOT",  contract_size=100000.0, tick_size=0.0078125, tick_value=7.8125, currency="USD"),
    # CBOT grains
    "ZC":  FutureSpec(symbol="ZC",  root="ZC",  exchange_group="CBOT",  contract_size=5000.0,  tick_size=0.25,   tick_value=12.50, currency="USD"),
    "ZW":  FutureSpec(symbol="ZW",  root="ZW",  exchange_group="CBOT",  contract_size=5000.0,  tick_size=0.25,   tick_value=12.50, currency="USD"),
    "ZS":  FutureSpec(symbol="ZS",  root="ZS",  exchange_group="CBOT",  contract_size=5000.0,  tick_size=0.25,   tick_value=12.50, currency="USD"),
    # CFE volatility
    "VIX": FutureSpec(symbol="VIX", root="VIX", exchange_group="CFE",   contract_size=1000.0,  tick_size=0.05,   tick_value=50.00, currency="USD"),
}


def _crypto_spec(symbol: str) -> CryptoSpec:
    """Parse a Kraken-style ``BASE/QUOTE`` or bare crypto ticker into a CryptoSpec."""
    up = symbol.upper()
    if "/" in up:
        base, quote = up.split("/", 1)
    elif "-" in up:
        base, quote = up.split("-", 1)
    else:
        base, quote = up, "USD"
    return CryptoSpec(symbol=symbol, base=base, quote=quote, currency=quote if quote != "USDT" else "USD",
                       exchange="KRAKEN")


def _fx_spec(symbol: str) -> FXSpec:
    """Parse a 6-letter FX pair (EURUSD) or slash-notation (EUR/USD) into an FXSpec."""
    up = symbol.upper().replace("/", "")
    if len(up) != 6:
        return FXSpec(symbol=symbol, base=up[:3], quote=up[3:], currency=up[3:] if len(up) >= 6 else "USD")
    return FXSpec(symbol=symbol, base=up[:3], quote=up[3:], currency=up[3:])


def spec_for(symbol: str) -> AssetSpec:
    """Resolve a symbol to its concrete :class:`AssetSpec` subclass.

    Consults the per-class registries first (they carry the class-specific fields — duration,
    metal, contract size, etc.); falls back to a bare spec of the right class when the symbol
    isn't in a registry. Never raises — an unknown symbol lands as :class:`EquitySpec` with
    the default US-equity fields, matching :func:`classify_symbol`'s default-to-equity behaviour.
    """
    up = symbol.strip().upper()
    if up in _BOND_REGISTRY:
        return _BOND_REGISTRY[up]
    if up in _METALS_REGISTRY:
        return _METALS_REGISTRY[up]
    if up in _COMMODITY_REGISTRY:
        return _COMMODITY_REGISTRY[up]
    if up in _FUTURES_REGISTRY:
        return _FUTURES_REGISTRY[up]

    cls = classify_symbol(symbol)
    if cls == "crypto":
        return _crypto_spec(symbol)
    if cls == "fx":
        return _fx_spec(symbol)
    if cls == "future":
        return FutureSpec(symbol=symbol, root=symbol.lstrip("/"), exchange_group="unknown")
    if cls == "fixed_income":
        return FixedIncomeSpec(symbol=symbol)
    if cls == "precious_metals":
        return PreciousMetalsSpec(symbol=symbol)
    if cls == "commodity":
        return CommoditySpec(symbol=symbol)
    # Default: equity, US-listed, USD. is_etf inferred from a small suffix heuristic when the
    # ticker happens to match a known ETF family; unknown = False.
    _ETF_SEEDS = {"SPY", "QQQ", "IWM", "DIA", "VTI", "VOO", "VEA", "VWO", "EFA", "EEM", "XIC.TO"}
    return EquitySpec(symbol=symbol, is_etf=up in _ETF_SEEDS)


__all__ = [
    "AssetSpec", "EquitySpec", "FixedIncomeSpec", "PreciousMetalsSpec",
    "CommoditySpec", "FutureSpec", "CryptoSpec", "FXSpec",
    "CreditTier", "FutureExchangeGroup", "CommoditySubclass", "Metal",
    "spec_for",
]
