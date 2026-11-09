"""The strategy plugin API.

A strategy is a class that looks at market history one bar at a time and says
what portfolio it wants to hold. The same class runs in backtests and in live
paper-trading bots, so a model you backtest is exactly the model you deploy.

Minimal example::

    from alpaca_manager.strategies import Strategy, Param, indicators

    class GoldenCross(Strategy):
        name = "Golden cross"
        description = "Hold a stock while its 50-day SMA is above its 200-day SMA."
        params = {
            "fast": Param(50, min=2, max=400, help="Fast SMA window"),
            "slow": Param(200, min=3, max=500, help="Slow SMA window"),
        }

        def warmup(self):
            return self.p["slow"]

        def on_bar(self, ctx):
            weights = {}
            for sym in ctx.symbols:
                close = ctx.history(sym)["close"]
                fast = indicators.sma(close, self.p["fast"]).iloc[-1]
                slow = indicators.sma(close, self.p["slow"]).iloc[-1]
                weights[sym] = 1 / len(ctx.symbols) if fast > slow else 0.0
            return weights

``on_bar`` returns **target portfolio weights**: ``{symbol: fraction of equity}``
(0 = no position, 0.25 = a quarter of equity; long-only, total <= 1). Return
``None`` to keep the current portfolio unchanged. For speed, indicators can be
computed once for the whole history in ``precompute`` (see below). Decisions are made on a bar's
close and executed at the next bar's open in backtests (no look-ahead), or right
away with market orders when running live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class Param:
    """A tunable strategy parameter, shown as a form field in the UI."""

    default: Any
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: list | None = None
    help: str = ""
    type: str = field(default="")

    def __post_init__(self):
        if not self.type:
            if self.choices:
                self.type = "choice"
            elif isinstance(self.default, bool):
                self.type = "bool"
            elif isinstance(self.default, int):
                self.type = "int"
            elif isinstance(self.default, float):
                self.type = "float"
            else:
                self.type = "str"

    def coerce(self, name: str, value: Any) -> Any:
        if value is None or value == "":
            return self.default
        try:
            if self.type == "bool":
                v = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}
            elif self.type == "int":
                v = int(float(value))
            elif self.type == "float":
                v = float(value)
            else:
                v = str(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: '{value}' is not a valid {self.type}") from None
        if self.choices and v not in self.choices:
            raise ValueError(f"{name}: must be one of {self.choices}")
        if self.min is not None and isinstance(v, (int, float)) and v < self.min:
            raise ValueError(f"{name}: must be at least {self.min}")
        if self.max is not None and isinstance(v, (int, float)) and v > self.max:
            raise ValueError(f"{name}: must be at most {self.max}")
        return v

    def describe(self) -> dict:
        return {"default": self.default, "type": self.type, "min": self.min, "max": self.max,
                "step": self.step, "choices": self.choices, "help": self.help}


class Strategy:
    """Base class for strategy plugins. Subclass it and implement ``on_bar``."""

    #: Short human-readable name.
    name: str = "Unnamed strategy"
    #: One or two sentences shown in the UI.
    description: str = ""
    #: Tunable parameters: {"name": Param(default, ...)}
    params: dict[str, Param] = {}
    #: Unique id; defaults to the class name. Set it explicitly to keep saved bots
    #: working if you rename the class.
    key: str = ""

    def __init__(self, **params):
        self.p: dict[str, Any] = {}
        for name, spec in self.params.items():
            self.p[name] = spec.coerce(name, params.get(name))

    def warmup(self) -> int:
        """Bars of history needed before ``on_bar`` can make a decision."""
        return 1

    def precompute(self, frames: dict[str, pd.DataFrame]) -> None:
        """Optional speed-up: compute indicators for the whole history at once.

        ``frames`` are the aligned OHLCV DataFrames ``ctx`` will walk through, so
        ``series.iat[ctx.bar_index]`` is the value on the current bar. Only use
        rolling/causal calculations here (each value may depend on past bars only),
        otherwise the backtest will see the future.
        """

    def on_start(self, ctx: "Context") -> None:
        """Called once before the first bar."""

    def on_bar(self, ctx: "Context") -> dict[str, float] | None:
        raise NotImplementedError

    @classmethod
    def strategy_key(cls) -> str:
        return cls.key or cls.__name__

    @classmethod
    def describe(cls) -> dict:
        return {"key": cls.strategy_key(), "name": cls.name, "description": cls.description,
                "params": {k: v.describe() for k, v in cls.params.items()}}


class Context:
    """What a strategy can see on each bar.

    Attributes:
        symbols   the strategy's universe
        time      timestamp of the current bar (UTC)
        bar_index index of the current bar (0-based)
        cash      cash available to the strategy
        equity    cash + market value of positions
        state     a dict that persists between bars (and, for bots, between runs)
    """

    def __init__(self, frames: dict[str, pd.DataFrame], index: pd.DatetimeIndex,
                 state: dict | None = None):
        self._frames = frames
        self._index = index
        self.symbols: list[str] = list(frames)
        self.state: dict = state if state is not None else {}
        self.bar_index = 0
        self.time: pd.Timestamp | None = None
        self.cash = 0.0
        self.equity = 0.0
        self._positions: dict[str, float] = {}
        self._prices: dict[str, float] = {}

    def _set(self, i: int, cash: float, positions: dict[str, float]) -> None:
        self.bar_index = i
        self.time = self._index[i]
        self._prices = {s: float(f["close"].iat[i]) for s, f in self._frames.items()}
        self.cash = cash
        self._positions = positions
        self.equity = cash + sum(q * self._prices[s] for s, q in positions.items() if q)

    def history(self, symbol: str, bars: int | None = None) -> pd.DataFrame:
        """OHLCV DataFrame up to and including the current bar (oldest first)."""
        f = self._frames[symbol]
        end = self.bar_index + 1
        start = 0 if bars is None else max(0, end - bars)
        return f.iloc[start:end]

    def closes(self, bars: int | None = None) -> pd.DataFrame:
        """Close prices of every symbol as one DataFrame (columns = symbols)."""
        return pd.DataFrame({s: self.history(s, bars)["close"] for s in self.symbols})

    def bars_since(self, when) -> int:
        """Bars that have closed after ``when`` (a timestamp or ISO string), up to now.

        Use this rather than comparing ``bar_index`` values saved in ``state``: a live
        bot sees a different window of history on every run, so indexes shift.
        """
        ts = pd.Timestamp(when)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return int(self.bar_index - self._index.searchsorted(ts, side="right") + 1)

    def close(self, symbol: str) -> float:
        return self._prices[symbol]

    def position(self, symbol: str) -> float:
        """Shares currently held."""
        return self._positions.get(symbol, 0.0)

    def weight(self, symbol: str) -> float:
        """Current fraction of equity in ``symbol``."""
        if self.equity <= 0:
            return 0.0
        return self.position(symbol) * self._prices[symbol] / self.equity
