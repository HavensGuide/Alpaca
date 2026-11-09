"""Machine-learning strategies with walk-forward training (no look-ahead).

Subclass ``ModelStrategy`` and implement ``make_model()`` (anything with
``fit(X, y)`` and ``predict(X)``: scikit-learn, a PyTorch wrapper, XGBoost, ...).
Optionally override ``features()``.

How it avoids look-ahead bias:
- Features at bar t use only data up to and including bar t (rolling calculations).
- The label for bar t is the return from t to t + horizon. On bar i the model is
  trained only on rows t <= i - horizon, whose labels were already known at bar i.
- The model is retrained every ``retrain_every`` bars on the latest ``train_window``
  bars, so a backtest never trains on data from after the decision it is making.

The same code runs in live paper bots: each run fetches recent history, trains on
it, and predicts the newest bar.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators
from .base import Param, Strategy

FEATURE_LOOKBACK = 60  # bars the default features need before they are defined


def default_features(df: pd.DataFrame) -> pd.DataFrame:
    """Causal features from one symbol's OHLCV bars."""
    close, volume = df["close"], df["volume"]
    r1 = close.pct_change()
    out = pd.DataFrame(index=df.index)
    for n in (1, 5, 10, 20):
        out[f"ret_{n}"] = close.pct_change(n)
    out["vol_20"] = r1.rolling(20).std()
    out["rsi_14"] = indicators.rsi(close, 14) / 100 - 0.5
    out["dist_sma_50"] = close / indicators.sma(close, 50) - 1
    out["range_14"] = ((df["high"] - df["low"]) / close).rolling(14).mean()
    avg_vol = volume.rolling(20).mean()
    out["volume_ratio"] = (volume / avg_vol.replace(0, np.nan) - 1).clip(-5, 5)
    return out


class RidgeModel:
    """Small, dependency-free ridge regression with feature standardisation."""

    def __init__(self, alpha: float = 10.0):
        self.alpha = alpha

    def fit(self, X: np.ndarray, y: np.ndarray) -> "RidgeModel":
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        Z = (X - self.mu) / self.sd
        self.b0 = y.mean()
        A = Z.T @ Z + self.alpha * np.eye(Z.shape[1])
        self.w = np.linalg.solve(A, Z.T @ (y - self.b0))
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mu) / self.sd) @ self.w + self.b0


class ModelStrategy(Strategy):
    """Base class: forecast each symbol's forward return, hold the best forecasts."""

    params = {
        "horizon": Param(5, min=1, max=60, help="Predict the return this many bars ahead"),
        "train_window": Param(500, min=50, max=5000, help="Bars of history used for each training run"),
        "min_train_rows": Param(200, min=20, max=100000, help="Don't trade until at least this many training rows exist"),
        "retrain_every": Param(21, min=1, max=1000, help="Retrain the model every N bars"),
        "rebalance_every": Param(5, min=1, max=500, help="Re-pick holdings every N bars"),
        "top_k": Param(0, min=0, max=100, help="Hold at most this many symbols (0 = every symbol above the threshold)"),
        "threshold": Param(0.0, step=0.001, help="Only hold symbols whose predicted return is above this (0.002 = 0.2%)"),
    }

    # ---- override these ------------------------------------------------ #
    def make_model(self):
        """Return a fresh model with fit(X, y) and predict(X)."""
        raise NotImplementedError

    def features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Feature columns for one symbol; each row may use only that bar and earlier ones."""
        return default_features(df)

    def feature_lookback(self) -> int:
        """Bars the features need before they are defined."""
        return FEATURE_LOOKBACK

    def fit(self, model, X: np.ndarray, y: np.ndarray) -> None:
        model.fit(X, y)

    def predict(self, model, X: np.ndarray) -> np.ndarray:
        return np.asarray(model.predict(X), dtype=float).ravel()

    # ---- machinery ------------------------------------------------------ #
    def warmup(self) -> int:
        return self.p["train_window"] + self.p["horizon"] + self.feature_lookback()

    def precompute(self, frames: dict[str, pd.DataFrame]) -> None:
        h = self.p["horizon"]
        self._symbols = list(frames)
        self._X, self._y = {}, {}
        for sym, f in frames.items():
            feats = self.features(f).replace([np.inf, -np.inf], np.nan)
            self._X[sym] = feats.to_numpy(dtype=float)
            # Forward return - only ever read for rows whose horizon has passed.
            self._y[sym] = (f["close"].shift(-h) / f["close"] - 1).to_numpy(dtype=float)
        self.feature_names = list(feats.columns)
        self.model = None
        self._last_fit = None
        self.last_training_rows = 0

    def _training_set(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        last_row = i - self.p["horizon"]  # newest row whose label is known at bar i
        if last_row < 0:
            return np.empty((0, len(self.feature_names))), np.empty(0)
        first_row = max(0, last_row - self.p["train_window"] + 1)
        Xs, ys = [], []
        for sym in self._symbols:
            X = self._X[sym][first_row:last_row + 1]
            y = self._y[sym][first_row:last_row + 1]
            ok = np.isfinite(X).all(axis=1) & np.isfinite(y)
            Xs.append(X[ok])
            ys.append(y[ok])
        return np.vstack(Xs), np.concatenate(ys)

    def on_bar(self, ctx):
        i = ctx.bar_index
        last = ctx.state.get("last_rebalance")
        if last is not None and ctx.bars_since(last) < self.p["rebalance_every"]:
            return None
        if self.model is None or i - self._last_fit >= self.p["retrain_every"]:
            X, y = self._training_set(i)
            if len(y) < self.p["min_train_rows"]:
                return None  # not enough history yet - stay as we are
            self.model = self.make_model()
            self.fit(self.model, X, y)
            self._last_fit = i
            self.last_training_rows = len(y)

        rows = {s: self._X[s][i] for s in ctx.symbols}
        live = [s for s, x in rows.items() if np.isfinite(x).all()]
        if not live:
            return None
        preds = dict(zip(live, self.predict(self.model, np.vstack([rows[s] for s in live]))))
        ranked = sorted((s for s in live if preds[s] > self.p["threshold"]), key=lambda s: preds[s], reverse=True)
        if self.p["top_k"]:
            ranked = ranked[: self.p["top_k"]]
        ctx.state["last_rebalance"] = str(ctx.time)
        ctx.state["last_predictions"] = {s: round(float(v), 6) for s, v in preds.items()}
        w = 1.0 / len(ranked) if ranked else 0.0
        return {s: (w if s in ranked else 0.0) for s in ctx.symbols}


class RidgeForecast(ModelStrategy):
    key = "ml_ridge"
    name = "ML: ridge forecast (walk-forward)"
    description = ("Learns to predict each symbol's return over the next few bars from momentum, "
                   "volatility, RSI and volume features, retraining as it goes. Holds the symbols "
                   "with the best positive forecasts. A template for your own models.")
    params = {**ModelStrategy.params,
              "alpha": Param(10.0, min=0.0, max=10000.0, help="Ridge regularisation strength")}

    def make_model(self):
        return RidgeModel(self.p["alpha"])
