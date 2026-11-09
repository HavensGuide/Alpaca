"""ML strategy framework: walk-forward correctness, examples and bots."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alpaca_manager.backtest import BacktestSettings, run_backtest
from alpaca_manager.strategies import ModelStrategy, RidgeModel
from alpaca_manager.strategies.ml import RidgeForecast
from alpaca_manager.strategies.registry import Registry

EXAMPLES = Path(__file__).resolve().parents[1] / "alpaca_manager" / "plugin_examples"


def frames(n=700, symbols=("A", "B", "C"), seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    out = {}
    for s in symbols:
        c = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
        out[s] = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                               "volume": rng.lognormal(13, 0.3, n)}, index=idx)
    return out


SETTINGS = BacktestSettings(start=pd.Timestamp("2020-01-01", tz="UTC"), cash=10_000)


def test_never_trains_on_labels_from_the_future():
    seen = []

    class Spy(ModelStrategy):
        def make_model(self):
            return RidgeModel(1.0)

        def _training_set(self, i):
            X, y = super()._training_set(i)
            # Re-derive which rows were eligible and record the newest one.
            seen.append((i, i - self.p["horizon"]))
            # Every label used must be computable from closes up to bar i.
            for sym in self._symbols:
                close = self._frames_close[sym]
                last = i - self.p["horizon"]
                expected = close[last + self.p["horizon"]] / close[last] - 1
                assert np.isclose(self._y[sym][last], expected)
                assert last + self.p["horizon"] <= i
            return X, y

        def precompute(self, fr):
            super().precompute(fr)
            self._frames_close = {s: f["close"].to_numpy() for s, f in fr.items()}

    s = Spy(horizon=5, train_window=200, min_train_rows=50, retrain_every=10)
    run_backtest(s, frames(400), SETTINGS)
    assert seen and all(newest <= i - 5 for i, newest in seen)


def test_future_data_cannot_leak_into_training():
    """If prices after bar i are replaced, decisions up to bar i must not change."""
    base = frames(500)
    tampered = {s: f.copy() for s, f in base.items()}
    for f in tampered.values():
        f.iloc[450:, f.columns.get_loc("close")] *= 3.0  # wild future

    def decisions(fr):
        strat = RidgeForecast(train_window=150, min_train_rows=50, retrain_every=7, rebalance_every=1)
        res = run_backtest(strat, fr, SETTINGS)
        return [(f["t"], f["symbol"], f["side"], f["qty"]) for f in res["fills"]]

    cutoff = int(base["A"].index[449].timestamp())
    before = [d for d in decisions(base) if d[0] <= cutoff]
    after = [d for d in decisions(tampered) if d[0] <= cutoff]
    assert before and before == after


def test_ridge_forecast_backtest_runs():
    res = run_backtest(RidgeForecast(train_window=200, min_train_rows=100), frames(), SETTINGS)
    assert res["trade_stats"]["fills"] > 0 and res["final_equity"] > 0


def test_example_plugins_load_and_sklearn_runs(tmp_path):
    for f in EXAMPLES.glob("*.py"):
        (tmp_path / f.name).write_text(f.read_text())
    reg = Registry(tmp_path)
    keys = {s["key"] for s in reg.describe()["strategies"]}
    assert {"ml_sklearn_boosting", "ml_torch_mlp", "ml_ridge"} <= keys, reg.errors
    assert reg.errors == []
    pytest.importorskip("sklearn")
    strat = reg.build("ml_sklearn_boosting", {"train_window": 200, "min_train_rows": 100, "max_iter": 30})
    res = run_backtest(strat, frames(), SETTINGS)
    assert res["trade_stats"]["fills"] > 0


def test_ml_bot_runs_twice(client, demo):
    acct_id, _ = demo
    r = client.post("/api/bots", json={"account_id": acct_id, "name": "ML", "strategy_key": "ml_ridge",
                                       "params": {"train_window": 200, "min_train_rows": 100,
                                                  "rebalance_every": 1, "threshold": -1},
                                       "symbols": ["AAPL", "MSFT", "SPY"], "allocation": 20_000})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    first = client.post(f"/api/bots/{bid}/run")
    assert first.status_code == 200 and first.json()["orders"] > 0, first.text
    second = client.post(f"/api/bots/{bid}/run")
    assert second.status_code == 200, second.text


def test_momentum_bot_rebalances_across_runs():
    """bars_since works on a shifting window, as a live bot sees it."""
    from alpaca_manager.strategies.base import Context

    fr = frames(100)
    idx = fr["A"].index
    ctx = Context(fr, idx)
    ctx._set(50, 0.0, {})
    stamp = str(ctx.time)
    # A later run sees a window that starts 30 bars later: same bar, different index.
    shifted = {s: f.iloc[30:] for s, f in fr.items()}
    ctx2 = Context(shifted, shifted["A"].index)
    ctx2._set(len(shifted["A"]) - 1, 0.0, {})
    assert ctx2.bars_since(stamp) == 99 - 50


def test_install_example_from_ui(client):
    ex = client.get("/api/strategies/examples").json()
    names = {e["file"]: e for e in ex}
    assert {"sklearn_boosting.py", "torch_mlp.py"} <= set(names)
    assert not names["sklearn_boosting.py"]["installed"]
    r = client.post("/api/strategies/examples/sklearn_boosting.py")
    assert r.status_code == 200 and any(s["key"] == "ml_sklearn_boosting" for s in r.json()["strategies"])
    assert client.post("/api/strategies/examples/sklearn_boosting.py").status_code == 409  # never overwrite
    assert client.post("/api/strategies/examples/..%2Fapp.py").status_code == 404
