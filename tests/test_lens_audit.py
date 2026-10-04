from __future__ import annotations

import ast
from pathlib import Path

import numpy as np
import pytest

from trading_live_claude.analysis import lens_audit
from trading_live_claude.analysis.lens_audit import (
    LensAudit,
    LensResult,
    _future_mask,
    _reliability_mask,
    audit_lenses,
    future_by_group,
    future_lens,
    reliability_by_group,
    reliability_lens,
    render_markdown,
)

N_PERM = 300


def _clustered(
    seed: int, *, signal: bool, n_groups: int = 4, per_group: int = 60, k: int = 5
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Events drawn from k feature clusters; with ``signal`` the outcome is the cluster's mean."""
    rng = np.random.default_rng(seed)
    centers = rng.normal(size=(k, 4)) * 3
    means = np.linspace(-1.0, 1.0, k) * 0.02
    feats, ys, gs, ts = [], [], [], []
    for gi in range(n_groups):
        c = rng.integers(0, k, per_group)
        feats.append(centers[c] + rng.normal(0, 0.3, (per_group, 4)))
        noise = rng.normal(0, 0.004, per_group)
        ys.append((means[c] if signal else 0.0) + noise + (0.0 if signal else rng.normal(0, 0.02, per_group)))
        gs.append(np.full(per_group, gi))
        ts.append(np.arange(per_group))
    return np.vstack(feats), np.concatenate(ys), np.concatenate(gs), np.concatenate(ts)


# --------------------------------------------------------------------------- reliability


def test_reliability_supported_when_outcome_follows_feature_cluster() -> None:
    x, y, g, _ = _clustered(0, signal=True)
    r = reliability_lens(x, y, g, n_perm=N_PERM)
    assert r.supported
    assert r.spearman > 0.5
    assert r.mae < r.mae_baseline


def test_reliability_rarely_supported_on_pure_noise() -> None:
    hits = 0
    for seed in range(6):
        x, y, g, _ = _clustered(seed, signal=False)
        hits += reliability_lens(x, y, g, n_perm=N_PERM, seed=seed).supported
    assert hits <= 1  # alpha = 0.05 over six seeds; two or more would mean a broken null


def test_reliability_needs_two_groups() -> None:
    x, y, _, _ = _clustered(1, signal=True)
    with pytest.raises(ValueError, match="two groups"):
        reliability_lens(x, y, np.zeros(len(y), dtype=int), n_perm=10)


# --------------------------------------------------------------------------- future + maturity


def test_future_supported_with_persistent_regimes() -> None:
    rng = np.random.default_rng(3)
    per, groups = 200, 2
    regime = np.repeat(rng.choice([-1.0, 1.0], size=per // 40), 40) * 0.02
    x = rng.normal(size=(per * groups, 3))
    y = np.concatenate([regime + rng.normal(0, 0.005, per) for _ in range(groups)])
    g = np.repeat(np.arange(groups), per)
    t = np.tile(np.arange(per), groups)
    r = future_lens(x, y, g, time=t, label_horizon=1, tau=5.0, recency=0.3, n_perm=N_PERM)
    assert r.supported


def test_future_mask_only_admits_matured_past_events() -> None:
    n, h = 30, 5
    g = np.zeros(n, dtype=np.int64)
    t = np.arange(n, dtype=float)
    m = _future_mask(g, t, h)
    i, j = np.indices((n, n))
    assert np.array_equal(m, (i - j) >= h)  # a label inside its own window is never readable
    assert not m[10, 6]  # event 6's window closes at 11, so event 10 may not read it
    assert m[10, 5]


def test_reliability_mask_embargoes_overlapping_windows_across_groups() -> None:
    g = np.array([0] * 10 + [1] * 10, dtype=np.int64)
    t = np.tile(np.arange(10, dtype=float), 2)
    m = _reliability_mask(g, t, 3)
    assert not m[0, 10]  # same date, other group: the shared market move is excluded
    assert not m[0, 12]  # still inside one window
    assert m[0, 15]
    assert not m[0, 1]  # same group is never a neighbour


def test_overlapping_windows_create_fake_skill_unless_horizon_is_right() -> None:
    """Forward returns over 3 bars overlap, so adjacent labels are correlated by construction."""
    rng = np.random.default_rng(11)
    n, h = 400, 3
    shocks = rng.normal(0, 0.01, n + h)
    y = np.array([shocks[i + 1 : i + 1 + h].sum() for i in range(n)])  # window (t, t+h]
    x = np.zeros((n, 1))  # constant feature: attention is recency-only
    g = np.zeros(n, dtype=np.int64)
    t = np.arange(n, dtype=float)
    leaky = future_lens(x, y, g, time=t, label_horizon=1, tau=0.15, recency=0.5, n_perm=N_PERM)
    honest = future_lens(x, y, g, time=t, label_horizon=h, tau=0.15, recency=0.5, n_perm=N_PERM)

    assert leaky.supported and leaky.spearman > 0.4  # skill that exists only because windows overlap
    assert not honest.supported and honest.spearman < 0.3


def test_future_requires_a_positive_label_horizon() -> None:
    x, y, g, t = _clustered(2, signal=True)
    with pytest.raises(ValueError, match="label_horizon"):
        future_lens(x, y, g, time=t, label_horizon=0, n_perm=10)


# --------------------------------------------------------------------------- inputs


def test_unknown_outcomes_are_dropped_not_zeroed() -> None:
    x, y, g, t = _clustered(4, signal=True)
    y = y.copy()
    y[:7] = np.nan  # e.g. forward windows that run off the end of the series
    r = reliability_lens(x, y, g, time=t, label_horizon=0, n_perm=50)
    assert r.n_scored + r.n_without_neighbours == len(y) - 7


def test_nan_features_are_rejected() -> None:
    x, y, g, _ = _clustered(5, signal=True)
    x[3, 1] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        reliability_lens(x, y, g, n_perm=10)


def test_too_few_events_are_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        reliability_lens(np.ones((5, 2)), np.arange(5.0), np.array([0, 0, 1, 1, 1]), n_perm=10)


def test_oversized_input_is_rejected_not_silently_truncated() -> None:
    x, y, g, _ = _clustered(6, signal=True)
    with pytest.raises(ValueError, match="max_events"):
        reliability_lens(x, y, g, n_perm=10, max_events=50)


def test_same_seed_same_answer() -> None:
    x, y, g, _ = _clustered(7, signal=True)
    a = reliability_lens(x, y, g, n_perm=N_PERM, seed=9)
    b = reliability_lens(x, y, g, n_perm=N_PERM, seed=9)
    assert a == b


# --------------------------------------------------------------------------- per-group verdicts


def test_by_group_gives_each_group_its_own_verdict() -> None:
    x, y, g, _ = _clustered(0, signal=True)
    rng = np.random.default_rng(99)
    noisy = g == 3
    y = y.copy()
    y[noisy] = rng.normal(0, 0.02, int(noisy.sum()))  # group 3 ignores the shared structure
    res = reliability_by_group(x, y, g, n_perm=N_PERM)
    assert set(res) == {0, 1, 2, 3}
    assert all(res[k].supported for k in (0, 1, 2))
    assert not res[3].supported  # a noisy symbol is not rescued by its neighbours' structure


def test_by_group_keys_are_the_original_labels() -> None:
    x, y, g, _ = _clustered(1, signal=True, n_groups=3)
    names = np.array(["AAA", "BBB", "CCC"])[g]
    res = reliability_by_group(x, y, names, n_perm=50)
    assert set(res) == {"AAA", "BBB", "CCC"}
    assert all(isinstance(k, str) for k in res)


def test_by_group_omits_groups_too_small_to_score() -> None:
    x, y, g, _ = _clustered(2, signal=True, n_groups=3)
    keep = np.ones(len(g), dtype=bool)
    small = np.flatnonzero(g == 2)[5:]  # leave group 2 with five events
    keep[small] = False
    res = reliability_by_group(x[keep], y[keep], g[keep], n_perm=50)
    assert set(res) == {0, 1}


def test_future_by_group_reads_only_each_groups_own_history() -> None:
    rng = np.random.default_rng(3)
    per, groups = 200, 2
    regime = np.repeat(rng.choice([-1.0, 1.0], size=per // 40), 40) * 0.02
    x = rng.normal(size=(per * groups, 3))
    y = np.concatenate([regime + rng.normal(0, 0.005, per) for _ in range(groups)])
    g = np.repeat(np.array(["AAA", "BBB"]), per)
    t = np.tile(np.arange(per), groups)
    res = future_by_group(x, y, g, time=t, label_horizon=1, tau=5.0, recency=0.3, n_perm=N_PERM)
    assert set(res) == {"AAA", "BBB"}
    assert all(r.supported for r in res.values())


def test_by_group_covers_exactly_the_events_the_pooled_lens_scores() -> None:
    """Per-group scoring partitions the pooled lens's scored events; it adds none and drops none."""
    x, y, g, t = _clustered(4, signal=True)
    pooled = future_lens(x, y, g, time=t, label_horizon=1, n_perm=N_PERM, seed=5)
    split = future_by_group(x, y, g, time=t, label_horizon=1, n_perm=N_PERM, seed=5)
    n_split = sum(r.n_scored for r in split.values())
    assert pooled.n_scored == n_split


# --------------------------------------------------------------------------- reporting + boundary


def test_markdown_names_each_lens_and_gives_a_verdict() -> None:
    x, y, g, t = _clustered(8, signal=True)
    audit = audit_lenses(x, y, g, time=t, label_horizon=1, n_perm=50)
    assert isinstance(audit, LensAudit) and isinstance(audit.reliability, LensResult)
    md = render_markdown(audit)
    assert "| reliability |" in md and "| future |" in md
    assert "supported" in md or "not distinguishable from chance" in md


def test_lens_audit_never_imports_the_trading_path() -> None:
    """The ruler must not be callable from, or call into, anything that moves money."""
    forbidden = {"execution", "risk", "brokers", "strategies", "monitor", "daemon", "scoring", "tune", "cli"}
    tree = ast.parse(Path(lens_audit.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            parts = module.split(".")
            imported |= {parts[0]} if node.level == 0 else set(parts)
    assert not (imported & forbidden), imported & forbidden
