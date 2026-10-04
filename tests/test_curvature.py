from __future__ import annotations

import ast
import math
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.analysis import curvature
from trading_live_claude.analysis.curvature import (
    StraddleLeg,
    _bsm_jets,
    _exact_straddle,
    _Jet,
    circle_radius,
    curvature_risk,
    exact_increment,
    gaussian_energy,
    spectral_decomposition,
    taylor_increment,
    whitened_tensors,
    whitener,
)

S, K, VOL, T, R, Q = 100.0, 100.0, 0.25, 0.25, 0.04, 0.01


def _leg(**kw: float) -> StraddleLeg:
    base = {"spot": S, "strike": K, "vol": VOL, "expiry": T, "rate": R, "dividend": Q,
            "horizon": 5 / 252, "sd_spot": 2.68, "sd_vol": 0.067, "rho": -0.7}
    base.update(kw)
    return StraddleLeg(**base)


def _phi(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# --------------------------------------------------------------------------- exact derivatives


def test_jet_log_and_reciprocal_match_their_known_series() -> None:
    x = _Jet.variable(2.0, 0)
    assert np.allclose(x.log().c[:, 0], [math.log(2.0), 1 / 2, -1 / 8, 1 / 24, -1 / 64])
    assert np.allclose(x.reciprocal().c[:, 0], [1 / 2, -1 / 4, 1 / 8, -1 / 16, 1 / 32])


def test_jets_reproduce_the_closed_form_greeks() -> None:
    call, _ = _bsm_jets(S, K, VOL, T, R, Q)
    rt = math.sqrt(T)
    d1 = (math.log(S / K) + (R - Q + 0.5 * VOL**2) * T) / (VOL * rt)
    d2 = d1 - VOL * rt
    eq = math.exp(-Q * T)
    c = call.c
    assert c[1, 0] == pytest.approx(eq * 0.5 * math.erfc(-d1 / math.sqrt(2)), rel=1e-12)  # delta
    assert 2 * c[2, 0] == pytest.approx(eq * _phi(d1) / (S * VOL * rt), rel=1e-12)  # gamma
    vega = S * eq * _phi(d1) * rt
    assert c[0, 1] == pytest.approx(vega, rel=1e-12)
    assert c[1, 1] == pytest.approx(-eq * _phi(d1) * d2 / VOL, rel=1e-11)  # vanna
    assert 2 * c[0, 2] == pytest.approx(vega * d1 * d2 / VOL, rel=1e-11)  # vomma


def test_jet_price_matches_the_independent_vectorised_price() -> None:
    call, put = _bsm_jets(S, K, VOL, T, R, Q)
    assert call.c[0, 0] + put.c[0, 0] == pytest.approx(float(_exact_straddle(S, K, VOL, T, R, Q)), rel=1e-12)


def test_put_call_parity_shows_up_in_the_derivatives() -> None:
    call, put = _bsm_jets(S, K, VOL, T, R, Q)
    for i in range(5):
        for j in range(5 - i):
            if i + j >= 2:
                assert call.c[i, j] == pytest.approx(put.c[i, j], abs=1e-12)  # C - P is linear in S
    assert call.c[1, 0] - put.c[1, 0] == pytest.approx(math.exp(-Q * T), rel=1e-12)
    assert call.c[0, 1] == pytest.approx(put.c[0, 1], rel=1e-12)


def test_taylor_error_falls_with_order_and_at_the_order_five_rate() -> None:
    """If any coefficient through order 4 were wrong, halving the shock would not cut the error ~32x."""
    leg = _leg()
    errs = [abs(exact_increment(leg, 3.0, 0.03) - taylor_increment(leg, 3.0, 0.03, k)) for k in (1, 2, 3, 4)]
    assert errs[0] > errs[1] > errs[2] > errs[3]
    e_full = abs(exact_increment(leg, 3.0, 0.03) - taylor_increment(leg, 3.0, 0.03, 4))
    e_half = abs(exact_increment(leg, 1.5, 0.015) - taylor_increment(leg, 1.5, 0.015, 4))
    assert 20 < e_full / e_half < 45


# --------------------------------------------------------------------------- spectrum


def test_premium_is_sensible_for_an_atm_three_month_straddle() -> None:
    assert 9.0 < _leg().premium < 11.0  # ~ 0.8 * S * vol * sqrt(T) = 10


def test_tensor_entries_are_the_taylor_coefficients_and_whitening_is_a_change_of_variables() -> None:
    """Tensors must equal the series the Taylor test validated, and T_z[z] must equal T[L z]."""
    leg = _leg()
    t = curvature.straddle_tensors(leg)
    tz, L = whitened_tensors(leg)
    c, prem = leg.coefficients(), leg.premium
    rng = np.random.default_rng(2)
    for _ in range(5):
        a, b = rng.normal(0, 1.0), rng.normal(0, 0.02)
        x = np.array([a, b])
        for k, einsum in ((2, "ab,a,b"), (3, "abc,a,b,c"), (4, "abcd,a,b,c,d")):
            series = sum(c[i, k - i] * a**i * b ** (k - i) for i in range(k + 1)) / prem
            assert np.einsum(einsum, t[k], *([x] * k)) / math.factorial(k) == pytest.approx(series, rel=1e-10)
        z = np.linalg.solve(L, x)
        for k, einsum in ((2, "ab,a,b"), (3, "abc,a,b,c"), (4, "abcd,a,b,c,d")):
            assert np.einsum(einsum, tz[k], *([z] * k)) == pytest.approx(
                np.einsum(einsum, t[k], *([x] * k)), rel=1e-10)


def test_whitener_reproduces_the_shock_covariance() -> None:
    leg = _leg()
    L = whitener(leg)
    cov = np.array([[leg.sd_spot**2, leg.rho * leg.sd_spot * leg.sd_vol],
                    [leg.rho * leg.sd_spot * leg.sd_vol, leg.sd_vol**2]])
    assert np.allclose(L @ L.T, cov)


def test_spectrum_is_a_proper_eigendecomposition() -> None:
    spec = spectral_decomposition(_leg())
    m, lam, q = spec.curvature_matrix, spec.eigenvalues, spec.eigenvectors
    assert np.allclose(m, m.T)
    assert np.allclose(q.T @ q, np.eye(2))
    assert np.allclose(q @ np.diag(lam) @ q.T, m)
    assert abs(lam[0]) >= abs(lam[1])  # sorted by magnitude
    assert lam.sum() > 0  # a long ATM straddle is convex overall


def test_order_two_energy_closed_form_equals_quadrature() -> None:
    spec = spectral_decomposition(_leg())
    assert spec.energy_by_order[2] == pytest.approx(spec.energy_order2_closed_form, rel=1e-10)


def test_energies_agree_with_monte_carlo_at_every_order() -> None:
    leg = _leg()
    tz, _ = whitened_tensors(leg)
    z = np.random.default_rng(5).standard_normal((600_000, 2))
    t2 = 0.5 * np.einsum("ab,na,nb->n", tz[2], z, z)
    t3 = np.einsum("abc,na,nb,nc->n", tz[3], z, z, z) / 6
    t4 = np.einsum("abcd,na,nb,nc,nd->n", tz[4], z, z, z, z) / 24
    for k, t, tol in ((2, t2, 0.02), (3, t3, 0.03), (4, t4, 0.06)):
        assert gaussian_energy(tz[k], k) == pytest.approx(float(np.mean(t**2)), rel=tol)


def test_circle_radius_is_the_two_dimensional_chi_quantile() -> None:
    assert circle_radius(0.99) == pytest.approx(math.sqrt(-2 * math.log(0.01)))
    with pytest.raises(ValueError, match="confidence"):
        circle_radius(1.0)


# --------------------------------------------------------------------------- risk vector and gate


def test_small_shocks_pass_the_series_gate() -> None:
    risk = curvature_risk(_leg(sd_spot=0.4, sd_vol=0.002), units=-1)
    assert risk.series_ok and risk.action == "USE TAYLOR-4"
    assert risk.domain_violations == 0


def test_shocks_that_leave_the_domain_are_reported_not_clipped() -> None:
    risk = curvature_risk(_leg(sd_spot=30.0, sd_vol=0.15), units=-1)
    assert risk.domain_violations > 0
    assert not risk.series_ok and "domain" in risk.action


def test_a_short_straddle_can_lose_more_than_a_long_one_on_the_same_shocks() -> None:
    leg = _leg()
    short, long_ = curvature_risk(leg, units=-1), curvature_risk(leg, units=+1)
    assert short.worst_loss > long_.worst_loss


def test_losses_scale_linearly_with_position_size() -> None:
    leg = _leg()
    one, two = curvature_risk(leg, units=-1), curvature_risk(leg, units=-2)
    assert two.worst_loss == pytest.approx(2 * one.worst_loss, rel=1e-12)
    assert two.stress_angle_deg == one.stress_angle_deg


def test_the_stress_ray_terms_match_the_profile_at_the_confidence_radius() -> None:
    leg = _leg()
    risk = curvature_risk(leg, units=-1)
    spec = spectral_decomposition(leg)
    theta = math.radians(risk.stress_angle_deg)
    r = circle_radius(0.99)
    for k in (2, 3, 4):
        u = np.array([math.cos(theta), math.sin(theta)])
        direct = {2: np.einsum("ab,a,b", whitened_tensors(leg)[0][2], u, u) / 2,
                  3: np.einsum("abc,a,b,c", whitened_tensors(leg)[0][3], u, u, u) / 6,
                  4: np.einsum("abcd,a,b,c,d", whitened_tensors(leg)[0][4], u, u, u, u) / 24}[k]
        assert risk.taylor_terms_on_stress_ray[k] == pytest.approx(direct * r**k, rel=1e-3, abs=1e-9)
    assert spec.eigenvalues.shape == (2,)


def test_same_inputs_same_answer() -> None:
    a, b = curvature_risk(_leg(), units=-1), curvature_risk(_leg(), units=-1)
    assert a.worst_loss == b.worst_loss and a.action == b.action
    assert np.array_equal(a.principal_curvatures, b.principal_curvatures)


# --------------------------------------------------------------------------- inputs


@pytest.mark.parametrize("bad", [
    {"spot": 0.0}, {"vol": -0.1}, {"expiry": 0.0}, {"horizon": 0.3}, {"sd_spot": 0.0}, {"rho": 1.0},
])
def test_invalid_legs_are_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        _leg(**bad)


def test_zero_units_is_rejected() -> None:
    with pytest.raises(ValueError, match="units"):
        curvature_risk(_leg(), units=0)


def test_from_daily_scales_by_root_time() -> None:
    leg = StraddleLeg.from_daily(spot=S, strike=K, vol=VOL, expiry=T, daily_return_sd=0.012,
                                 daily_vol_sd=0.03, rho=-0.7, horizon_days=5, rate=R, dividend=Q)
    assert leg.sd_spot == pytest.approx(0.012 * math.sqrt(5) * S)
    assert leg.sd_vol == pytest.approx(0.03 * math.sqrt(5))
    assert leg.horizon == pytest.approx(5 / 252)


# --------------------------------------------------------------------------- boundary


def test_curvature_needs_nothing_but_numpy_and_the_standard_library() -> None:
    """Self-contained by design: no sibling bsm modules, no scipy, nothing that touches trading."""
    tree = ast.parse(Path(curvature.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "math", "collections", "dataclasses", "numpy"}, imported
