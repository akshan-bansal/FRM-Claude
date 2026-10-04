"""L4 curvature risk: the spectral structure of a convex position's nonlinear P&L.

Self-contained port of the curvature-risk part of the research script ``bsm_spectral.py``. The
original imported three sibling modules (``bsm_parallel_surface_4th``, ``bsm_signal``,
``bsm_direction``) that could not be found, so this re-derives what it needs from first principles:
exact Black-Scholes-Merton derivatives in (spot, volatility) through order 4 by truncated bivariate
Taylor arithmetic ("jets"), then the construction the script describes. It is NOT a bit-for-bit
copy: conventions (units, premium normalisation, the stress-ray loss) are re-derived from how the
script uses them, and are stated below so they can be checked against the original.

What it computes for one straddle position
    shocks   x = (dS, dvol) = L z with z ~ N(0, I); L is the Cholesky factor of the shock covariance
    order 2  M = L' H L, the premium-normalised curvature matrix; eigen M = Q diag(lam) Q'
             principal curvatures lam, principal risk directions Q
    energy   E[t_k^2], t_k = T_k[z..z] / k!, exact by Gauss-Hermite; for order 2 also the closed
             form ((tr M)^2 + 2 tr M^2) / 4
    gate     "series trust": on the stress ray at the confidence radius, |t3| < |t2| and
             |t4| < |t3|. If not, the Taylor series is not contracting and the answer is REPRICE.

Conventions
    * Tensors are per unit of the position's opening premium (call + put at the original expiry)
      and are taken at the rolled state (expiry minus the horizon), so carry is excluded from the
      curvature terms and included in the exact repricing used for the stress ray.
    * Shocks are linear in (spot, vol); a state with spot <= 0 or vol <= 0 is outside the model's
      domain. It is reported, never clipped away.

Contract, same as the rest of ``analysis``: this is a ruler. It is a RISK MEASURE for convex
positions and never an entry signal. A spot or crypto position has no curvature, so nothing here
applies to the current book; the module is deliberately unconnected to orders until an options path
exists. Curvature energy is a proposed diagnostic, not a validated trading signal. Nothing here is
imported by execution, risk, brokers, strategies, monitor or the daemon;
``tests/test_curvature.py`` enforces the import boundary.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

FloatArray = NDArray[np.float64]

ORDER = 4
_N = ORDER + 1
_THETA: FloatArray = np.linspace(0.0, 2.0 * np.pi, 3601)[:-1]
_SQRT_2PI = math.sqrt(2.0 * math.pi)
_erfc = np.vectorize(math.erfc, otypes=[float])


# --------------------------------------------------------------------------- exact derivatives


class _Jet:
    """Truncated bivariate Taylor polynomial in (dS, dvol): c[i, j] multiplies dS^i * dvol^j.

    Only terms with i + j <= ORDER are kept. The derivative d^i/dS^i d^j/dvol^j of the function is
    i! * j! * c[i, j]. Arithmetic is exact to floating point; there is no finite differencing.
    """

    __slots__ = ("c",)

    def __init__(self, c: FloatArray) -> None:
        self.c = c

    @classmethod
    def constant(cls, value: float) -> _Jet:
        c = np.zeros((_N, _N))
        c[0, 0] = value
        return cls(c)

    @classmethod
    def variable(cls, value: float, axis: int) -> _Jet:
        c = np.zeros((_N, _N))
        c[0, 0] = value
        c[(1, 0) if axis == 0 else (0, 1)] = 1.0
        return cls(c)

    def __add__(self, other: _Jet | float) -> _Jet:
        o = other.c if isinstance(other, _Jet) else _Jet.constant(other).c
        return _Jet(self.c + o)

    __radd__ = __add__

    def __neg__(self) -> _Jet:
        return _Jet(-self.c)

    def __sub__(self, other: _Jet | float) -> _Jet:
        return self + (-other)

    def __mul__(self, other: _Jet | float) -> _Jet:
        if not isinstance(other, _Jet):
            return _Jet(self.c * other)
        out = np.zeros((_N, _N))
        for i1 in range(_N):
            for j1 in range(_N - i1):
                a = self.c[i1, j1]
                if a == 0.0:
                    continue
                for i2 in range(_N - i1 - j1):
                    for j2 in range(_N - i1 - j1 - i2):
                        out[i1 + i2, j1 + j2] += a * other.c[i2, j2]
        return _Jet(out)

    __rmul__ = __mul__

    def _compose(self, derivs: Sequence[float]) -> _Jet:
        """f(self) from f's derivatives at self's constant term: sum_k f^(k)(c0) / k! * delta^k."""
        delta = _Jet(self.c.copy())
        delta.c[0, 0] = 0.0
        result = _Jet.constant(derivs[0])
        power = _Jet.constant(1.0)
        for k in range(1, ORDER + 1):
            power = power * delta
            result = result + power * (derivs[k] / math.factorial(k))
        return result

    def log(self) -> _Jet:
        x = float(self.c[0, 0])
        return self._compose([math.log(x), 1 / x, -1 / x**2, 2 / x**3, -6 / x**4])

    def reciprocal(self) -> _Jet:
        x = float(self.c[0, 0])
        return self._compose([1 / x, -1 / x**2, 2 / x**3, -6 / x**4, 24 / x**5])

    def __truediv__(self, other: _Jet | float) -> _Jet:
        return self * (other.reciprocal() if isinstance(other, _Jet) else 1.0 / other)

    def ncdf(self) -> _Jet:
        """Standard normal CDF of the jet. Phi^(k)(x) = (-1)^(k-1) He_(k-1)(x) phi(x)."""
        x = float(self.c[0, 0])
        phi = math.exp(-0.5 * x * x) / _SQRT_2PI
        return self._compose([
            0.5 * math.erfc(-x / math.sqrt(2.0)), phi, -x * phi, (x * x - 1) * phi, -(x**3 - 3 * x) * phi])


def _bsm_jets(spot: float, strike: float, vol: float, expiry: float, rate: float, dividend: float
              ) -> tuple[_Jet, _Jet]:
    """Call and put as jets in (spot, vol) around the given state."""
    if min(spot, strike, vol, expiry) <= 0:
        raise ValueError("spot, strike, vol and expiry must all be positive")
    s = _Jet.variable(spot, 0)
    v = _Jet.variable(vol, 1)
    root_t = math.sqrt(expiry)
    d1 = ((s / strike).log() + (rate - dividend) * expiry + (v * v) * (0.5 * expiry)) / (v * root_t)
    d2 = d1 - v * root_t
    disc_q, disc_r = math.exp(-dividend * expiry), math.exp(-rate * expiry)
    call = s * d1.ncdf() * disc_q - d2.ncdf() * (strike * disc_r)
    put = (-d2).ncdf() * (strike * disc_r) - s * (-d1).ncdf() * disc_q
    return call, put


def _ncdf(x: FloatArray) -> FloatArray:
    out: FloatArray = 0.5 * _erfc(-x / math.sqrt(2.0))
    return out


def _exact_straddle(spot: FloatArray | float, strike: float, vol: FloatArray | float, expiry: float,
                    rate: float, dividend: float) -> FloatArray:
    """Exact straddle price, vectorised over spot and vol."""
    root_t = math.sqrt(expiry)
    s = np.asarray(spot, dtype=float)
    v = np.asarray(vol, dtype=float)
    d1 = (np.log(s / strike) + (rate - dividend + 0.5 * v * v) * expiry) / (v * root_t)
    d2 = d1 - v * root_t
    disc_q, disc_r = math.exp(-dividend * expiry), math.exp(-rate * expiry)
    call = s * disc_q * _ncdf(d1) - strike * disc_r * _ncdf(d2)
    put = strike * disc_r * _ncdf(-d2) - s * disc_q * _ncdf(-d1)
    out: FloatArray = call + put
    return out


# --------------------------------------------------------------------------- the position


@dataclass(frozen=True)
class StraddleLeg:
    """A straddle (call + put, one strike and expiry) and the shock model over its horizon.

    ``sd_spot`` is the standard deviation of the spot move over the horizon in price units;
    ``sd_vol`` is that of the volatility-level move (0.01 = one vol point); ``rho`` is their
    correlation. ``horizon`` and ``expiry`` are in years, with horizon < expiry.
    """

    spot: float
    strike: float
    vol: float
    expiry: float
    horizon: float
    sd_spot: float
    sd_vol: float
    rho: float
    rate: float = 0.0
    dividend: float = 0.0

    def __post_init__(self) -> None:
        if min(self.spot, self.strike, self.vol, self.expiry, self.horizon) <= 0:
            raise ValueError("spot, strike, vol, expiry and horizon must be positive")
        if self.horizon >= self.expiry:
            raise ValueError("horizon must be shorter than expiry (the leg rolls to expiry - horizon)")
        if self.sd_spot <= 0 or self.sd_vol <= 0:
            raise ValueError("sd_spot and sd_vol must be positive")
        if not -1.0 < self.rho < 1.0:
            raise ValueError("rho must lie strictly between -1 and 1")

    @classmethod
    def from_daily(cls, *, spot: float, strike: float, vol: float, expiry: float, daily_return_sd: float,
                   daily_vol_sd: float, rho: float, horizon_days: float, rate: float = 0.0,
                   dividend: float = 0.0, trading_days: float = 252.0) -> StraddleLeg:
        """Scale daily move sizes to the horizon by sqrt(time), the usual random-walk convention."""
        root = math.sqrt(horizon_days)
        return cls(spot=spot, strike=strike, vol=vol, expiry=expiry, horizon=horizon_days / trading_days,
                   sd_spot=daily_return_sd * root * spot, sd_vol=daily_vol_sd * root, rho=rho,
                   rate=rate, dividend=dividend)

    @property
    def rolled_expiry(self) -> float:
        return self.expiry - self.horizon

    @property
    def premium(self) -> float:
        call, put = _bsm_jets(self.spot, self.strike, self.vol, self.expiry, self.rate, self.dividend)
        return float(call.c[0, 0] + put.c[0, 0])

    def coefficients(self) -> FloatArray:
        """Straddle Taylor coefficients c[i, j] at the rolled state (carry excluded)."""
        call, put = _bsm_jets(self.spot, self.strike, self.vol, self.rolled_expiry, self.rate, self.dividend)
        out: FloatArray = call.c + put.c
        return out


# --------------------------------------------------------------------------- tensors


def whitener(leg: StraddleLeg) -> FloatArray:
    """L with x = (dS, dvol) = L z and z ~ N(0, I)."""
    s = math.sqrt(1.0 - leg.rho**2)
    return np.array([[leg.sd_spot, 0.0], [leg.sd_vol * leg.rho, leg.sd_vol * s]])


def straddle_tensors(leg: StraddleLeg) -> dict[int, FloatArray]:
    """Symmetric derivative tensors of the straddle per unit premium. Index 0 is spot, 1 is vol."""
    c = leg.coefficients()
    prem = leg.premium
    out: dict[int, FloatArray] = {}
    for order in (2, 3, 4):
        t = np.zeros((2,) * order)
        for idx in np.ndindex(*(2,) * order):
            n_vol = sum(idx)
            n_spot = order - n_vol
            t[idx] = c[n_spot, n_vol] * math.factorial(n_spot) * math.factorial(n_vol) / prem
        out[order] = t
    return out


def whitened_tensors(leg: StraddleLeg) -> tuple[dict[int, FloatArray], FloatArray]:
    L = whitener(leg)
    t = straddle_tensors(leg)
    return {
        2: np.einsum("ij,ia,jb->ab", t[2], L, L),
        3: np.einsum("ijk,ia,jb,kc->abc", t[3], L, L, L),
        4: np.einsum("ijkl,ia,jb,kc,ld->abcd", t[4], L, L, L, L),
    }, L


def q_profile(tz: FloatArray, order: int, theta: FloatArray = _THETA) -> FloatArray:
    """q_k(theta) = T_k[u, .., u] / k! on the unit circle of whitened shocks."""
    u = np.stack([np.cos(theta), np.sin(theta)], axis=1)
    if order == 2:
        val = np.einsum("ab,na,nb->n", tz, u, u)
    elif order == 3:
        val = np.einsum("abc,na,nb,nc->n", tz, u, u, u)
    else:
        val = np.einsum("abcd,na,nb,nc,nd->n", tz, u, u, u, u)
    out: FloatArray = val / math.factorial(order)
    return out


def _gauss_hermite(n: int = 7) -> tuple[FloatArray, FloatArray]:
    x, w = np.polynomial.hermite_e.hermegauss(n)
    w = w / w.sum()
    zs = np.array([[a, b] for a in x for b in x])
    ws = np.array([p * q for p in w for q in w])
    return zs, ws


def gaussian_energy(tz: FloatArray, order: int) -> float:
    """Exact E[t_k^2] under z ~ N(0, I), by Gauss-Hermite (t_k^2 has degree 2k, well inside 7 nodes)."""
    zs, ws = _gauss_hermite(7)
    th = np.arctan2(zs[:, 1], zs[:, 0])
    r = np.hypot(zs[:, 0], zs[:, 1])
    return float(np.sum(ws * (r**order * q_profile(tz, order, th)) ** 2))


def _stationary(q: FloatArray) -> list[tuple[float, float]]:
    """Angles (degrees) and values where q_k has a stationary point on the circle (Z-eigenpairs)."""
    d = np.gradient(q)
    idx = np.where(np.sign(d) != np.sign(np.roll(d, 1)))[0]
    return [(float(np.degrees(_THETA[i])), float(q[i])) for i in idx]


@dataclass(frozen=True)
class Spectrum:
    """Spectral representation of a leg's curvature. Eigenvalues are sorted by |lambda|, descending."""

    whitener: FloatArray
    curvature_matrix: FloatArray  # M = L' H L, premium-normalised
    eigenvalues: FloatArray
    eigenvectors: FloatArray  # columns are the principal risk directions in whitened space
    energy_by_order: dict[int, float]
    energy_order2_closed_form: float
    anisotropy_by_order: dict[int, float]
    stationary_by_order: dict[int, list[tuple[float, float]]]
    t4_flat_spectrum: FloatArray
    q_by_order: dict[int, FloatArray]


def spectral_decomposition(leg: StraddleLeg) -> Spectrum:
    tz, L = whitened_tensors(leg)
    m = tz[2]
    lam, q = np.linalg.eigh(m)
    order = np.argsort(-np.abs(lam))
    lam, q = lam[order], q[:, order]
    f4 = tz[4].reshape(4, 4)
    q_by = {k: q_profile(tz[k], k) for k in (2, 3, 4)}
    return Spectrum(
        whitener=L,
        curvature_matrix=m,
        eigenvalues=lam,
        eigenvectors=q,
        energy_by_order={k: gaussian_energy(tz[k], k) for k in (2, 3, 4)},
        energy_order2_closed_form=float(0.25 * (np.trace(m) ** 2 + 2.0 * np.trace(m @ m))),
        anisotropy_by_order={k: float(np.abs(q_by[k]).max() / np.sqrt(np.mean(q_by[k] ** 2))) for k in (2, 3, 4)},
        stationary_by_order={k: _stationary(q_by[k]) for k in (2, 3, 4)},
        t4_flat_spectrum=np.sort(np.abs(np.linalg.eigvalsh((f4 + f4.T) / 2)))[::-1],
        q_by_order=q_by,
    )


# --------------------------------------------------------------------------- the risk vector


def circle_radius(confidence: float) -> float:
    """Radius containing ``confidence`` of a 2-D standard normal: sqrt(-2 ln(1 - confidence))."""
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must lie strictly between 0 and 1")
    return math.sqrt(-2.0 * math.log(1.0 - confidence))


@dataclass(frozen=True)
class CurvatureRisk:
    """Spectral risk vector for one straddle position, with the gate derived from it."""

    principal_curvatures: FloatArray
    principal_directions_deg: list[float]
    energy_by_order: dict[int, float]
    stress_angle_deg: float
    worst_loss: float  # per unit of opening premium, exact repricing, carry included
    stress_in_eigenbasis: FloatArray
    taylor_terms_on_stress_ray: dict[int, float]
    series_ok: bool
    domain_violations: int  # shocked states with spot <= 0 or vol <= 0, excluded and reported
    action: str


def curvature_risk(leg: StraddleLeg, units: float, confidence: float = 0.99) -> CurvatureRisk:
    """Where this position loses most on the confidence circle, and whether to trust the Taylor series.

    ``units`` is +1 for a long straddle and -1 for a short one (any non-zero size works; losses are
    per unit of opening premium of ONE straddle, scaled by ``units``).
    """
    if units == 0:
        raise ValueError("units must be non-zero")
    spec = spectral_decomposition(leg)
    r_c = circle_radius(confidence)
    z = np.stack([np.cos(_THETA), np.sin(_THETA)], axis=1) * r_c
    shocks = z @ spec.whitener.T  # x = L z, one row per angle
    spot, vol = leg.spot + shocks[:, 0], leg.vol + shocks[:, 1]
    valid = (spot > 0) & (vol > 0)
    violations = int((~valid).sum())
    if not valid.any():
        raise ValueError("every shocked state leaves the model's domain (spot or vol <= 0)")

    base = float(_exact_straddle(leg.spot, leg.strike, leg.vol, leg.expiry, leg.rate, leg.dividend))
    pnl = np.full(len(_THETA), np.nan)
    pnl[valid] = units * (_exact_straddle(spot[valid], leg.strike, vol[valid], leg.rolled_expiry,
                                          leg.rate, leg.dividend) - base) / base
    loss = np.where(valid, -pnl, -np.inf)
    k = int(np.argmax(loss))
    u = np.array([math.cos(_THETA[k]), math.sin(_THETA[k])])
    q = {kk: float(spec.q_by_order[kk][k] * r_c**kk) for kk in (2, 3, 4)}
    contracting = abs(q[3]) < abs(q[2]) and abs(q[4]) < abs(q[3])
    series_ok = contracting and violations == 0
    if violations:
        action = "REPRICE (shock leaves the valid domain)"
    elif not contracting:
        action = "REPRICE (series not contracting on the stress ray)"
    else:
        action = "USE TAYLOR-4"
    ev = spec.eigenvectors
    return CurvatureRisk(
        principal_curvatures=spec.eigenvalues,
        principal_directions_deg=[float(np.degrees(np.arctan2(ev[1, i], ev[0, i])) % 180) for i in range(2)],
        energy_by_order=spec.energy_by_order,
        stress_angle_deg=float(np.degrees(_THETA[k])),
        worst_loss=float(loss[k]),
        stress_in_eigenbasis=ev.T @ u,
        taylor_terms_on_stress_ray=q,
        series_ok=bool(series_ok),
        domain_violations=violations,
        action=action,
    )


def taylor_increment(leg: StraddleLeg, d_spot: float, d_vol: float, order: int = ORDER) -> float:
    """Premium-normalised straddle increment through degree ``order`` around the rolled state.

    Excludes carry. Compare with ``exact_increment`` to see how well the series holds at a shock.
    """
    if not 1 <= order <= ORDER:
        raise ValueError(f"order must be between 1 and {ORDER}")
    c = leg.coefficients()
    total = 0.0
    for i in range(_N):
        for j in range(_N - i):
            if 1 <= i + j <= order:
                total += c[i, j] * d_spot**i * d_vol**j
    return float(total / leg.premium)


def exact_increment(leg: StraddleLeg, d_spot: float, d_vol: float) -> float:
    """Premium-normalised exact repricing difference at the rolled expiry (carry excluded)."""
    new = _exact_straddle(leg.spot + d_spot, leg.strike, leg.vol + d_vol, leg.rolled_expiry,
                          leg.rate, leg.dividend)
    old = _exact_straddle(leg.spot, leg.strike, leg.vol, leg.rolled_expiry, leg.rate, leg.dividend)
    return float((new - old) / leg.premium)
