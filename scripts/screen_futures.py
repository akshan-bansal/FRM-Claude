"""Cross-sectional futures screen with intel-graph cross-reference.

Pulls ~2y daily front-month bars for a fixed universe of commodity, energy, metals,
and grain futures via IB socket (``ib_insync`` / TWS on 7496 by default), computes a
small stack of tech signals (12-month momentum, 20-day mean-reversion z-score,
50/200 trend regime, annualized realized vol), then blends the rank with two
intel-graph reads:

* the current asset-class scalar from ``intel.overlay.IntelOverlay.evaluate`` on the
  most recent overlay snapshot in ``state/intel_overlay.jsonl`` (commodity /
  precious_metals — clamped to [floor, 1.0]);
* an optional theme boost when ``intel.interpret.interpret`` fires a thesis whose
  ``themes`` mention the root's mapped class (e.g. "energy" or "materials").

Output: ``reports/futures_screen.csv`` + ``reports/futures_screen.png``. Nothing about
this screen is an entry signal — the ranks are research aids and the intel component
carries a *research posture*, not a trade instruction (framing per intel/interpret.py).

Not routed through Router — it does not place orders and does not touch state/ (other
than reading intel_overlay.jsonl). Safe to run alongside a paper session or the
autonomous daemon.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[union-attr]
    except Exception:                                                 # pragma: no cover
        pass

from trading_live_claude.intel.interpret import interpret
from trading_live_claude.intel.overlay import IntelOverlay, IntelSnapshot


# ---- universe: root → (exchange, currency, asset_class, intel_domain, theme) ----
# asset_class picks which overlay decision scalar applies (commodity vs precious_metals).
# intel_domain names the WorldMonitor event-acceleration bucket most relevant to the root.
# theme is what interpret.interpret's Thesis.themes call the class (energy / materials).
_UniverseRow = tuple[str, str, str, str, str]

UNIVERSE: dict[str, _UniverseRow] = {
    # Energy / oil & gas
    "CL":  ("NYMEX", "USD", "commodity",       "energy",   "energy"),
    "NG":  ("NYMEX", "USD", "commodity",       "energy",   "energy"),
    "HO":  ("NYMEX", "USD", "commodity",       "energy",   "energy"),
    "RB":  ("NYMEX", "USD", "commodity",       "energy",   "energy"),
    "BZ":  ("NYMEX", "USD", "commodity",       "energy",   "energy"),
    # Metals — precious go to precious_metals scalar; industrial (copper) stays commodity.
    "GC":  ("COMEX", "USD", "precious_metals", "commerce", "materials"),
    "SI":  ("COMEX", "USD", "precious_metals", "commerce", "materials"),
    "PL":  ("NYMEX", "USD", "precious_metals", "commerce", "materials"),
    "PA":  ("NYMEX", "USD", "precious_metals", "commerce", "materials"),
    "HG":  ("COMEX", "USD", "commodity",       "commerce", "materials"),
    # Grains — CBOT open-outcry successor; ib_insync ContFuture resolves front-month.
    "ZC":  ("CBOT",  "USD", "commodity",       "commerce", "materials"),
    "ZS":  ("CBOT",  "USD", "commodity",       "commerce", "materials"),
    "ZW":  ("CBOT",  "USD", "commodity",       "commerce", "materials"),
    "ZL":  ("CBOT",  "USD", "commodity",       "commerce", "materials"),
    "ZM":  ("CBOT",  "USD", "commodity",       "commerce", "materials"),
}


@dataclass
class RootRow:
    """One row of the screen output. Every numeric field can be None when the fetch
    for that root failed — the CSV/PNG code renders 'n/a' for those slots."""

    root: str
    exchange: str
    asset_class: str
    intel_domain: str
    theme: str
    n_bars: int
    last: float | None
    mom_12m: float | None                 # 252d total return
    mom_1m: float | None                  # 21d total return
    z20: float | None                     # (last - mean20) / std20 — mean-rev proxy
    trend_50_200: str                     # "up" / "down" / "n/a" per 50/200 SMA state
    vol_ann: float | None                 # annualized daily stdev
    tech_score: float | None              # z-normalized composite over the universe
    class_scalar: float | None            # from IntelOverlay.evaluate
    theme_boost: float                    # 1.0 baseline, 1.15 if in an active thesis's themes
    intel_score: float | None
    final_score: float | None
    note: str = ""


# ---- data fetch --------------------------------------------------------------


def _fetch_root(ib, root: str, exchange: str, currency: str, *, years: float) -> tuple:
    """Return (dataframe_or_None, note) for one root. Uses ContFuture front-month.

    Handles IB error 200 (no security definition) and empty-bar returns as informational
    'note' rows rather than aborting the whole screen.
    """
    from ib_insync import ContFuture
    import pandas as pd

    c = ContFuture(root, exchange, currency=currency)
    qualified = ib.qualifyContracts(c) or []
    if not qualified or not getattr(qualified[0], "conId", 0):
        return None, "unqualified"
    con = qualified[0]

    # Duration MUST be expressed in years for >365 days. Anything else trips
    # error 321 ("Historical data requests for durations longer than 365 days
    # must be made in years").
    y = max(1, int(math.ceil(years)))
    end_dt = datetime.now(UTC)
    bars = ib.reqHistoricalData(
        con,
        endDateTime=end_dt.strftime("%Y%m%d %H:%M:%S UTC"),
        durationStr=f"{y} Y",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    if not bars:
        return None, "no bars"
    rows = [
        {"date": b.date, "open": float(b.open), "high": float(b.high),
         "low": float(b.low), "close": float(b.close), "volume": int(b.volume)}
        for b in bars
    ]
    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df, ""


# ---- signals ----------------------------------------------------------------


def _compute_signals(df) -> dict[str, float | None]:
    """Compute the small signal stack. All returns are simple; z20 is a mean-rev proxy.

    Returns a dict — a None value on any key means the underlying window didn't have
    enough history (the row still lands in the output CSV, just marked n/a).
    """
    if df is None or len(df) < 30:
        return {"last": None, "mom_12m": None, "mom_1m": None, "z20": None,
                "trend_50_200": "n/a", "vol_ann": None}
    close = df["close"]
    last = float(close.iloc[-1])
    mom_12m = float(close.iloc[-1] / close.iloc[-253] - 1.0) if len(close) >= 253 else None
    mom_1m = float(close.iloc[-1] / close.iloc[-22] - 1.0) if len(close) >= 22 else None
    m20 = float(close.tail(20).mean())
    s20 = float(close.tail(20).std(ddof=0)) or 0.0
    z20 = float((last - m20) / s20) if s20 > 0 else None
    if len(close) >= 200:
        sma50 = float(close.tail(50).mean())
        sma200 = float(close.tail(200).mean())
        trend = "up" if sma50 > sma200 else "down"
    else:
        trend = "n/a"
    rets = close.pct_change().dropna()
    vol_ann = float(rets.std(ddof=0) * math.sqrt(252.0)) if len(rets) >= 30 else None
    return {"last": last, "mom_12m": mom_12m, "mom_1m": mom_1m, "z20": z20,
            "trend_50_200": trend, "vol_ann": vol_ann}


def _rank_z(values: list[float | None]) -> list[float | None]:
    """Cross-sectional z-score. None-in → None-out. Zero variance → all zeros."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2:
        return [0.0 if v is not None else None for v in values]
    mean = sum(xs) / len(xs)
    var = sum((v - mean) ** 2 for v in xs) / len(xs)
    sd = math.sqrt(var) if var > 0 else 0.0
    if sd == 0.0:
        return [0.0 if v is not None else None for v in values]
    return [None if v is None else (v - mean) / sd for v in values]


# ---- intel cross-reference --------------------------------------------------


def _load_latest_snapshot(path: Path) -> IntelSnapshot | None:
    """Load the last IntelSnapshot from state/intel_overlay.jsonl. None if the file is
    missing or empty — the screen still runs, intel component just degrades to 1.0."""
    if not path.exists():
        return None
    last: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            last = json.loads(line)
        except Exception:
            continue
    if last is None:
        return None
    snap = last.get("snapshot") or {}
    as_of_str = last.get("as_of") or snap.get("as_of")
    try:
        as_of = datetime.fromisoformat(as_of_str) if as_of_str else datetime.now(UTC)
    except Exception:
        as_of = datetime.now(UTC)
    return IntelSnapshot(
        global_alert_count=int(snap.get("global_alert_count", 0) or 0),
        global_max_importance=float(snap.get("global_max_importance", 0.0) or 0.0),
        category_alert_counts=dict(snap.get("category_alert_counts") or {}),
        country_alert_counts=dict(snap.get("country_alert_counts") or {}),
        conflict_events_active=int(snap.get("conflict_events_active", 0) or 0),
        natural_disasters_active=int(snap.get("natural_disasters_active", 0) or 0),
        energy_stress=float(snap.get("energy_stress", 0.0) or 0.0),
        strategic_risk=float(snap.get("strategic_risk", 0.0) or 0.0),
        event_acceleration=dict(snap.get("event_acceleration") or {}),
        source_age_hours=dict(snap.get("source_age_hours") or {}),
        fear_greed=snap.get("fear_greed"),
        market=dict(snap.get("market") or {}),
        degraded=bool(snap.get("degraded", False)),
        as_of=as_of,
    )


def _active_theme_set(snap: IntelSnapshot) -> set[str]:
    """The union of themes across current theses. Empty when the tape reads quiet."""
    themes: set[str] = set()
    for th in interpret(snap):
        for t in th.themes:
            themes.add(t)
    return themes


# ---- output -----------------------------------------------------------------


def _fmt(v: float | None, sfx: str = "", digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}{sfx}"


def _write_csv(rows: list[RootRow], path: Path) -> None:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "root", "exchange", "asset_class", "intel_domain",
                     "n_bars", "last", "mom_12m", "mom_1m", "z20",
                     "trend_50_200", "vol_ann_%", "tech_score", "class_scalar",
                     "theme_boost", "intel_score", "final_score", "note"])
        for i, r in enumerate(rows, start=1):
            w.writerow([
                i, r.root, r.exchange, r.asset_class, r.intel_domain, r.n_bars,
                _fmt(r.last, "", 4), _fmt((r.mom_12m or 0) * 100 if r.mom_12m is not None else None, "%"),
                _fmt((r.mom_1m or 0) * 100 if r.mom_1m is not None else None, "%"),
                _fmt(r.z20), r.trend_50_200,
                _fmt((r.vol_ann or 0) * 100 if r.vol_ann is not None else None, "%"),
                _fmt(r.tech_score), _fmt(r.class_scalar),
                _fmt(r.theme_boost), _fmt(r.intel_score),
                _fmt(r.final_score), r.note,
            ])


def _write_png(rows: list[RootRow], path: Path, snap_ts: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    scored = [r for r in rows if r.final_score is not None]
    scored.sort(key=lambda r: r.final_score or 0.0)                   # bottom → top
    if not scored:
        return
    labels = [f"{r.root}  ({r.intel_domain})" for r in scored]
    finals = [r.final_score or 0.0 for r in scored]
    techs = [r.tech_score or 0.0 for r in scored]
    scalars = [(r.class_scalar or 1.0) for r in scored]

    fig, (ax_final, ax_split) = plt.subplots(
        1, 2, figsize=(12, max(4, 0.5 * len(scored))), gridspec_kw={"width_ratios": [3, 2]}
    )
    ax_final.barh(labels, finals,
                    color=["#2b8a3e" if v >= 0 else "#c92a2a" for v in finals])
    ax_final.axvline(0, color="#495057", linewidth=0.8)
    ax_final.set_title("final = tech_z × intel_scalar × theme_boost")
    ax_final.set_xlabel("blended rank score")
    ax_final.grid(True, axis="x", alpha=0.3)

    y = list(range(len(scored)))
    ax_split.barh(y, techs, height=0.4, label="tech_z", color="#1c7ed6", alpha=0.8)
    ax_split.barh([i + 0.4 for i in y], scalars, height=0.4,
                    label="class_scalar", color="#e8590c", alpha=0.8)
    ax_split.set_yticks([i + 0.2 for i in y])
    ax_split.set_yticklabels(labels)
    ax_split.axvline(0, color="#495057", linewidth=0.8)
    ax_split.axvline(1, color="#495057", linewidth=0.5, linestyle="--")
    ax_split.set_title("components (tech_z vs class scalar)")
    ax_split.legend(loc="lower right", fontsize=8)
    ax_split.grid(True, axis="x", alpha=0.3)

    fig.suptitle(f"Futures cross-sectional screen — intel snapshot {snap_ts}",
                  fontsize=11)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


# ---- main -------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7496)
    ap.add_argument("--client-id", type=int, default=44)
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--out-csv", default="reports/futures_screen.csv")
    ap.add_argument("--out-png", default="reports/futures_screen.png")
    ap.add_argument("--overlay-path", default="state/intel_overlay.jsonl")
    ap.add_argument("--only", default="",
                     help="Comma-separated root allow-list — restricts UNIVERSE to just these.")
    ap.add_argument("--tech-weight-mom", type=float, default=0.5,
                     help="Weight of 12m momentum z in the tech composite. 20d z-score gets "
                          "the complementary weight (negated — cheap-vs-mean is a positive.")
    args = ap.parse_args()

    try:
        from ib_insync import IB
    except ImportError:                                              # pragma: no cover
        raise SystemExit("ib_insync not installed — pip install ib_insync")

    universe = dict(UNIVERSE)
    if args.only:
        keep = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        universe = {k: v for k, v in universe.items() if k in keep}
        if not universe:
            raise SystemExit(f"--only filtered every root out. Known: {sorted(UNIVERSE)}")

    # 1. Fetch bars
    print(f"[futures-screen] connecting IB {args.host}:{args.port} (client_id={args.client_id})",
          flush=True)
    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id, readonly=True, timeout=15.0)
    per_root: dict[str, tuple] = {}
    try:
        for root, (exch, ccy, cls, dom, theme) in universe.items():
            try:
                df, note = _fetch_root(ib, root, exch, ccy, years=args.years)
            except Exception as e:                                    # pragma: no cover
                df, note = None, f"fetch error: {e}"
            per_root[root] = (df, note)
            n = 0 if df is None else len(df)
            print(f"[futures-screen] {root:<4}/{exch:<6}  bars={n:<5}  {note}", flush=True)
    finally:
        ib.disconnect()

    # 2. Signals per root
    sig_map: dict[str, dict] = {}
    for root, (df, _note) in per_root.items():
        sig_map[root] = _compute_signals(df)

    # 3. Cross-sectional tech z-score = w * z(mom_12m) + (1-w) * -z(z20)
    #    (cheap-vs-mean is a POSITIVE, hence the sign flip on z20)
    roots = list(universe.keys())
    mom_z = _rank_z([sig_map[r]["mom_12m"] for r in roots])
    mr_z = _rank_z([sig_map[r]["z20"] for r in roots])
    w = max(0.0, min(1.0, args.tech_weight_mom))
    tech: dict[str, float | None] = {}
    for i, r in enumerate(roots):
        m = mom_z[i]
        z = mr_z[i]
        if m is None and z is None:
            tech[r] = None
        else:
            m = 0.0 if m is None else m
            z = 0.0 if z is None else z
            tech[r] = w * m + (1.0 - w) * (-z)

    # 4. Intel cross-reference
    snap = _load_latest_snapshot(Path(args.overlay_path))
    class_scalars: dict[str, float | None] = {r: None for r in roots}
    themes: set[str] = set()
    snap_ts = "n/a"
    if snap is None:
        print("[futures-screen] no overlay snapshot found — intel component degrades to 1.0",
              flush=True)
    else:
        decisions = IntelOverlay().evaluate(snap)
        for r in roots:
            cls = universe[r][2]
            d = decisions.get(cls)                                    # type: ignore[arg-type]
            class_scalars[r] = float(d.scalar) if d else None
        themes = _active_theme_set(snap)
        snap_ts = snap.as_of.isoformat()
        print(f"[futures-screen] intel snapshot {snap_ts}  "
              f"active themes={sorted(themes) or '(quiet)'}", flush=True)

    # 5. Compose final rows + final score
    rows: list[RootRow] = []
    for r in roots:
        exch, ccy, cls, dom, theme = universe[r]
        s = sig_map[r]
        df, note = per_root[r]
        cs = class_scalars[r]
        boost = 1.15 if theme in themes else 1.0
        intel = None if cs is None else float(cs) * boost
        t = tech[r]
        final = None if (t is None or intel is None) else float(t) * float(intel)
        rows.append(RootRow(
            root=r, exchange=exch, asset_class=cls, intel_domain=dom, theme=theme,
            n_bars=0 if df is None else len(df),
            last=s["last"], mom_12m=s["mom_12m"], mom_1m=s["mom_1m"],
            z20=s["z20"], trend_50_200=s["trend_50_200"], vol_ann=s["vol_ann"],
            tech_score=t, class_scalar=cs, theme_boost=boost,
            intel_score=intel, final_score=final, note=note,
        ))

    rows.sort(key=lambda r: (r.final_score is None, -(r.final_score or 0.0)))

    _write_csv(rows, Path(args.out_csv))
    _write_png(rows, Path(args.out_png), snap_ts)

    print("[futures-screen] top 5:", flush=True)
    for r in rows[:5]:
        print(f"    {r.root:>4}  final={_fmt(r.final_score)}  "
              f"tech={_fmt(r.tech_score)}  intel={_fmt(r.intel_score)}  "
              f"mom12m={_fmt((r.mom_12m or 0)*100 if r.mom_12m is not None else None, '%')}  "
              f"trend={r.trend_50_200}", flush=True)
    print(f"[futures-screen] wrote {args.out_csv} and {args.out_png}", flush=True)


if __name__ == "__main__":
    main()
