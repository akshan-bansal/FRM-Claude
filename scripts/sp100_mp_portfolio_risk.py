"""
S&P 100 Random Matrix Theory Portfolio Risk Engine

Pipeline:
Prices -> Returns -> Correlation -> Marchenko-Pastur filtering
-> Covariance -> Portfolio Volatility -> VaR/CVaR
-> Asset Risk Attribution -> Eigenfactor Risk -> Stress Tests

Standalone research script. Places no orders, touches no broker, reads no secrets.

NOTE: this file deliberately stays self-contained and depends on `requests`,
`yfinance`, `seaborn` and `lxml`, which are NOT project dependencies (the repo
rule is httpx + the local candle cache). Run it in its own venv:

    python -m venv .venv-sp100
    .venv-sp100/Scripts/pip install yfinance requests seaborn lxml pandas numpy scipy matplotlib
    .venv-sp100/Scripts/python scripts/sp100_mp_portfolio_risk.py

A repo-native port over `trading_live_claude.analysis.rmt` is the better long-term home.

Optional command-line arguments:
    --start 2013-01-01
    --end 2019-01-01
    --portfolio-value 1000000
    --confidence 0.95
    --window 252
    --outdir reports
    --no-plots
"""

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns

from scipy.stats import norm


warnings.filterwarnings("ignore")


# Wikipedia asks for a descriptive, contactable User-Agent. One honest string,
# not a rotating pool -- rotating UAs is evasion, and it buys nothing here.
USER_AGENT = (
    "sp100-mp-portfolio-risk/1.0 (research script; contact via repository owner)"
)


def normalize_ticker(symbol):
    """
    Map a Wikipedia class-share symbol to Yahoo's spelling.

    Wikipedia writes BRK.B / BF.B; Yahoo wants BRK-B / BF-B. Without this the
    class-share names download as all-NaN and are silently dropped by the
    cleaning step, so the universe quietly loses constituents.
    """
    return symbol.strip().upper().replace(".", "-")


def get_sp100():
    """Retrieve current S&P 100 ticker symbols from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/S%26P_100"
    headers = {"User-Agent": USER_AGENT}

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    tables = pd.read_html(response.text)

    # Wikipedia's table position can change, so locate the table by column name.
    for table in tables:
        if "Symbol" in table.columns:
            tickers = table["Symbol"].astype(str).tolist()
            if len(tickers) >= 80:
                return [normalize_ticker(t) for t in tickers]

    raise RuntimeError("Could not locate the S&P 100 constituent table.")


def download_prices(tickers, start_date, end_date):
    """Download adjusted historical close prices."""
    data = yf.download(
        tickers=tickers,
        start=start_date,
        end=end_date,
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )

    if data.empty:
        raise RuntimeError("No market data was downloaded.")

    closes = {}

    for ticker in tickers:
        try:
            series = data[(ticker, "Close")]
            if isinstance(series, pd.Series):
                closes[ticker] = series
        except (KeyError, TypeError):
            continue

    prices = pd.DataFrame(closes)

    requested = set(tickers)

    # Remove completely unavailable tickers.
    prices = prices.dropna(axis=1, how="all")

    # Remove assets with more than 5% missing observations.
    prices = prices.dropna(axis=1, thresh=int(0.95 * len(prices)))

    # Report what fell out rather than shrinking the universe in silence.
    dropped = sorted(requested - set(prices.columns))
    if dropped:
        print(
            f"Dropped {len(dropped)} symbols (no/too-sparse data): "
            f"{', '.join(dropped)}"
        )

    # Remaining observations must be complete for this RMT analysis.
    prices = prices.dropna()

    if prices.shape[1] < 10:
        raise RuntimeError(
            "Too few usable securities remain after cleaning."
        )

    return prices


def calculate_returns(prices):
    """Calculate daily log returns."""
    returns = np.log(prices / prices.shift(1)).dropna()
    return returns


def empirical_correlation(returns):
    """Calculate empirical correlation matrix."""
    return returns.corr()


def mp_bounds(n_assets, n_observations, sigma2=1.0):
    """
    Marchenko-Pastur bounds.

    q = N/T for N assets and T observations.
    """
    q = n_assets / n_observations

    lambda_plus = sigma2 * (1.0 + np.sqrt(q)) ** 2
    lambda_minus = sigma2 * (1.0 - np.sqrt(q)) ** 2

    return q, lambda_minus, lambda_plus


def estimate_noise_sigma2(eigenvalues, q):
    """
    Estimate the variance of the noise bulk.

    sigma2 is NOT 1 for real equity markets. The market mode alone absorbs
    25-35% of the total variance of an S&P 100 correlation matrix, so the
    variance left over for the noise bulk is well below 1. Using sigma2 = 1
    puts lambda_+ too high and buries genuine sector structure inside the
    noise band, which over-clips the matrix.

    One pass, following Laloux et al. (2000) / Potters et al. (2005):
    eigenvalues above the sigma2 = 1 edge are provisionally signal, and sigma2
    is the mean of the rest (the MP law has mean sigma2). Deliberately not
    iterated to a fixed point -- iteration chases the bulk downwards and
    over-fits the cut.
    """
    provisional_plus = (1.0 + np.sqrt(q)) ** 2

    bulk = eigenvalues[eigenvalues <= provisional_plus]

    if bulk.size == 0:
        return 1.0

    return float(bulk.mean())


def eigendecompose(correlation):
    """Symmetric eigendecomposition, sorted largest first."""
    values = correlation.values if hasattr(correlation, "values") else correlation

    eigenvalues, eigenvectors = np.linalg.eigh(values)

    idx = np.argsort(eigenvalues)[::-1]

    eigenvalues = eigenvalues[idx]
    eigenvectors = eigenvectors[:, idx]

    return eigenvalues, eigenvectors


def mp_filter_correlation(
    correlation,
    eigenvalues,
    eigenvectors,
    lambda_plus,
):
    """
    MP eigenvalue clipping.

    Eigenvalues above lambda_plus are retained.
    Eigenvalues inside the MP noise band are replaced by
    their mean. The matrix is subsequently renormalized
    to have unit diagonal.
    """
    noise_mask = eigenvalues <= lambda_plus

    if noise_mask.any():
        noise_mean = eigenvalues[noise_mask].mean()
    else:
        noise_mean = 1.0

    filtered_eigenvalues = eigenvalues.copy()
    filtered_eigenvalues[noise_mask] = noise_mean

    filtered = (
        eigenvectors
        @ np.diag(filtered_eigenvalues)
        @ eigenvectors.T
    )

    filtered = (filtered + filtered.T) / 2.0

    # Normalize back to a correlation matrix.
    diagonal = np.sqrt(np.maximum(np.diag(filtered), 1e-12))

    filtered = filtered / np.outer(diagonal, diagonal)

    np.fill_diagonal(filtered, 1.0)

    filtered = pd.DataFrame(
        filtered,
        index=correlation.index,
        columns=correlation.columns,
    )

    return filtered, filtered_eigenvalues, noise_mask


def correlation_to_covariance(correlation, returns):
    """Convert correlation matrix to annualized covariance matrix."""
    annual_vol = returns.std() * np.sqrt(252.0)

    D = np.diag(annual_vol.values)

    covariance = D @ correlation.values @ D

    return pd.DataFrame(
        covariance,
        index=correlation.index,
        columns=correlation.columns,
    ), annual_vol


def equal_weights(returns):
    """Create an equal-weight portfolio."""
    n = returns.shape[1]
    return pd.Series(
        np.ones(n) / n,
        index=returns.columns,
        name="Weight",
    )


def portfolio_statistics(
    returns,
    weights,
    covariance,
    confidence,
    portfolio_value,
    window=None,
):
    """
    Calculate portfolio volatility and VaR/CVaR.

    Two different estimators are reported and they are not interchangeable:

    * historical VaR/CVaR are empirical quantiles of the realized portfolio
      return series. They do NOT depend on the MP filtering at all.
    * parametric VaR is derived from the (MP-filtered) covariance matrix.

    `window` restricts the historical estimators to the most recent N
    observations; None uses the full sample.
    """
    w = weights.values

    portfolio_variance = w.T @ covariance.values @ w
    portfolio_volatility = np.sqrt(max(portfolio_variance, 0.0))

    portfolio_returns = returns @ weights

    if window is not None and window > 0:
        var_sample = portfolio_returns.tail(int(window))
    else:
        var_sample = portfolio_returns

    historical_var_return = np.quantile(
        var_sample,
        1.0 - confidence,
    )

    tail = var_sample[
        var_sample <= historical_var_return
    ]

    historical_cvar_return = tail.mean()

    z = norm.ppf(confidence)

    parametric_var_return = (
        z * portfolio_volatility / np.sqrt(252.0)
    )

    results = {
        "portfolio_volatility": portfolio_volatility,
        "var_sample_size": int(len(var_sample)),
        "historical_var_return": -historical_var_return,
        "historical_cvar_return": -historical_cvar_return,
        "parametric_var_return": parametric_var_return,
        "historical_var_dollar": (
            -historical_var_return * portfolio_value
        ),
        "historical_cvar_dollar": (
            -historical_cvar_return * portfolio_value
        ),
        "parametric_var_dollar": (
            parametric_var_return * portfolio_value
        ),
        "portfolio_returns": portfolio_returns,
    }

    return results


def risk_contribution(weights, covariance):
    """
    Calculate marginal and component volatility contribution.
    """
    w = weights.values

    portfolio_volatility = np.sqrt(
        w.T @ covariance.values @ w
    )

    marginal = (
        covariance.values @ w
    ) / portfolio_volatility

    component = w * marginal

    result = pd.DataFrame(
        {
            "Weight": weights,
            "Marginal_Risk": marginal,
            "Component_Risk": component,
        },
        index=weights.index,
    )

    result["Risk_%"] = (
        result["Component_Risk"] /
        portfolio_volatility
    )

    return result.sort_values(
        "Risk_%",
        ascending=False,
    )


def factor_risk(eigenvalues, eigenvectors, weights, annual_vol):
    """
    Portfolio variance decomposed across eigenfactors.

    Two things have to line up for this to be an attribution of the SAME
    number the volatility section reports:

    1. The eigenpairs must be those of the FINAL filtered correlation matrix.
       Clipping changes eigenvalues and the unit-diagonal renormalization that
       follows changes them again, so the pre-renormalization clipped values
       are not the spectrum of the matrix actually used downstream.

    2. Weights must be scaled into correlation space. Portfolio variance is
       w' Sigma w with Sigma = D C D, so with w_tilde = D w it equals
       w_tilde' C w_tilde = sum_k lambda_k * (v_k . w_tilde)^2. Decomposing
       with raw w instead attributes correlation-space variance and does not
       sum to the portfolio variance -- normalizing to percentages hides that.
    """
    w_scaled = annual_vol.values * weights.values

    factor_exposure = eigenvectors.T @ w_scaled

    factor_variance = (
        eigenvalues *
        factor_exposure ** 2
    )

    result = pd.DataFrame(
        {
            "Factor": np.arange(1, len(eigenvalues) + 1),
            "Eigenvalue": eigenvalues,
            "Exposure": factor_exposure,
            "Variance_Contribution": factor_variance,
        }
    )

    total = result["Variance_Contribution"].sum()

    if total > 0:
        result["Variance_%"] = (
            result["Variance_Contribution"] / total
        )
    else:
        result["Variance_%"] = 0.0

    return result, float(total)


def volatility_stress(
    filtered_correlation,
    annual_vol,
    weights,
    multiplier=1.50,
    correlation_shift=0.0,
):
    """
    Portfolio volatility under a volatility and/or correlation shock.

    A pure volatility multiplier is not really a stress test: scaling every
    asset vol by k scales portfolio vol by exactly k, so it restates the base
    number and carries no information about the portfolio. What actually hurts
    a diversified book in a crisis is correlation convergence, so
    `correlation_shift` blends the correlation matrix toward the all-ones
    matrix:

        C_stressed = (1 - a) * C + a * J

    That is a convex combination of two PSD matrices, so it stays PSD, and it
    has unit diagonal by construction.
    """
    stressed_vol = annual_vol * multiplier

    C = filtered_correlation.values

    a = float(correlation_shift)

    if a > 0.0:
        C = (1.0 - a) * C + a * np.ones_like(C)
        np.fill_diagonal(C, 1.0)

    D = np.diag(stressed_vol.values)

    covariance = D @ C @ D

    w = weights.values

    return float(np.sqrt(w.T @ covariance @ w))


def plot_eigenvalues(
    eigenvalues,
    lambda_minus,
    lambda_plus,
    save_path=None,
):
    """Plot empirical eigenvalue spectrum and MP bounds."""
    plt.figure(figsize=(12, 6))

    plt.plot(
        range(1, len(eigenvalues) + 1),
        eigenvalues,
        marker="o",
        linewidth=1,
        markersize=3,
    )

    plt.axhline(
        lambda_plus,
        linestyle="--",
        color="crimson",
        label=r"$\lambda_+$",
    )

    plt.axhline(
        lambda_minus,
        linestyle="--",
        color="darkorange",
        label=r"$\lambda_-$",
    )

    # The market eigenvalue is ~30x the bulk; on a linear axis the MP bounds
    # collapse onto the x-axis and the plot says nothing.
    plt.yscale("log")

    plt.xlabel("Principal Component")
    plt.ylabel("Eigenvalue (log scale)")
    plt.title("S&P 100 Eigenvalue Spectrum vs MP Bounds")
    plt.legend()
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150)

    plt.close()


def plot_correlation_comparison(
    raw,
    filtered,
    save_path=None,
):
    """Plot raw and MP-filtered correlation matrices."""
    fig, axes = plt.subplots(
        1,
        2,
        figsize=(18, 7),
    )

    sns.heatmap(
        raw,
        cmap="coolwarm",
        center=0,
        vmin=-1,
        vmax=1,
        ax=axes[0],
    )

    axes[0].set_title("Empirical Correlation")

    sns.heatmap(
        filtered,
        cmap="coolwarm",
        center=0,
        vmin=-1,
        vmax=1,
        ax=axes[1],
    )

    axes[1].set_title("MP-Filtered Correlation")

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150)

    plt.close()


def main():
    parser = argparse.ArgumentParser(
        description="S&P 100 MP Portfolio Risk Engine"
    )

    parser.add_argument(
        "--start",
        default="2013-01-01",
    )

    parser.add_argument(
        "--end",
        default="2019-01-01",
    )

    parser.add_argument(
        "--portfolio-value",
        type=float,
        default=1_000_000.0,
    )

    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
    )

    parser.add_argument(
        "--window",
        type=int,
        default=252,
        help=(
            "Trailing observations used for the historical VaR/CVaR "
            "estimators. 0 uses the full sample."
        ),
    )

    parser.add_argument(
        "--outdir",
        default="reports",
    )

    parser.add_argument(
        "--no-plots",
        action="store_true",
    )

    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    window = args.window if args.window and args.window > 0 else None

    print("=" * 70)
    print("S&P 100 RANDOM MATRIX PORTFOLIO RISK ENGINE")
    print("=" * 70)

    print("\nDownloading S&P 100 constituents...")
    tickers = get_sp100()

    print(f"Constituents found: {len(tickers)}")

    print("\nDownloading price history...")
    prices = download_prices(
        tickers,
        args.start,
        args.end,
    )

    returns = calculate_returns(prices)

    N = returns.shape[1]
    T = returns.shape[0]

    print(f"Usable assets:       {N}")
    print(f"Observations:        {T}")

    correlation = empirical_correlation(returns)

    eigenvalues, eigenvectors = eigendecompose(
        correlation
    )

    q = N / T

    sigma2 = estimate_noise_sigma2(eigenvalues, q)

    _, naive_minus, naive_plus = mp_bounds(N, T, sigma2=1.0)
    q, lambda_minus, lambda_plus = mp_bounds(N, T, sigma2=sigma2)

    print("\nMarchenko-Pastur parameters")
    print("-" * 40)
    print(f"q:                   {q:.6f}")
    print(f"Noise sigma^2:       {sigma2:.6f}")
    print(f"Lambda minus:        {lambda_minus:.6f}")
    print(f"Lambda plus:         {lambda_plus:.6f}")
    print(
        f"(sigma^2=1 would give lambda_plus = {naive_plus:.6f}, "
        f"lambda_minus = {naive_minus:.6f})"
    )

    filtered_corr, clipped_eigenvalues, noise_mask = (
        mp_filter_correlation(
            correlation,
            eigenvalues,
            eigenvectors,
            lambda_plus,
        )
    )

    signal_count = int((~noise_mask).sum())
    noise_count = int(noise_mask.sum())

    naive_noise_count = int((eigenvalues <= naive_plus).sum())

    print(f"Signal eigenvalues:   {signal_count}")
    print(f"Noise eigenvalues:    {noise_count}")
    print(
        f"(sigma^2=1 would have called {naive_noise_count} of {N} noise, "
        f"clipping {naive_noise_count - noise_count} extra)"
    )

    # Eigenpairs of the matrix actually used downstream, after clipping AND
    # the unit-diagonal renormalization.
    final_eigenvalues, final_eigenvectors = eigendecompose(filtered_corr)

    # Covariance matrices.
    raw_covariance, annual_vol = correlation_to_covariance(
        correlation,
        returns,
    )

    filtered_covariance, _ = correlation_to_covariance(
        filtered_corr,
        returns,
    )

    # Equal-weight portfolio.
    weights = equal_weights(returns)

    raw_portfolio_vol = np.sqrt(
        weights.values.T
        @ raw_covariance.values
        @ weights.values
    )

    filtered_stats = portfolio_statistics(
        returns,
        weights,
        filtered_covariance,
        args.confidence,
        args.portfolio_value,
        window=window,
    )

    print("\nPORTFOLIO RISK")
    print("=" * 70)
    print(f"Portfolio value:          ${args.portfolio_value:,.2f}")
    print(f"Raw annual volatility:    {raw_portfolio_vol:.2%}")
    print(
        f"MP annual volatility:     "
        f"{filtered_stats['portfolio_volatility']:.2%}"
    )

    print(
        f"\nParametric {args.confidence:.0%} 1-day VaR "
        f"(from the MP covariance):"
    )
    print(
        f"  {filtered_stats['parametric_var_return']:.2%}   "
        f"${filtered_stats['parametric_var_dollar']:,.2f}"
    )

    sample_label = (
        f"last {filtered_stats['var_sample_size']} obs"
        if window
        else f"full sample, {filtered_stats['var_sample_size']} obs"
    )

    print(
        f"\nHistorical {args.confidence:.0%} 1-day VaR/CVaR "
        f"({sample_label}, empirical -- independent of MP filtering):"
    )
    print(
        f"  VaR   {filtered_stats['historical_var_return']:.2%}   "
        f"${filtered_stats['historical_var_dollar']:,.2f}"
    )
    print(
        f"  CVaR  {filtered_stats['historical_cvar_return']:.2%}   "
        f"${filtered_stats['historical_cvar_dollar']:,.2f}"
    )

    # Asset-level risk contribution.
    risk_table = risk_contribution(
        weights,
        filtered_covariance,
    )

    # Factor-level risk contribution, on the final filtered spectrum.
    factor_table, factor_total_variance = factor_risk(
        final_eigenvalues,
        final_eigenvectors,
        weights,
        annual_vol,
    )

    # Stress tests.
    vol_only_stress = volatility_stress(
        filtered_corr,
        annual_vol,
        weights,
        multiplier=1.50,
        correlation_shift=0.0,
    )

    corr_only_stress = volatility_stress(
        filtered_corr,
        annual_vol,
        weights,
        multiplier=1.00,
        correlation_shift=0.50,
    )

    combined_stress = volatility_stress(
        filtered_corr,
        annual_vol,
        weights,
        multiplier=1.50,
        correlation_shift=0.50,
    )

    print("\nRISK ATTRIBUTION")
    print("=" * 70)
    print(risk_table.head(15).to_string())

    print("\nTOP EIGENFACTORS")
    print("=" * 70)
    print(
        factor_table
        .head(15)
        .to_string(index=False)
    )

    base_variance = filtered_stats["portfolio_volatility"] ** 2

    print(
        f"\nFactor variance sums to {factor_total_variance:.8f} "
        f"vs portfolio variance {base_variance:.8f} "
        f"(residual {abs(factor_total_variance - base_variance):.2e})"
    )

    print("\nSTRESS TEST")
    print("=" * 70)
    base_vol = filtered_stats["portfolio_volatility"]
    print(f"Base MP volatility:                 {base_vol:.2%}")
    print(
        f"50% vol shock, correlations held:   {vol_only_stress:.2%}  "
        f"(x{vol_only_stress / base_vol:.2f} "
        f"-- exactly the multiplier, by construction)"
    )
    print(
        f"Correlations 50% toward 1.0:        {corr_only_stress:.2%}  "
        f"(x{corr_only_stress / base_vol:.2f})"
    )
    print(
        f"Both together:                      {combined_stress:.2%}  "
        f"(x{combined_stress / base_vol:.2f})"
    )

    # Save analytical outputs.
    risk_table.to_csv(outdir / "asset_risk_contribution.csv")

    factor_table.to_csv(
        outdir / "factor_risk_contribution.csv",
        index=False,
    )

    filtered_corr.to_csv(outdir / "mp_filtered_correlation.csv")

    filtered_covariance.to_csv(outdir / "mp_filtered_covariance.csv")

    pd.DataFrame(
        {
            "Eigenvalue": eigenvalues,
            "Clipped_Eigenvalue": clipped_eigenvalues,
            "Final_Eigenvalue": final_eigenvalues,
            "MP_Noise": noise_mask,
        }
    ).to_csv(
        outdir / "mp_eigenvalues.csv",
        index=False,
    )

    print(f"\nFiles written to {outdir}/:")
    print("  asset_risk_contribution.csv")
    print("  factor_risk_contribution.csv")
    print("  mp_filtered_correlation.csv")
    print("  mp_filtered_covariance.csv")
    print("  mp_eigenvalues.csv")

    if not args.no_plots:
        plot_eigenvalues(
            eigenvalues,
            lambda_minus,
            lambda_plus,
            save_path=outdir / "mp_eigenvalue_spectrum.png",
        )

        plot_correlation_comparison(
            correlation,
            filtered_corr,
            save_path=outdir / "mp_correlation_comparison.png",
        )

        print("  mp_eigenvalue_spectrum.png")
        print("  mp_correlation_comparison.png")


if __name__ == "__main__":
    main()
