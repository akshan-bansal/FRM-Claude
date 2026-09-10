"""Futures expiration calendar — the enumeration layer for the continuous-contract pipeline.

Given a root symbol, this module can produce the list of expected historical expirations over a
lookback window. That list is what :mod:`ib_futures_history` iterates on to pull per-contract bars,
and what :mod:`futures_continuous` splices into a continuous back-adjusted series.

Two things to know:

1. **The calendar itself is data, not a schedule fetch.** IB's ``/trsrv/futures`` endpoint returns
   only currently-listed (unexpired) contracts, so past expirations must be enumerated locally
   from each root's known cycle. Roots trade on either a quarterly cycle (Mar/Jun/Sep/Dec — the
   HMUZ months — for financials, rates, grains, most metals) or a serial-monthly cycle (every
   calendar month for energies and VIX). One row in :data:`ROOT_CALENDARS` per root captures its
   cycle, its exchange group, and the ``active_days`` window over which its front-month bars are
   the truly liquid ones (the tail before that is illiquid; after that another contract has taken
   the lead).

2. **The third-Friday convention.** All CME / CBOT / NYMEX / COMEX futures cash-settle or expire
   around the third Friday of the delivery month. This module uses that as the expiration proxy;
   any per-contract precision beyond the day of the third Friday isn't needed for the roll rule,
   which triggers ``N days before`` on a bar-count basis.

Deliberately no I/O in this module — pure functions returning dataclasses. The IB round-trips
happen in ``ib_futures_history``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

FutureExchangeGroup = Literal["CME", "NYMEX", "COMEX", "CBOT", "CFE"]
Cycle = Literal["quarterly", "monthly"]

# Month codes — the futures industry standard. Only H/M/U/Z appear in the quarterly cycle.
_MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
                7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
_QUARTERLY_MONTHS = (3, 6, 9, 12)


@dataclass(frozen=True)
class RootCalendar:
    """Per-root cycle + splice policy. Populated in :data:`ROOT_CALENDARS`."""
    root: str
    exchange_group: FutureExchangeGroup
    cycle: Cycle
    # Bars considered "active" for this contract — how far back from expiry we take the contract's
    # bars into the continuous series. Quarterly financials use ~90 (the contract is liquid for
    # its whole 3-month window before expiry); serial-monthly energies use ~30; VIX uses ~40
    # because contracts extend ~7 months and are liquid roughly the last 40 days.
    active_days: int
    # Days before expiration when the ROLL happens. Front-month bars stop being taken from THIS
    # contract at (expiration - roll_lead_days) and continue from the NEXT contract. Financials
    # roll ~5 days before, energies ~8 (settlement window), VIX ~3 (last-minute rebalance).
    roll_lead_days: int


@dataclass(frozen=True)
class ContractSpec:
    """One historical contract identifier — root plus its expiration month/year plus expected
    liquid window. ``month_code`` is the standard F-Z letter for that delivery month."""
    root: str
    year: int
    month: int
    month_code: str
    expiration_approx: date          # third-Friday proxy
    active_start: date               # expiration - active_days
    active_end: date                 # expiration - 1 day (last day we take bars from THIS contract)
    roll_out_date: date              # expiration - roll_lead_days (when this contract stops being front)

    @property
    def local_symbol(self) -> str:
        """IB local-symbol style: ES + Z + last-digit-of-year (e.g. ESZ6 for Dec 2026).

        IB has multiple conventions; ``ROOT + MONTHCODE + YEAR`` is the form the API's search
        endpoint accepts when combined with ``secType=FUT`` and ``month=YYYYMM``.
        """
        return f"{self.root}{self.month_code}{self.year % 10}"


# The canonical registry. Add a row per root; the enumeration + fetch code is generic across all.
# ``active_days`` and ``roll_lead_days`` were sourced from CME / ICE / CFE published contract
# specifications and cross-checked against public quant-finance splice conventions (e.g. Chan,
# "Quantitative Trading"). Not derived from data — these are conventions.
ROOT_CALENDARS: dict[str, RootCalendar] = {
    # CME equity index — quarterly HMUZ
    "ES":  RootCalendar(root="ES",  exchange_group="CME",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "NQ":  RootCalendar(root="NQ",  exchange_group="CME",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "RTY": RootCalendar(root="RTY", exchange_group="CME",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "YM":  RootCalendar(root="YM",  exchange_group="CME",  cycle="quarterly", active_days=90, roll_lead_days=5),
    # NYMEX energy — serial monthly, each contract liquid ~30 days before expiration
    "CL":  RootCalendar(root="CL",  exchange_group="NYMEX", cycle="monthly", active_days=30, roll_lead_days=8),
    "NG":  RootCalendar(root="NG",  exchange_group="NYMEX", cycle="monthly", active_days=30, roll_lead_days=8),
    "HO":  RootCalendar(root="HO",  exchange_group="NYMEX", cycle="monthly", active_days=30, roll_lead_days=8),
    # COMEX metals — quarterly-ish for GC, effectively bi-monthly for HG. Both treated as
    # quarterly with a longer active window for smoothness.
    "GC":  RootCalendar(root="GC",  exchange_group="COMEX", cycle="quarterly", active_days=90, roll_lead_days=5),
    "SI":  RootCalendar(root="SI",  exchange_group="COMEX", cycle="quarterly", active_days=90, roll_lead_days=5),
    "HG":  RootCalendar(root="HG",  exchange_group="COMEX", cycle="quarterly", active_days=90, roll_lead_days=5),
    # CBOT rates — quarterly HMUZ, standard 5-day roll
    "ZN":  RootCalendar(root="ZN",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "ZB":  RootCalendar(root="ZB",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "ZF":  RootCalendar(root="ZF",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    # CBOT grains — quarterly proper (Mar/May/Jul/Sep/Dec is the actual cycle for corn/beans/
    # wheat but we treat as HMUZ for the pipeline; the extra May/Jul contracts are ignored, which
    # captures the ~90% of the price series that trades on the quarterly).
    "ZC":  RootCalendar(root="ZC",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "ZW":  RootCalendar(root="ZW",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    "ZS":  RootCalendar(root="ZS",  exchange_group="CBOT",  cycle="quarterly", active_days=90, roll_lead_days=5),
    # CFE volatility — serial monthly. Contracts extend 6-9 months out but the tradeable window
    # per contract is roughly the last 40 days.
    "VIX": RootCalendar(root="VIX", exchange_group="CFE",   cycle="monthly", active_days=40, roll_lead_days=3),
}


def _third_friday(year: int, month: int) -> date:
    """The industry-standard third-Friday-of-the-month expiration proxy for futures / options."""
    d = date(year, month, 1)
    # weekday(): Mon=0 .. Fri=4. First Friday offset:
    first_friday_offset = (4 - d.weekday()) % 7
    return d + timedelta(days=first_friday_offset + 14)


def enumerate_contracts(root: str, *, lookback_years: float = 6.0,
                          today: date | None = None) -> list[ContractSpec]:
    """Every historical + current contract for ``root`` within the lookback window.

    Newest-first. The returned list is meant to feed :mod:`ib_futures_history`, which fetches bars
    for each entry, then :mod:`futures_continuous`, which splices them. Contracts whose
    ``active_end`` is before the lookback cutoff are excluded — they'd contribute bars we can't
    use.
    """
    cal = ROOT_CALENDARS.get(root.upper())
    if cal is None:
        raise KeyError(f"unknown root {root!r} — add to ROOT_CALENDARS")

    ref = today or date.today()
    cutoff = ref - timedelta(days=int(lookback_years * 365))
    out: list[ContractSpec] = []

    months = _QUARTERLY_MONTHS if cal.cycle == "quarterly" else tuple(range(1, 13))
    # Walk from ref year backwards. Include one year forward in case current contract is next-year.
    for year in range(ref.year + 1, ref.year - int(lookback_years) - 2, -1):
        for month in sorted(months, reverse=True):
            exp = _third_friday(year, month)
            active_start = exp - timedelta(days=cal.active_days)
            active_end = exp - timedelta(days=1)
            roll_out = exp - timedelta(days=cal.roll_lead_days)
            if active_end < cutoff:
                continue
            if active_start > ref + timedelta(days=90):
                # Skip contracts whose active window is entirely more than 90 days in the future —
                # they haven't started trading meaningfully yet.
                continue
            out.append(ContractSpec(
                root=cal.root, year=year, month=month, month_code=_MONTH_CODES[month],
                expiration_approx=exp, active_start=active_start,
                active_end=active_end, roll_out_date=roll_out,
            ))
    # Newest-first for iteration; the splice pass will reverse when it walks history.
    out.sort(key=lambda c: c.expiration_approx, reverse=True)
    return out


def calendar_for(root: str) -> RootCalendar:
    """The registered calendar for ``root``. Raises with an actionable message on an unknown root."""
    cal = ROOT_CALENDARS.get(root.upper())
    if cal is None:
        raise KeyError(f"unknown futures root {root!r} — add to futures_calendar.ROOT_CALENDARS")
    return cal


__all__ = [
    "ROOT_CALENDARS",
    "RootCalendar",
    "ContractSpec",
    "FutureExchangeGroup",
    "Cycle",
    "enumerate_contracts",
    "calendar_for",
]
