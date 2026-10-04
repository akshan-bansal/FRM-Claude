"""Membership and attrition: the synthetic generator, its estimator, and the map from real users.

The generator's output is SYNTHETIC by construction. The estimator test is a round trip (simulate
with known parameters, then recover them), which is what makes the map for real users trustworthy
once real data exists. The map is tested against the real approval-store schema, built by the store's
own code, so a schema change breaks the test instead of the mapping.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.analysis.edge_tails import skewness
from trading_live_claude.execution.approval_sqlite import _open_db
from trading_live_claude.sim.membership import (
    EstimatesRefused,
    MembershipEstimates,
    ScenarioParams,
    estimate,
    panel_from_approval_db,
    simulate,
)


def params(**kw) -> ScenarioParams:
    base = dict(periods=400, initial_members=5000, seed=1, capacity=40_000)
    base.update(kw)
    return ScenarioParams(**base)


# ---- the generator ---------------------------------------------------------------------------

def test_every_panel_says_it_is_synthetic_and_carries_its_parameters() -> None:
    pan = simulate(params(periods=30))
    assert pan.source == "synthetic" and pan.params["seed"] == 1 and pan.params["periods"] == 30
    assert "SYNTHETIC" in pan.describe() and any("SYNTHETIC" in n for n in pan.notes)


def test_the_seed_is_required_and_fixes_the_scenario() -> None:
    with pytest.raises(TypeError):
        ScenarioParams(periods=10, initial_members=10)                  # type: ignore[call-arg]
    a, b = simulate(params(periods=50)), simulate(params(periods=50))
    assert np.array_equal(a.members_end, b.members_end) and np.array_equal(a.transactions, b.transactions)
    assert not np.array_equal(a.members_end, simulate(params(periods=50, seed=2)).members_end)


def test_membership_accounting_balances_every_period() -> None:
    pan = simulate(params(periods=300))
    assert np.array_equal(pan.members_end, pan.members_start - pan.churned + pan.joined)
    assert np.array_equal(pan.members_start[1:], pan.members_end[:-1])
    assert (pan.members_end >= 0).all() and (pan.active_members <= pan.members_start).all()
    assert (pan.shock_loss <= pan.churned).all()                          # type: ignore[operator]


def test_transactions_are_proportionate_to_membership() -> None:
    pan = simulate(params(periods=500, sigma=0.0, cycles=(), shock_prob=0.0, tx_rate=0.4))
    per_member = pan.transactions / np.maximum(pan.members_start, 1)
    assert abs(float(per_member.mean()) - 0.4) < 0.02                      # activity 1, so rate = tx_rate
    big = simulate(params(periods=300, initial_members=20_000, capacity=200_000, sigma=0.0, cycles=(),
                          shock_prob=0.0, join_base=0.0, join_referral=0.0, churn_base=0.0))
    small = simulate(params(periods=300, initial_members=2_000, capacity=200_000, sigma=0.0, cycles=(),
                            shock_prob=0.0, join_base=0.0, join_referral=0.0, churn_base=0.0))
    assert 8.0 < big.transactions.sum() / small.transactions.sum() < 12.0   # ten times the members


def test_activity_reverts_to_a_cyclical_mean() -> None:
    pan = simulate(params(periods=1500, sigma=0.15, kappa=0.2))
    gap = np.log(pan.activity) - np.log(pan.cycle_mean)
    assert abs(float(gap.mean())) < 0.05 and float(gap.std()) < 0.3       # bounded around its cycle
    phi = float(np.sum(gap[1:] * gap[:-1]) / np.sum(gap[:-1] ** 2))
    assert abs(phi - 0.8) < 0.06                                           # AR(1) coefficient = 1 - kappa
    weekly = np.log(pan.cycle_mean)[:70]
    assert weekly.max() - weekly.min() > 0.4                               # the weekly cycle is really there


def test_attrition_rises_when_activity_sits_below_its_cycle() -> None:
    pan = simulate(params(periods=2000, initial_members=20_000, capacity=400_000, shock_prob=0.0,
                          churn_base=0.02, churn_activity_link=3.0, sigma=0.2))
    gap = np.log(pan.activity) - np.log(pan.cycle_mean)
    rate = pan.churned / np.maximum(pan.members_start, 1)
    assert rate[gap < np.quantile(gap, 0.25)].mean() > 1.3 * rate[gap > np.quantile(gap, 0.75)].mean()


def test_mass_exit_shocks_make_membership_change_left_skewed() -> None:
    calm = simulate(params(periods=2000, shock_prob=0.0, initial_members=8000))
    shocked = simulate(params(periods=2000, shock_prob=0.03, shock_scale=0.05, initial_members=8000))
    change = lambda p: (p.members_end - p.members_start) / np.maximum(p.members_start, 1)   # noqa: E731
    assert skewness(change(shocked)) < -1.0
    assert skewness(change(shocked)) < skewness(change(calm)) - 1.0
    assert shocked.shock_loss.max() > 0 and calm.shock_loss.max() == 0       # type: ignore[union-attr]
    assert change(shocked).min() > -0.5 - 0.1                                 # the cap holds


def test_scenario_parameters_are_validated() -> None:
    for bad in (dict(kappa=0.0), dict(kappa=1.5), dict(shock_cap=0.0), dict(churn_base=2.0),
                dict(cycles=((1.0, 0.1, 0.0),)), dict(periods=0), dict(sigma=-1.0)):
        with pytest.raises(ValueError):
            params(**bad)


# ---- the estimator (the round trip that makes the map useful) --------------------------------

def test_the_estimator_recovers_the_parameters_that_made_a_scenario() -> None:
    pan = simulate(params(periods=1500, initial_members=20_000, capacity=400_000, tx_rate=0.6, kappa=0.2,
                          sigma=0.12, churn_base=0.01, shock_prob=0.0, churn_activity_link=0.0))
    est = estimate(pan)
    assert isinstance(est, MembershipEstimates)
    assert abs(est.tx_per_member - 0.6) < 0.06
    assert abs(est.kappa - 0.2) < 0.06 and abs(est.sigma - 0.12) < 0.03
    assert abs(est.cycle_amplitudes[7.0] - 0.30) < 0.04
    assert abs(est.churn_median - 0.01) < 0.002


def test_the_estimator_refuses_what_it_cannot_support() -> None:
    assert isinstance(estimate(simulate(params(periods=20))), EstimatesRefused)
    tiny = simulate(params(periods=200, initial_members=10, join_base=0.0, join_referral=0.0))
    assert isinstance(estimate(tiny), EstimatesRefused)
    sparse = simulate(params(periods=200, initial_members=100, tx_rate=0.05, capacity=300))
    r = estimate(sparse)
    assert isinstance(r, EstimatesRefused)


# ---- the map from real users -----------------------------------------------------------------

def make_store(path: Path, cards, intents) -> Path:
    """cards: (card_id, created_at, revoked_at); intents: (intent_id, resolved_at, verdict, card_id)."""
    con = _open_db(path)                                   # the store's own schema and migrations
    for cid, created, revoked in cards:
        con.execute("INSERT INTO cards (card_id, pubkey_pem, created_at, revoked_at) VALUES (?,?,?,?)",
                    (cid, "pem", created, revoked))
    for iid, resolved, verdict, cid in intents:
        con.execute(
            "INSERT INTO intents (intent_id, issued_at, expires_at, resolved_at, verdict, consumed, broker,"
            " symbol, action, shares, entry, stop, target, notional_usd, risk_dollars, strategy, account,"
            " mode, thesis, intel_ref, nonce, canonical, signer_card_id) VALUES "
            "(?,?,?,?,?,1,'paper','BTC/USD','BUY',1,1,0.9,NULL,1,0.1,'s','a','paper','','', 'n','c',?)",
            (iid, resolved, resolved, resolved, verdict, cid))
    con.close()
    return path


def test_real_cards_and_signed_approvals_map_into_a_panel(tmp_path: Path) -> None:
    db = make_store(
        tmp_path / "approval.db",
        cards=[("c1", "2026-09-01T10:00:00+00:00", None),            # joined before the window
               ("c2", "2026-10-02T09:00:00+00:00", None),            # joins on day 2
               ("c3", "2026-10-02T11:00:00+00:00", "2026-10-04T08:00:00+00:00")],   # revoked on day 4
        intents=[("i1", "2026-10-01T12:00:00+00:00", "ACCEPT", "c1"),
                 ("i2", "2026-10-01T13:00:00+00:00", "ACCEPT", "c1"),
                 ("i3", "2026-10-02T12:00:00+00:00", "ACCEPT", "c2"),
                 ("i4", "2026-10-02T14:00:00+00:00", "DECLINE", "c2"),     # not a transaction
                 ("i5", "2026-10-03T14:00:00+00:00", "EXPIRED", None)])    # nobody signed it
    pan = panel_from_approval_db(db, start=date(2026, 10, 1), end=date(2026, 10, 5), lapse_days=0)
    assert pan.source == "real" and pan.periods == 5 and "SYNTHETIC" not in pan.describe()
    assert pan.members_start.tolist() == [1, 1, 3, 3, 2]
    assert pan.joined.tolist() == [0, 2, 0, 0, 0] and pan.churned.tolist() == [0, 0, 0, 1, 0]
    assert pan.transactions.tolist() == [2, 1, 0, 0, 0] and pan.active_members.tolist() == [1, 1, 0, 0, 0]
    assert np.array_equal(pan.members_end, pan.members_start - pan.churned + pan.joined)
    assert any("handful of devices" in n for n in pan.notes)


def test_lapse_attrition_is_inferred_labelled_and_off_when_zero(tmp_path: Path) -> None:
    db = make_store(tmp_path / "approval.db",
                    cards=[("c1", "2026-09-01T00:00:00+00:00", None)],
                    intents=[("i1", "2026-09-05T00:00:00+00:00", "ACCEPT", "c1")])
    off = panel_from_approval_db(db, start=date(2026, 9, 1), end=date(2026, 9, 30), lapse_days=0)
    assert off.churned.sum() == 0 and not any("INFERRED" in n for n in off.notes)
    on = panel_from_approval_db(db, start=date(2026, 9, 1), end=date(2026, 9, 30), lapse_days=10)
    assert on.churned.sum() == 1 and on.churned[14] == 1                     # 5 Sep + 10 days = 15 Sep
    assert any("INFERRED" in n for n in on.notes)


def test_an_empty_store_gives_an_empty_panel_that_says_so(tmp_path: Path) -> None:
    db = tmp_path / "approval.db"
    _open_db(db).close()
    pan = panel_from_approval_db(db, start=date(2026, 10, 1), end=date(2026, 10, 7), lapse_days=7)
    assert pan.members_end.sum() == 0 and pan.transactions.sum() == 0
    assert any("nothing to map" in n for n in pan.notes)
    assert isinstance(estimate(pan), EstimatesRefused)


def test_the_map_never_writes_and_rejects_bad_arguments(tmp_path: Path) -> None:
    db = make_store(tmp_path / "approval.db", cards=[("c1", "2026-09-01T00:00:00+00:00", None)], intents=[])
    before = db.read_bytes()
    panel_from_approval_db(db, start=date(2026, 10, 1), end=date(2026, 10, 2), lapse_days=3)
    assert db.read_bytes() == before
    with pytest.raises(ValueError):
        panel_from_approval_db(db, start=date(2026, 10, 2), end=date(2026, 10, 1), lapse_days=3)
    with pytest.raises(ValueError):
        panel_from_approval_db(db, start=date(2026, 10, 1), end=date(2026, 10, 2), lapse_days=-1)
    with pytest.raises(TypeError):
        panel_from_approval_db(db, start=date(2026, 10, 1), end=date(2026, 10, 2))   # type: ignore[call-arg]
    with pytest.raises(FileNotFoundError):
        panel_from_approval_db(tmp_path / "missing.db", start=date(2026, 10, 1), end=date(2026, 10, 2),
                               lapse_days=1)


def test_a_wrong_kind_of_database_is_not_silently_mapped(tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(sqlite3.OperationalError):
        panel_from_approval_db(other, start=date(2026, 10, 1), end=date(2026, 10, 2), lapse_days=1)
