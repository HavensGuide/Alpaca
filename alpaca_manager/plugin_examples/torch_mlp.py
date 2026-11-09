"""Example plugin: a PyTorch neural network with walk-forward training.

Uses your GPU automatically if PyTorch can see one (torch.cuda.is_available()).
The app must run in the environment where torch is installed, e.g.
    conda activate torch_gpu_env
    alpaca-manager

Copy this file into your plugins folder (shown on Strategies -> Plugins), then press
"Reload plugins". It appears as "ML: PyTorch MLP".

Two ways to use it:
1. Train inside the backtest (default): the network is retrained every
   `retrain_every` bars on past data only - an honest walk-forward test.
2. Load weights you trained elsewhere: set `weights_path` to a state_dict file saved
   with torch.save(model.state_dict(), path). Training is then skipped. Only backtest
   on dates AFTER the data you trained on, or the result will be unrealistically good.
"""

import numpy as np

from alpaca_manager.strategies import ModelStrategy, Param


class TorchRegressor:
    """fit/predict wrapper around a small PyTorch MLP."""

    def __init__(self, hidden=32, epochs=60, lr=1e-3, weight_decay=1e-4, dropout=0.1,
                 weights_path="", seed=0):
        import torch  # imported lazily so the app starts even without torch

        self.torch = torch
        torch.manual_seed(seed)
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.hidden, self.epochs, self.lr = hidden, epochs, lr
        self.weight_decay, self.dropout = weight_decay, dropout
        self.weights_path = weights_path
        self.net = None

    def _build(self, n_features):
        nn = self.torch.nn
        return nn.Sequential(
            nn.Linear(n_features, self.hidden), nn.ReLU(), nn.Dropout(self.dropout),
            nn.Linear(self.hidden, self.hidden), nn.ReLU(), nn.Dropout(self.dropout),
            nn.Linear(self.hidden, 1),
        ).to(self.device)

    def fit(self, X, y):
        torch = self.torch
        # Standardise with statistics from the training window only.
        self.mu, self.sd = X.mean(axis=0), X.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        self.y_scale = float(np.std(y)) or 1.0
        self.net = self._build(X.shape[1])
        if self.weights_path:
            self.net.load_state_dict(torch.load(self.weights_path, map_location=self.device))
            self.net.eval()
            return self
        Xt = torch.tensor((X - self.mu) / self.sd, dtype=torch.float32, device=self.device)
        yt = torch.tensor(y / self.y_scale, dtype=torch.float32, device=self.device).unsqueeze(1)
        opt = torch.optim.AdamW(self.net.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        loss_fn = torch.nn.HuberLoss()
        self.net.train()
        batch = 256
        for _ in range(self.epochs):
            perm = torch.randperm(len(Xt), device=self.device)
            for k in range(0, len(Xt), batch):
                idx = perm[k:k + batch]
                opt.zero_grad()
                loss = loss_fn(self.net(Xt[idx]), yt[idx])
                loss.backward()
                opt.step()
        self.net.eval()
        return self

    def predict(self, X):
        torch = self.torch
        with torch.no_grad():
            Xt = torch.tensor((X - self.mu) / self.sd, dtype=torch.float32, device=self.device)
            return self.net(Xt).squeeze(1).cpu().numpy() * self.y_scale


class TorchMLP(ModelStrategy):
    key = "ml_torch_mlp"
    name = "ML: PyTorch MLP"
    description = ("A small neural network predicting each symbol's return over the next few bars. "
                   "Trains walk-forward on your GPU if available, or loads weights you trained.")
    params = {
        **ModelStrategy.params,
        "hidden": Param(32, min=4, max=1024, help="Neurons per hidden layer"),
        "epochs": Param(60, min=1, max=2000, help="Training epochs per retrain"),
        "lr": Param(0.001, min=0.00001, max=1.0, step=0.0001, help="Learning rate"),
        "dropout": Param(0.1, min=0.0, max=0.9, step=0.05, help="Dropout rate"),
        "weights_path": Param("", help="Optional: path to a saved state_dict (skips training)"),
    }

    def make_model(self):
        return TorchRegressor(hidden=self.p["hidden"], epochs=self.p["epochs"], lr=self.p["lr"],
                              dropout=self.p["dropout"], weights_path=self.p["weights_path"])
