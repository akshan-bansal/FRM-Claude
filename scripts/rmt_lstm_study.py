"""RMT eigen-embedding (6D, volume-weighted eig1) -> LSTM-Transformer (SGD + momentum) study.

Research only. Places no orders, touches no broker, reads no secrets.

For one universe (``crypto`` from Kraken's public OHLC endpoint, ``equities`` from the local
candle cache) this runs three experiments on a single chronological train/test split:

  A. Diversifier effect size. The basket is fitted with and without its diversifier groups
     (crypto: privacy = XMR, ZEC; commodity = PAXG. equities: commodity = GLD, SLV, DBA — there is
     no equity analogue of a privacy coin). Two outcomes, each as a paired daily difference with a
     Newey-West HAC t-stat and Cohen's d_z:
       * forecast loss of the LSTM-Transformer on the assets common to both baskets
         (seed-averaged per-day MSE, ``--seeds`` training seeds per configuration);
       * realized variance of the out-of-sample minimum-variance portfolio built from the
         MP-denoised correlation.
  B. Lower Marchenko-Pastur boundary x noise shape sweep. The lower cut moves from the
     MP-consistent edge ``lambda_- - 2 TW`` (all below-band eigenvalues kept as structure) down to
     0 (all removed as noise), crossed with the noise-spectrum shapes constant / mp / uniform /
     arcsine / binomial. Scored by in-sample spectral SNR and two out-of-sample checks: Gaussian
     NLL of test returns and min-variance portfolio volatility.
  C. 6D embedding vs raw returns: the same model with and without the eigen-factor features.

Features per bar t: train-standardized asset returns + projections onto the top 6 directions
(eig1 tilted by training-window dollar volume), all through frozen train-fitted eigenvectors.
Target: standardized returns at t+1. Denoising only changes eigenVALUES, never eigenvectors, so
experiment B cannot move the LSTM features — it is scored on the correlation matrix itself.

Usage::

    python scripts/rmt_lstm_study.py --universe crypto
    python scripts/rmt_lstm_study.py --universe equities --seeds 5
"""
from __future__ import annotations

import argparse
import glob
import json
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

from trading_live_claude.analysis.effect_size import PairedEffect, paired_effect
from trading_live_claude.analysis.rmt import (
    NOISE_SHAPES,
    RMTResult,
    fit_rmt,
    gaussian_nll,
    marchenko_pastur_pdf,
    min_variance_weights,
    volume_weights,
)
from trading_live_claude.data.kraken_ohlc import kraken_ohlc
from trading_live_claude.models.lstm_transformer import TrainConfig, make_windows, predict, train

# dataviz reference palette (light surface); categorical slots in fixed order
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4")
BLUE, ORANGE, NEUTRAL = SERIES[0], SERIES[1], "#b4b3ad"
DIVERGING = LinearSegmentedColormap.from_list("blue_gray_red", ["#184f95", "#f0efec", "#b3261e"])

K_DEEP = 6
LOWER_FRACS = (1.0, 0.75, 0.5, 0.25, 0.0)


# ---- universes ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Universe:
    name: str
    core: tuple[str, ...]
    groups: dict[str, tuple[str, ...]]   # diversifier groups
    periods_per_year: int

    @property
    def all_symbols(self) -> tuple[str, ...]:
        return self.core + tuple(s for g in self.groups.values() for s in g)

    def variants(self) -> dict[str, tuple[str, ...]]:
        """'with all' first (the reference), then each group removed, then all removed."""
        out = {"with diversifiers": self.all_symbols}
        if len(self.groups) > 1:
            for g, syms in self.groups.items():
                out[f"without {g}"] = tuple(s for s in self.all_symbols if s not in syms)
        out["without diversifiers"] = self.core
        return out


CRYPTO = Universe(
    "crypto",
    core=("BTC", "ETH", "SOL", "ADA", "XRP", "XLM", "LINK", "AAVE", "UNI", "POL", "LTC", "DOT",
          "AVAX", "ATOM", "ALGO", "DOGE"),
    groups={"privacy": ("XMR", "ZEC"), "commodity": ("PAXG",)},
    periods_per_year=365,
)
EQUITIES = Universe(
    "equities",
    core=(*(f"{s}.TO" for s in (
        "ABX", "AEM", "ARX", "ATD", "BCE", "BIR", "BNS", "BTE", "BTO", "CNQ", "CNR", "EFN", "EMA",
        "ENB", "FRU", "FTS", "GEI", "KEY", "L", "MFC", "NTR", "OTEX", "RY", "SHOP", "SLF", "SRU.UN",
        "SU", "T", "TA", "TD", "WCN", "WCP")), "MSFT", "RS"),
    groups={"commodity": ("GLD", "SLV", "DBA")},
    periods_per_year=252,
)


def load_crypto(u: Universe, sleep_s: float = 1.05) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes, dvol = {}, {}
    for i, sym in enumerate(u.all_symbols):
        if i:
            time.sleep(sleep_s)  # Kraken public tier ~1 req/s
        df = kraken_ohlc(("XBT" if sym == "BTC" else sym) + "USD").set_index("time")
        closes[sym], dvol[sym] = df["close"], df["close"] * df["volume"]
    c = pd.concat(closes, axis=1, join="inner").sort_index().iloc[:-1]  # drop still-forming bar
    v = pd.concat(dvol, axis=1).reindex(c.index)
    return c, v


def _cached_daily(symbol: str, root: Path) -> pd.DataFrame:
    """Union of cache fragments, keeping only true daily bars (stamped 00:00 America/New_York).
    Rows stamped at fetch time (00:xx UTC with fractional seconds, 13-21 UTC) are snapshots of a
    partial or prior bar and disagree with the daily bar by up to 8% (AAPL) — they are dropped."""
    safe = symbol.replace(".", "_").replace("/", "_")
    frames = []
    for p in glob.glob(str(root / f"{safe}_1d_*.parquet")):
        try:
            frames.append(pd.read_parquet(p))
        except Exception:  # truncated fragments exist in the cache; skip them
            continue
    if not frames:
        raise FileNotFoundError(f"no cached daily bars for {symbol}")
    df = pd.concat(frames)
    ny = df["time"].dt.tz_convert("America/New_York")
    keep = (ny.dt.hour == 0) & (ny.dt.minute == 0) & (ny.dt.second == 0) & (ny.dt.microsecond == 0)
    df = df[keep].assign(date=ny[keep].dt.normalize().dt.tz_localize(None))
    return df.sort_values("time").groupby("date")[["close", "volume"]].last()


def load_equities(u: Universe, root: Path = Path("data/cache")) -> tuple[pd.DataFrame, pd.DataFrame]:
    bars = {s: _cached_daily(s, root) for s in u.all_symbols}
    c = pd.concat({s: b["close"] for s, b in bars.items()}, axis=1, join="inner").sort_index()
    v = pd.concat({s: b["close"] * b["volume"] for s, b in bars.items()}, axis=1).reindex(c.index)
    fx = pd.read_parquet(root / "USDCAD_daily.parquet")
    usdcad = fx.set_index(fx["time"].dt.tz_localize(None).dt.normalize())["close"]
    usdcad = usdcad.reindex(c.index, method="ffill").bfill()
    tsx = [s for s in v.columns if s.endswith(".TO")]
    v[tsx] = v[tsx].div(usdcad, axis=0)  # CAD dollar volume -> USD so weights are comparable
    return c, v


# ---- plotting ----------------------------------------------------------------------------------

def _style(ax: plt.Axes) -> None:
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)


def _save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)


def plot_spectrum(r: RMTResult, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(11, 4.5), facecolor=SURFACE)
    _style(ax)
    edge = r.lower_edge()
    noise = r.eigenvalues[r.eigenvalues <= r.threshold]
    signal = r.eigenvalues[r.eigenvalues > r.threshold]
    bins = np.linspace(0, max(r.lambda_plus * 1.3, noise.max() * 1.1), 40)
    ax.hist(noise, bins=bins, density=True, color=NEUTRAL, edgecolor=SURFACE, linewidth=1,
            label=f"eigenvalues at or below the signal cut ({len(noise)})")
    xs = np.linspace(r.lambda_minus, r.lambda_plus, 400)
    ax.plot(xs, marchenko_pastur_pdf(xs, r.q, r.sigma2), color=ORANGE, linewidth=2,
            label=f"Marchenko-Pastur (q={r.q:.3f}, sigma2={r.sigma2:.3f})")
    ax.axvline(edge, color=INK2, linestyle="-.", linewidth=1,
               label=f"lower edge, lambda- - 2 TW = {edge:.3f} ({int((r.eigenvalues < edge).sum())} below)")
    ax.axvline(r.lambda_plus, color=INK2, linestyle="--", linewidth=1,
               label=f"lambda+ = {r.lambda_plus:.3f}")
    ax.axvline(r.threshold, color=INK2, linestyle=":", linewidth=1,
               label=f"signal cut, lambda+ + 2 TW = {r.threshold:.3f}")
    shown = signal[signal <= bins[-1]]
    if len(shown):
        ax.vlines(shown, 0, ax.get_ylim()[1] * 0.3, color=BLUE, linewidth=2, label="signal (in view)")
    off = ", ".join(f"{v:.2f}" for v in signal[signal > bins[-1]])
    ax.set_title(f"Eigenvalue spectrum vs MP law: {len(signal)} signal eigenvalue(s) above cut"
                 + (f"; off-axis: {off}" if off else ""), color=INK, fontsize=10, loc="left")
    ax.set_xlabel("eigenvalue", color=INK2)
    ax.set_ylabel("density", color=INK2)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    _save(fig, path)


def _axis_names(r: RMTResult, k: int) -> list[str]:
    var = r.mode_variances(k)
    names = []
    for i in range(k):
        tag = "signal" if i < r.n_signal else "noise band"
        vw = ", vol-weighted" if i == 0 and r.eig1_weights is not None else ""
        names.append(f"eig{i + 1} (var {var[i]:.2f}, {tag}{vw})")
    return names


def plot_embedding_3d(r: RMTResult, path: Path) -> None:
    coords = r.embed(3)
    fig = plt.figure(figsize=(8, 7), facecolor=SURFACE)
    ax = fig.add_subplot(projection="3d")
    ax.set_facecolor(SURFACE)
    ax.scatter(coords[:, 0], coords[:, 1], coords[:, 2], s=48, color=BLUE,
               edgecolor=SURFACE, linewidth=1.5, depthshade=False)
    for (x, y, z), name in zip(coords, r.symbols, strict=True):
        ax.text(x, y, z, f" {name}", fontsize=7, color=INK)
    n0, n1, n2 = _axis_names(r, 3)
    ax.set_xlabel(n0, fontsize=7, color=INK2)
    ax.set_ylabel(n1, fontsize=7, color=INK2)
    ax.set_zlabel(n2, fontsize=7, color=INK2)
    ax.tick_params(labelsize=7, colors=INK2)
    ax.set_title("First 3 of 6 eigen-coordinates (loading x sqrt(variance))",
                 color=INK, fontsize=10, loc="left")
    _save(fig, path)


def plot_embedding_6d(r: RMTResult, path: Path) -> None:
    """Lower-triangle pairs plot of the 6D embedding; each panel labels its 3 outermost assets."""
    coords = r.embed(K_DEEP)
    k = coords.shape[1]
    names = _axis_names(r, k)
    fig, axes = plt.subplots(k - 1, k - 1, figsize=(15, 15), facecolor=SURFACE)
    for row in range(1, k):
        for col in range(k - 1):
            ax = axes[row - 1, col]
            if col >= row:
                ax.axis("off")
                continue
            _style(ax)
            x, y = coords[:, col], coords[:, row]
            ax.scatter(x, y, s=18, color=BLUE, edgecolor=SURFACE, linewidth=0.8)
            far = np.argsort(np.hypot(x - x.mean(), y - y.mean()))[-3:]
            for i in far:
                ax.annotate(r.symbols[i], (x[i], y[i]), fontsize=7, color=INK,
                            xytext=(3, 3), textcoords="offset points")
            if row == k - 1:
                ax.set_xlabel(names[col], fontsize=7, color=INK2)
            if col == 0:
                ax.set_ylabel(names[row], fontsize=7, color=INK2)
    fig.suptitle("6D eigen-embedding, pairwise (loading x sqrt(variance)); 3 outermost assets "
                 "labelled per panel", x=0.02, ha="left", color=INK, fontsize=11)
    fig.tight_layout()
    _save(fig, path)


def plot_corr(r: RMTResult, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 6.5), facecolor=SURFACE)
    for ax, mat, title in ((axes[0], r.raw_corr, "Raw sample correlation (train)"),
                           (axes[1], r.clean_corr, "MP-denoised correlation (train)")):
        im = ax.imshow(mat, cmap=DIVERGING, vmin=-1, vmax=1)
        fs = 8 if len(r.symbols) <= 20 else 6
        ax.set_xticks(range(len(r.symbols)), r.symbols, rotation=90, fontsize=fs, color=INK2)
        ax.set_yticks(range(len(r.symbols)), r.symbols, fontsize=fs, color=INK2)
        ax.set_title(title, color=INK, fontsize=10, loc="left")
        for s in ax.spines.values():
            s.set_visible(False)
    cb = fig.colorbar(im, ax=axes, shrink=0.8)
    cb.ax.tick_params(labelsize=8, colors=INK2)
    _save(fig, path)


def plot_sweep(sweep: pd.DataFrame, raw_nll: float, raw_vol: float, path: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(18, 4.8), facecolor=SURFACE)
    panels = (("spectral_snr", "in-sample spectral SNR (identical for every shape)", None),
              ("oos_nll", "OOS Gaussian NLL per asset (lower = better)", raw_nll),
              ("oos_minvar_vol", "OOS min-variance portfolio vol, annualized (lower = better)",
               raw_vol))
    for j, (ax, (col, title, ref)) in enumerate(zip(axes, panels, strict=True)):
        _style(ax)
        shapes = NOISE_SHAPES if j else ("constant",)
        for color, shape in zip(SERIES, shapes, strict=False):
            d = sweep[sweep["shape"] == shape].sort_values("lower_frac", ascending=False)
            ax.plot(d["lower_frac"], d[col], color=color if j else INK2, linewidth=2, marker="o",
                    markersize=5, label=shape)
        if ref is not None:
            ax.axhline(ref, color=INK2, linestyle="--", linewidth=1, label="raw sample correlation")
        ax.set_xlim(1.05, -0.05)
        ax.set_xlabel("lower cut as fraction of MP lower edge (1 = keep below-band, 0 = remove all)",
                      color=INK2, fontsize=8)
        ax.set_title(title, color=INK, fontsize=9, loc="left")
    axes[2].legend(frameon=False, fontsize=8, labelcolor=INK, loc="upper left",
                   bbox_to_anchor=(1.02, 1.0), title="noise shape", title_fontsize=8)
    fig.tight_layout()
    _save(fig, path)


def plot_effects(effects: dict[str, dict[str, PairedEffect]], path: Path) -> None:
    kinds = list(next(iter(effects.values())).keys())
    labels = list(effects)
    fig, axes = plt.subplots(len(kinds), 1, figsize=(10, (1.1 * len(labels) + 1.2) * len(kinds)),
                             facecolor=SURFACE)
    for ax, kind in zip(np.atleast_1d(axes), kinds, strict=True):
        _style(ax)
        vals = [effects[lab][kind] for lab in labels]
        scale = 10 ** np.floor(np.log10(max(max(abs(v) for v in e.ci95()) for e in vals) or 1.0))
        for i, e in enumerate(vals):
            lo, hi = (b / scale for b in e.ci95())
            ax.plot([lo, hi], [i, i], color=BLUE, linewidth=2)
            ax.plot([e.mean_diff / scale], [i], "o", color=BLUE, markersize=8,
                    markeredgecolor=SURFACE)
            ax.annotate(f"d_z = {e.cohens_dz:+.3f}   t = {e.t_stat:+.2f}   p = {e.p_value:.3f}",
                        (e.mean_diff / scale, i), xytext=(0, 9), textcoords="offset points",
                        ha="center", fontsize=8, color=INK)
        ax.axvline(0, color=INK2, linewidth=1)
        ax.set_yticks(range(len(labels)), labels, fontsize=8, color=INK2)
        ax.set_ylim(-0.6, len(labels) - 0.2)
        ax.margins(x=0.15)
        ax.set_title(kind, color=INK, fontsize=9, loc="left")
        ax.set_xlabel(f"mean paired daily difference (x {scale:.0e}), 95% HAC CI;  "
                      ">0 = the removed assets were helping", color=INK2, fontsize=8)
    fig.tight_layout()
    _save(fig, path)


def replot(out: Path) -> None:
    """Redraw the effect-size and sweep charts from a finished run's saved outputs."""
    summ = json.loads((out / "summary.json").read_text())
    effects = {
        lab: {kind: PairedEffect(v["mean_diff"], (v["ci95_hi"] - v["ci95_lo"]) / 3.92, v["t_stat"],
                                 v["p_value"], v["cohens_dz"], v["n_days"], v["hac_lags"])
              for kind, v in kinds.items()}
        for lab, kinds in summ["effects"].items()
    }
    plot_effects(effects, out / "diversifier_effect_sizes.png")
    ref = summ["structure"]["with diversifiers"]
    plot_sweep(pd.read_csv(out / "lower_boundary_sweep.csv"), ref["raw_nll"],
               ref["raw_minvar_vol"], out / "lower_boundary_sweep.png")


def plot_training(histories: dict[str, tuple[list[float], list[float], int]], path: Path) -> None:
    n = len(histories)
    fig, axes = plt.subplots(1, n, figsize=(5 * n, 3.8), facecolor=SURFACE, sharey=True)
    for ax, (name, (tr, va, best)) in zip(np.atleast_1d(axes), histories.items(), strict=True):
        _style(ax)
        ax.plot(tr, color=BLUE, linewidth=2, label="train")
        ax.plot(va, color=ORANGE, linewidth=2, label="validation")
        ax.axvline(best, color=INK2, linestyle="--", linewidth=1)
        ax.set_title(f"{name} (seed 0, best epoch {best})", color=INK, fontsize=9, loc="left")
        ax.set_xlabel("epoch", color=INK2)
        ax.legend(frameon=False, fontsize=8, labelcolor=INK)
    np.atleast_1d(axes)[0].set_ylabel("MSE (standardized returns)", color=INK2)
    fig.tight_layout()
    _save(fig, path)


# ---- evaluation --------------------------------------------------------------------------------

def forecast_metrics(pred: np.ndarray, y: np.ndarray) -> dict[str, float]:
    mse = float(((pred - y) ** 2).mean())
    mse_zero = float((y ** 2).mean())
    ics = pd.DataFrame(pred.T).rank().corrwith(pd.DataFrame(y.T).rank()).to_numpy()
    ics = ics[np.isfinite(ics)]
    ic = float(ics.mean())
    return {"mse": mse, "r2_vs_zero": 1 - mse / mse_zero, "rank_ic": ic,
            "ic_t": float(ic / (ics.std(ddof=1) / np.sqrt(len(ics)))),
            "hit_rate": float((np.sign(pred) == np.sign(y)).mean())}


def matrix_scores(r: RMTResult, test: pd.DataFrame, ppy: int) -> dict[str, float | np.ndarray]:
    z = (test.to_numpy() - r.mean) / r.std
    n = len(r.symbols)
    out: dict[str, float | np.ndarray] = {}
    for tag, c in (("clean", r.clean_corr), ("raw", r.raw_corr)):
        w = min_variance_weights(c, r.std)
        port = test.to_numpy() @ w
        out[f"{tag}_nll"] = float(gaussian_nll(c, z).mean() / n)
        out[f"{tag}_minvar_vol"] = float(port.std(ddof=1) * np.sqrt(ppy))
        out[f"{tag}_minvar_sq"] = port ** 2
    return out


@dataclass
class ConfigRun:
    preds: list[np.ndarray]
    y: np.ndarray
    metrics: list[dict[str, float]]
    history0: tuple[list[float], list[float], int]


def run_model(feats: np.ndarray, z_all: np.ndarray, n_train: int, cfg: TrainConfig, seeds: int,
              val_frac: float) -> ConfigRun:
    x, y = make_windows(feats, z_all, cfg.lookback)
    n_tr = n_train - cfg.lookback           # window i targets row i+lookback
    n_val = int(n_tr * val_frac)
    fit, val, test = slice(0, n_tr - n_val), slice(n_tr - n_val, n_tr), slice(n_tr, None)
    preds, metrics, hist0 = [], [], ([], [], 0)
    for s in range(seeds):
        res = train(x[fit], y[fit], x[val], y[val], TrainConfig(**{**asdict(cfg), "seed": s}))
        p = predict(res.model, x[test])
        preds.append(p)
        metrics.append({**forecast_metrics(p, y[test]), "best_epoch": res.best_epoch})
        if s == 0:
            hist0 = (res.train_loss, res.val_loss, res.best_epoch)
    return ConfigRun(preds, y[test], metrics, hist0)


def _fmt(e: PairedEffect) -> dict[str, float]:
    lo, hi = e.ci95()
    return {"mean_diff": e.mean_diff, "ci95_lo": lo, "ci95_hi": hi, "t_stat": e.t_stat,
            "p_value": e.p_value, "cohens_dz": e.cohens_dz, "n_days": e.n, "hac_lags": e.lags}


# ---- main --------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--universe", choices=("crypto", "equities"), required=True)
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--lookback", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=300)
    ap.add_argument("--lr", type=float, default=0.01)
    ap.add_argument("--momentum", type=float, default=0.9)
    ap.add_argument("--vol-power", type=float, default=1.0,
                    help="eig1 volume weight = dollar_volume^power (1 = linear, 0 = off)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--replot", type=Path, default=None,
                    help="redraw charts from a finished run directory and exit")
    args = ap.parse_args()
    if args.replot:
        replot(args.replot)
        return
    warnings.filterwarnings("ignore", category=UserWarning)

    u = CRYPTO if args.universe == "crypto" else EQUITIES
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    out = args.out or Path("reports") / f"rmt_lstm_{u.name}_{stamp}"
    out.mkdir(parents=True, exist_ok=True)

    closes, dvol = load_crypto(u) if u.name == "crypto" else load_equities(u)
    rets = np.log(closes).diff().iloc[1:]
    gap_days = pd.Series(closes.index).diff().dt.days.to_numpy()[1:]
    rets = rets[gap_days <= 5]  # returns spanning a hole in the cache are multi-day; drop them
    dvol = dvol.reindex(rets.index)
    n_train = int(len(rets) * args.train_frac)
    train_rets, test_rets = rets.iloc[:n_train], rets.iloc[n_train:]
    years = n_train / u.periods_per_year
    pd.concat({"close": closes, "dollar_volume": dvol}, axis=1).to_parquet(out / "panel.parquet")
    print(f"[data] {u.name}: {rets.shape[1]} assets, {len(rets)} returns {rets.index[0].date()} -> "
          f"{rets.index[-1].date()}; train {n_train} ({years:.1f}y) / test {len(test_rets)}",
          flush=True)

    cfg = TrainConfig(lookback=args.lookback, epochs=args.epochs, lr=args.lr,
                      momentum=args.momentum)
    variants = u.variants()
    ref_name = "with diversifiers"
    fits: dict[str, RMTResult] = {}
    runs: dict[str, ConfigRun] = {}
    mats: dict[str, dict[str, float | np.ndarray]] = {}
    structure: dict[str, dict[str, object]] = {}

    for vname, syms in variants.items():
        cols = list(syms)
        w = volume_weights(dvol[cols].iloc[:n_train], args.vol_power) if args.vol_power else None
        r = fit_rmt(train_rets[cols], eig1_weights=w)
        fits[vname] = r
        mats[vname] = matrix_scores(r, test_rets[cols], u.periods_per_year)
        structure[vname] = {
            "n_assets": len(cols), "n_signal": r.n_signal, "sigma2": r.sigma2,
            "lambda1": float(r.eigenvalues[0]), "lambda1_share": float(r.eigenvalues[0] / len(cols)),
            "signal_share": r.signal_share, "spectral_snr": r.spectral_snr,
            "eigenvalues_top8": r.eigenvalues[:8].round(4).tolist(),
            "eig1_vol_weights": dict(zip(cols, np.round(w, 4).tolist(), strict=True)) if w is not None else None,
            **{k: v for k, v in mats[vname].items() if not isinstance(v, np.ndarray)},
        }
        z_all = (rets[cols].to_numpy() - r.mean) / r.std
        feats = np.hstack([z_all, r.factor_returns(rets[cols], k=K_DEEP).to_numpy()])
        t0 = time.time()
        runs[vname] = run_model(feats, z_all, n_train, cfg, args.seeds, args.val_frac)
        print(f"[model] {vname}: N={len(cols)} signal={r.n_signal} "
              f"mse={np.mean([m['mse'] for m in runs[vname].metrics]):.4f} "
              f"r2={np.mean([m['r2_vs_zero'] for m in runs[vname].metrics]):+.4f} "
              f"({time.time() - t0:.0f}s)", flush=True)
        if vname == ref_name:
            plot_spectrum(r, out / "spectrum_vs_mp.png")
            plot_embedding_3d(r, out / "eigen_embedding_3d.png")
            plot_embedding_6d(r, out / "eigen_embedding_6d_pairs.png")
            plot_corr(r, out / "correlation_raw_vs_denoised.png")
            pd.DataFrame(r.embed(K_DEEP), index=list(r.symbols),
                         columns=[f"eig{i + 1}" for i in range(K_DEEP)]).to_csv(
                out / "eigen_embedding_6d.csv")
            t0 = time.time()
            runs["raw returns only (ablation)"] = run_model(z_all, z_all, n_train, cfg, args.seeds,
                                                            args.val_frac)
            print(f"[model] raw-returns ablation ({time.time() - t0:.0f}s)", flush=True)

    # --- A + C: paired effect sizes -----------------------------------------------------------
    ref_syms = list(variants[ref_name])

    def daily_loss(run: ConfigRun, syms: list[str], common: list[str]) -> np.ndarray:
        idx = [syms.index(s) for s in common]
        per_seed = [((p[:, idx] - run.y[:, idx]) ** 2).mean(axis=1) for p in run.preds]
        return np.mean(per_seed, axis=0)

    effects: dict[str, dict[str, PairedEffect]] = {}
    for vname, syms in variants.items():
        if vname == ref_name:
            continue
        common = list(syms)
        effects[f"{vname} vs with"] = {
            "LSTM-Transformer forecast MSE (common assets)":
                paired_effect(daily_loss(runs[vname], list(syms), common)
                              - daily_loss(runs[ref_name], ref_syms, common)),
            "min-variance portfolio squared return":
                paired_effect(np.asarray(mats[vname]["clean_minvar_sq"])
                              - np.asarray(mats[ref_name]["clean_minvar_sq"])),
        }
    ablation = paired_effect(daily_loss(runs["raw returns only (ablation)"], ref_syms, ref_syms)
                             - daily_loss(runs[ref_name], ref_syms, ref_syms))
    plot_effects(effects, out / "diversifier_effect_sizes.png")
    plot_training({k: v.history0 for k, v in runs.items()}, out / "training_curves_seed0.png")

    # --- B: lower boundary x noise shape sweep on the reference basket ------------------------
    base = fits[ref_name]
    edge = base.lower_edge()
    rows = []
    for frac in LOWER_FRACS:
        for shape in NOISE_SHAPES:
            r = fit_rmt(train_rets[ref_syms], lower_cut=frac * edge, noise_shape=shape,
                        eig1_weights=base.eig1_weights)
            m = matrix_scores(r, test_rets[ref_syms], u.periods_per_year)
            rows.append({"lower_frac": frac, "lower_cut": frac * edge, "shape": shape,
                         "n_lower_kept": r.n_lower_kept, "spectral_snr": r.spectral_snr,
                         "oos_nll": m["clean_nll"], "oos_minvar_vol": m["clean_minvar_vol"]})
    sweep = pd.DataFrame(rows)
    sweep.to_csv(out / "lower_boundary_sweep.csv", index=False)
    plot_sweep(sweep, float(mats[ref_name]["raw_nll"]), float(mats[ref_name]["raw_minvar_vol"]),
               out / "lower_boundary_sweep.png")

    # --- summary ------------------------------------------------------------------------------
    def agg(run: ConfigRun) -> dict[str, float]:
        keys = run.metrics[0].keys()
        res = {f"{k}_mean": float(np.mean([m[k] for m in run.metrics])) for k in keys}
        if len(run.metrics) > 1:
            res.update({f"{k}_seed_sd": float(np.std([m[k] for m in run.metrics], ddof=1))
                        for k in ("mse", "r2_vs_zero", "rank_ic")})
        return res

    summary = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "universe": u.name, "window": [str(rets.index[0].date()), str(rets.index[-1].date())],
        "n_train": n_train, "n_test": len(test_rets), "train_years": round(years, 2),
        "overfit_flag_under_2y": years < 2, "seeds": args.seeds, "vol_power": args.vol_power,
        "train_config": asdict(cfg),
        "structure": structure,
        "forecast": {k: agg(v) for k, v in runs.items()},
        "effects": {k: {kk: _fmt(e) for kk, e in v.items()} for k, v in effects.items()},
        "ablation_raw_minus_6d_forecast_mse": _fmt(ablation),
        "lower_boundary_sweep": {"lower_edge": edge, "rows": rows},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(json.dumps({"structure": {k: {kk: v[kk] for kk in ("n_assets", "n_signal", "lambda1_share",
                                                             "spectral_snr", "clean_minvar_vol",
                                                             "raw_minvar_vol")}
                                    for k, v in structure.items()},
                      "forecast": {k: {kk: round(vv, 4) for kk, vv in agg(v).items()}
                                   for k, v in runs.items()},
                      "effects": summary["effects"],
                      "ablation": summary["ablation_raw_minus_6d_forecast_mse"]},
                     indent=1, default=float))
    print(sweep.round(4).to_string())
    print(f"[out] {out}")


if __name__ == "__main__":
    main()
