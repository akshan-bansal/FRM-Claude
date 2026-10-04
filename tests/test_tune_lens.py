from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from click.testing import Result
from numpy.typing import NDArray

import trading_live_claude.tune as tune_mod
from trading_live_claude.tune import (
    CandidateEvents,
    TuneResult,
    audit_candidates,
    entry_events,
    lens_cell,
    run_tune,
)

H = 10


def _frame(n: int = 400, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return pd.DataFrame({
        "time": pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC"),
        "open": close, "high": close * 1.002, "low": close * 0.998, "close": close, "volume": 1.0,
    })


def _days(df: pd.DataFrame) -> NDArray[np.float64]:
    days: NDArray[np.float64] = (
        (df["time"] - pd.Timestamp("1970-01-01", tz="UTC")).dt.days.to_numpy(dtype=float))
    return days


# --------------------------------------------------------------------------- entry_events


def test_features_never_look_ahead_but_outcomes_do() -> None:
    df = _frame()
    entry = pd.Series(np.arange(len(df)) % 7 == 0)
    k = 250
    df2 = df.copy()
    df2.loc[k + 1 :, "close"] = df2.loc[k + 1 :, "close"] * 1.5  # rewrite the future after bar k
    a = entry_events("s", "AAA", df, entry, horizon=H)
    b = entry_events("s", "AAA", df2, entry, horizon=H)
    assert a is not None and b is not None
    cut = _days(df)[k]
    early_a, early_b = a.day <= cut, b.day <= cut
    assert np.array_equal(a.day[early_a], b.day[early_b])
    assert np.allclose(a.features[early_a], b.features[early_b])  # bars up to k know nothing of k+1
    near_end = (a.day > _days(df)[k - H]) & (a.day <= cut)
    assert not np.allclose(a.outcome[near_end], b.outcome[: len(a.outcome)][near_end])  # labels do


def test_unfinished_forward_windows_and_unready_trailing_windows_are_dropped() -> None:
    df = _frame()
    ev = entry_events("s", "AAA", df, pd.Series(True, index=df.index), horizon=H)
    assert ev is not None
    days = _days(df)
    assert ev.day.max() <= days[len(df) - 1 - H]  # a window that runs off the end has no outcome
    assert ev.day.min() >= days[60]  # ret_60 needs 60 bars of history
    assert np.isfinite(ev.features).all() and np.isfinite(ev.outcome).all()


def test_no_entries_or_no_dates_means_no_events() -> None:
    df = _frame()
    assert entry_events("s", "AAA", df, pd.Series(False, index=df.index), horizon=H) is None
    assert entry_events("s", "AAA", df.drop(columns="time"), pd.Series(True, index=df.index), horizon=H) is None


# --------------------------------------------------------------------------- audit_candidates


def _events(strategy: str, symbol_ix: int, symbol: str, *, signal: bool, seed: int, n: int = 80) -> CandidateEvents:
    rng = np.random.default_rng(seed)
    centers = np.random.default_rng(1234).normal(size=(5, 4)) * 3  # shared across symbols
    means = np.linspace(-1.0, 1.0, 5) * 0.02
    c = rng.integers(0, 5, n)
    outcome = (means[c] if signal else 0.0) + rng.normal(0, 0.004 if signal else 0.02, n)
    return CandidateEvents(
        strategy=strategy, symbol=symbol,
        day=np.arange(n, dtype=float) * 30 + symbol_ix * 11,
        features=centers[c] + rng.normal(0, 0.3, (n, 4)),
        outcome=outcome,
    )


def test_audit_says_yes_when_entry_state_predicts_outcome_everywhere() -> None:
    evs = [_events("s", i, sym, signal=True, seed=i) for i, sym in enumerate(("AAA", "BBB", "CCC", "DDD"))]
    out = audit_candidates(evs, label_horizon=H, n_perm=300)
    assert set(out) == {("s", "AAA"), ("s", "BBB"), ("s", "CCC"), ("s", "DDD")}
    for v in out.values():
        assert v.reliability is not None and v.reliability.supported
        assert v.future is not None and v.future.supported
        assert v.n_events == 80


def test_audit_rarely_says_yes_on_noise() -> None:
    evs = [_events("s", i, sym, signal=False, seed=40 + i) for i, sym in enumerate(("AAA", "BBB", "CCC", "DDD"))]
    out = audit_candidates(evs, label_horizon=H, n_perm=300)
    verdicts = [r.supported for v in out.values() for r in (v.reliability, v.future) if r is not None]
    assert len(verdicts) == 8
    assert sum(verdicts) <= 1  # eight tests at alpha 0.05: two or more would mean a broken null


def test_too_little_data_is_na_not_a_verdict() -> None:
    evs = [_events("s", i, sym, signal=True, seed=i) for i, sym in enumerate(("AAA", "BBB", "CCC"))]
    tiny = _events("s", 3, "TINY", signal=True, seed=9, n=5)
    out = audit_candidates([*evs, tiny], label_horizon=H, n_perm=100)
    assert out[("s", "TINY")].reliability is None and out[("s", "TINY")].future is None
    assert out[("s", "AAA")].reliability is not None
    assert lens_cell(None) == "n/a"


def test_a_lone_symbol_has_no_transfer_verdict() -> None:
    out = audit_candidates([_events("s", 0, "AAA", signal=True, seed=1)], label_horizon=H, n_perm=100)
    assert out[("s", "AAA")].reliability is None  # nothing to transfer from


def test_answer_does_not_depend_on_thread_completion_order() -> None:
    evs = [_events("s", i, sym, signal=True, seed=i) for i, sym in enumerate(("AAA", "BBB", "CCC"))]
    a = audit_candidates(evs, label_horizon=H, n_perm=100)
    b = audit_candidates(list(reversed(evs)), label_horizon=H, n_perm=100)
    assert a == b


def test_lens_cell_shows_verdict_and_numbers() -> None:
    evs = [_events("s", i, sym, signal=True, seed=i) for i, sym in enumerate(("AAA", "BBB", "CCC"))]
    v = audit_candidates(evs, label_horizon=H, n_perm=100)[("s", "AAA")]
    cell = lens_cell(v.reliability)
    assert cell.startswith("yes (rho +") and "p " in cell


# --------------------------------------------------------------------------- run_tune is unchanged by the audit


class _FakeMarket:
    def __init__(self, broker: object, cache: object = None) -> None:
        pass

    def history(self, symbol: str, years: float, interval: str = "1d") -> pd.DataFrame:
        return _frame(600, seed=sum(map(ord, symbol)))


def _tune(
    monkeypatch: pytest.MonkeyPatch, audit_sink: list[CandidateEvents] | None = None
) -> list[TuneResult]:
    monkeypatch.setattr(tune_mod, "MarketData", _FakeMarket)
    stub: Any = object()  # the fake market ignores the broker and cache entirely
    return run_tune(
        stub, stub,
        symbols=("AAA", "BBB", "CCC"), strategies=("bollinger", "ema_crossover"),
        years=2.0, parallel=1, audit_sink=audit_sink,
    )


def _board(rs: list[TuneResult]) -> list[tuple[str, str, float, int]]:
    return [(r.strategy, r.symbol, r.score, r.num_trades) for r in rs]


def test_ranking_is_identical_with_and_without_the_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = _tune(monkeypatch)
    sink: list[CandidateEvents] = []
    audited = _tune(monkeypatch, audit_sink=sink)
    assert _board(plain) == _board(audited)
    assert plain and sink and all(isinstance(e, CandidateEvents) for e in sink)


def test_a_fault_in_the_audit_cannot_cost_a_candidate_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = _tune(monkeypatch)

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("audit fault")

    monkeypatch.setattr(tune_mod, "entry_events", boom)
    sink: list[CandidateEvents] = []
    audited = _tune(monkeypatch, audit_sink=sink)
    assert _board(plain) == _board(audited)  # every row survives
    assert sink == []


def test_no_audit_field_reaches_the_persisted_scoreboard() -> None:
    """``asdict(TuneResult)`` is what pick_config writes into trading.yaml; it must not grow."""
    row = TuneResult("s", "AAA", 1.0, -0.1, 0.1, 0.5, 20, 1.0)
    assert set(asdict(row)) == {
        "strategy", "symbol", "sharpe", "max_drawdown", "cagr", "win_rate", "num_trades",
        "score", "sortino", "precision", "recall",
    }


# --------------------------------------------------------------------------- CLI


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, audit_explodes: bool = False) -> SimpleNamespace:
    import trading_live_claude.cli as cli

    seen = SimpleNamespace(sink="unset", applied=0, cli=cli)
    settings = SimpleNamespace(log_level="WARNING", log_dir=tmp_path, data_cache_dir=tmp_path,
                               execution_mode="paper", questrade_env="practice")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "_make_questrade", lambda _s: object())
    monkeypatch.setattr(cli, "CandleCache", lambda _d: object())

    rows = [TuneResult("bollinger", s, 1.0, -0.1, 0.1, 0.5, 20, 2.0 - i * 0.1)
            for i, s in enumerate(("AAA", "BBB", "CCC"))]

    def fake_run_tune(*_a: object, audit_sink: list[CandidateEvents] | None = None, **_k: object) -> list[TuneResult]:
        seen.sink = audit_sink
        if audit_sink is not None:
            audit_sink.extend(_events("bollinger", i, r.symbol, signal=True, seed=i) for i, r in enumerate(rows))
        return rows

    def fake_apply(_results: list[TuneResult], *, dry_run: bool = False) -> dict[str, object]:
        seen.applied += 1
        return {"default_strategy": "bollinger", "default_symbols": "AAA,BBB,CCC"}

    monkeypatch.setattr(cli, "run_tune", fake_run_tune)
    monkeypatch.setattr(cli, "apply_tune", fake_apply)
    if audit_explodes:
        def boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("audit fault")
        monkeypatch.setattr(cli, "audit_candidates", boom)
    return seen


def _invoke(seen: SimpleNamespace, *flags: str) -> Result:
    from typer.testing import CliRunner

    return CliRunner().invoke(seen.cli.app, ["tune", "--dry-run", *flags], env={"COLUMNS": "220"})


def test_cli_prints_the_audit_only_when_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    off = _cli(monkeypatch, tmp_path)
    res = _invoke(off)
    assert res.exit_code == 0, res.output
    assert off.sink is None and "Lens audit" not in res.output  # default run is untouched

    on = _cli(monkeypatch, tmp_path)
    res = _invoke(on, "--lens-audit", "--lens-permutations", "100")
    assert res.exit_code == 0, res.output
    assert "Lens audit" in res.output and "advisory only" in res.output
    assert isinstance(on.sink, list) and on.applied == 1  # tune still completes and applies


def test_cli_audit_fault_does_not_stop_the_tune(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = _cli(monkeypatch, tmp_path, audit_explodes=True)
    res = _invoke(seen, "--lens-audit")
    assert res.exit_code == 0, res.output
    assert "Lens audit skipped" in res.output
    assert seen.applied == 1  # apply_tune still ran
