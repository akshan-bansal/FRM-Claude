"""LSTM-Transformer: window construction can't leak the target, the optimizer really is
SGD-with-momentum, and the model learns a planted lagged signal on synthetic data."""
from __future__ import annotations

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from trading_live_claude.models.lstm_transformer import (  # noqa: E402
    LSTMTransformer,
    TrainConfig,
    build_optimizer,
    make_windows,
    predict,
    train,
)


def test_windows_never_contain_their_target() -> None:
    feats = np.arange(20, dtype=np.float64).reshape(-1, 1)
    x, y = make_windows(feats, feats, lookback=5)
    assert x.shape == (15, 5, 1)
    # the target row index is strictly after the last feature row in its window
    assert (y[:, 0] == x[:, -1, 0] + 1).all()


def test_optimizer_is_sgd_with_momentum() -> None:
    cfg = TrainConfig(lookback=4, hidden=8, n_heads=2, n_layers=1, momentum=0.9)
    opt = build_optimizer(LSTMTransformer(3, 2, cfg), cfg)
    assert isinstance(opt, torch.optim.SGD)
    assert opt.defaults["momentum"] == 0.9
    assert opt.defaults["nesterov"] is True


def test_learns_planted_lag_signal() -> None:
    rng = np.random.default_rng(0)
    t = 700
    driver = rng.standard_normal(t)
    target = np.zeros((t, 2))
    target[1:, 0] = 0.9 * driver[:-1]          # asset 0 follows yesterday's driver
    target[:, 1] = rng.standard_normal(t)      # asset 1 is pure noise
    target[:, 0] += 0.3 * rng.standard_normal(t)
    feats = np.column_stack([driver, target])
    x, y = make_windows(feats, target, lookback=10)
    cfg = TrainConfig(lookback=10, hidden=16, n_heads=2, n_layers=1, epochs=60, lr=0.02,
                      patience=15, dropout=0.0)
    res = train(x[:500], y[:500], x[500:600], y[500:600], cfg)
    pred = predict(res.model, x[600:])
    corr = np.corrcoef(pred[:, 0], y[600:, 0])[0, 1]
    assert corr > 0.8
    assert res.val_loss[res.best_epoch] < res.val_loss[0]
