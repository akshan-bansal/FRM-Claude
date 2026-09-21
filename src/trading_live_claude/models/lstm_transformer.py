"""LSTM -> Transformer hybrid for next-bar multi-asset return forecasting, trained with SGD+momentum.

Architecture (per window of ``lookback`` bars, ``F`` features per bar):

    x (B, L, F) -> LSTM(hidden) -> + learned positional embedding -> TransformerEncoder (pre-norm,
    ``n_layers`` x ``n_heads`` self-attention over the L LSTM states) -> last position -> Linear(N)

The LSTM gives each position a recurrent summary of everything before it; self-attention then lets
the final position weigh any earlier state directly instead of only through the recurrence.

Optimizer is plain ``torch.optim.SGD`` with a momentum term (Nesterov by default), gradient-norm
clipping, and early stopping on a chronologically-later validation slice. No shuffling across the
train/val boundary: windows are built so that ``X[i]`` covers feature rows ``i .. i+L-1`` and the
target is row ``i+L`` — the forecast never sees the bar it predicts.

Requires the optional ``deep`` extra (``torch``). Nothing in the package imports this module
eagerly, so the rest of the framework runs without torch installed.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt
import torch
from torch import nn

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class TrainConfig:
    lookback: int = 30
    hidden: int = 32
    n_heads: int = 4
    n_layers: int = 2
    dropout: float = 0.1
    lr: float = 0.01
    momentum: float = 0.9
    nesterov: bool = True
    weight_decay: float = 1e-4
    epochs: int = 300
    batch_size: int = 32
    clip_norm: float = 1.0
    patience: int = 30
    seed: int = 0


class LSTMTransformer(nn.Module):
    def __init__(self, n_features: int, n_outputs: int, cfg: TrainConfig) -> None:
        super().__init__()
        self.lstm = nn.LSTM(n_features, cfg.hidden, batch_first=True)
        self.pos = nn.Parameter(torch.zeros(1, cfg.lookback, cfg.hidden))
        layer = nn.TransformerEncoderLayer(
            d_model=cfg.hidden, nhead=cfg.n_heads, dim_feedforward=2 * cfg.hidden,
            dropout=cfg.dropout, batch_first=True, norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(layer, cfg.n_layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(cfg.hidden)
        self.head = nn.Linear(cfg.hidden, n_outputs)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, _ = self.lstm(x)
        h = h + self.pos[:, : h.shape[1]]
        z = self.encoder(h)
        out: torch.Tensor = self.head(self.norm(z[:, -1]))
        return out


def make_windows(features: FloatArray, targets: FloatArray,
                 lookback: int) -> tuple[FloatArray, FloatArray]:
    """``X[i] = features[i : i+lookback]``, ``y[i] = targets[i+lookback]``.

    ``features`` and ``targets`` share a row index (row = bar). The target row is strictly after
    every feature row in its window, so a window can never contain the bar it forecasts.
    """
    if len(features) != len(targets):
        raise ValueError("features and targets must have the same number of rows")
    s = len(features) - lookback
    if s <= 0:
        raise ValueError(f"need more than lookback={lookback} rows, got {len(features)}")
    x = np.stack([features[i : i + lookback] for i in range(s)])
    y = targets[lookback:]
    return x.astype(np.float64), y.astype(np.float64)


@dataclass
class TrainResult:
    model: LSTMTransformer
    train_loss: list[float] = field(default_factory=list)
    val_loss: list[float] = field(default_factory=list)
    best_epoch: int = 0


def _t(a: FloatArray) -> torch.Tensor:
    return torch.as_tensor(a, dtype=torch.float32)


def build_optimizer(model: nn.Module, cfg: TrainConfig) -> torch.optim.SGD:
    return torch.optim.SGD(model.parameters(), lr=cfg.lr, momentum=cfg.momentum,
                           nesterov=cfg.nesterov and cfg.momentum > 0,
                           weight_decay=cfg.weight_decay)


def train(x_train: FloatArray, y_train: FloatArray, x_val: FloatArray, y_val: FloatArray,
          cfg: TrainConfig) -> TrainResult:
    """Minibatch SGD+momentum on MSE; keeps the weights from the best validation epoch."""
    torch.manual_seed(cfg.seed)
    rng = np.random.default_rng(cfg.seed)
    model = LSTMTransformer(x_train.shape[2], y_train.shape[1], cfg)
    opt = build_optimizer(model, cfg)
    loss_fn = nn.MSELoss()
    xt, yt, xv, yv = _t(x_train), _t(y_train), _t(x_val), _t(y_val)

    result = TrainResult(model=model)
    best_val, best_state, stale = float("inf"), copy.deepcopy(model.state_dict()), 0
    for epoch in range(cfg.epochs):
        model.train()
        # Shuffling windows *within* the training slice is fine: each window is self-contained
        # and every training target precedes every validation target.
        order = rng.permutation(len(xt))
        total = 0.0
        for start in range(0, len(order), cfg.batch_size):
            idx = torch.as_tensor(order[start : start + cfg.batch_size])
            opt.zero_grad()
            loss = loss_fn(model(xt[idx]), yt[idx])
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), cfg.clip_norm)
            opt.step()
            total += float(loss.item()) * len(idx)
        result.train_loss.append(total / len(xt))

        model.eval()
        with torch.no_grad():
            v = float(loss_fn(model(xv), yv).item())
        result.val_loss.append(v)
        if v < best_val - 1e-6:
            best_val, best_state, stale = v, copy.deepcopy(model.state_dict()), 0
            result.best_epoch = epoch
        else:
            stale += 1
            if stale >= cfg.patience:
                break

    model.load_state_dict(best_state)
    model.eval()
    return result


def predict(model: LSTMTransformer, x: FloatArray) -> FloatArray:
    model.eval()
    with torch.no_grad():
        out: FloatArray = model(_t(x)).numpy().astype(np.float64)
    return out
