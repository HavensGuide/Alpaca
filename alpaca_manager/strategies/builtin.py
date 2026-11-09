"""Strategies that ship with the app. Use them as-is or as templates for your own."""

from __future__ import annotations

from .. import indicators
from .base import Param, Strategy


def _equal(symbols) -> float:
    return 1.0 / len(symbols) if symbols else 0.0


class BuyAndHold(Strategy):
    key = "buy_and_hold"
    name = "Buy and hold"
    description = "Split the money equally across the symbols on the first bar and never trade again. A useful baseline."

    def on_bar(self, ctx):
        if ctx.state.get("bought"):
            return None
        ctx.state["bought"] = True
        return {s: _equal(ctx.symbols) for s in ctx.symbols}


class SmaCrossover(Strategy):
    key = "sma_crossover"
    name = "Moving-average crossover"
    description = ("Hold each symbol while its fast moving average is above its slow one; "
                   "go to cash when it crosses below. Trend following.")
    params = {
        "fast": Param(20, min=2, max=400, help="Fast moving-average window (bars)"),
        "slow": Param(50, min=3, max=500, help="Slow moving-average window (bars)"),
        "kind": Param("sma", choices=["sma", "ema"], help="Simple or exponential average"),
    }

    def warmup(self):
        return self.p["slow"] * (3 if self.p["kind"] == "ema" else 1)

    def precompute(self, frames):
        if self.p["fast"] >= self.p["slow"]:
            raise ValueError("The fast window must be shorter than the slow window")
        avg = indicators.ema if self.p["kind"] == "ema" else indicators.sma
        self.signal = {s: avg(f["close"], self.p["fast"]) > avg(f["close"], self.p["slow"])
                       for s, f in frames.items()}

    def on_bar(self, ctx):
        w = _equal(ctx.symbols)
        return {s: w if self.signal[s].iat[ctx.bar_index] else 0.0 for s in ctx.symbols}


class RsiMeanReversion(Strategy):
    key = "rsi_mean_reversion"
    name = "RSI mean reversion"
    description = ("Buy a symbol when RSI says it is oversold and sell once it recovers. "
                   "Tends to do well in choppy, range-bound markets.")
    params = {
        "window": Param(14, min=2, max=100, help="RSI window (bars)"),
        "buy_below": Param(30.0, min=1, max=99, help="Buy when RSI drops below this"),
        "sell_above": Param(55.0, min=1, max=99, help="Sell when RSI rises above this"),
    }

    def warmup(self):
        return self.p["window"] * 4

    def precompute(self, frames):
        self.rsi = {s: indicators.rsi(f["close"], self.p["window"]) for s, f in frames.items()}

    def on_bar(self, ctx):
        held = ctx.state.setdefault("held", {})
        w = _equal(ctx.symbols)
        out = {}
        for s in ctx.symbols:
            r = self.rsi[s].iat[ctx.bar_index]
            if r != r:  # NaN - not enough data yet
                out[s] = w if held.get(s) else 0.0
                continue
            if not held.get(s) and r < self.p["buy_below"]:
                held[s] = True
            elif held.get(s) and r > self.p["sell_above"]:
                held[s] = False
            out[s] = w if held.get(s) else 0.0
        return out


class MomentumRotation(Strategy):
    key = "momentum_rotation"
    name = "Momentum rotation"
    description = ("Every N bars, hold the top-K symbols with the best return over the lookback "
                   "period, equally weighted. Optionally go to cash when nothing is rising.")
    params = {
        "lookback": Param(63, min=2, max=500, help="Bars used to measure momentum (63 ≈ 3 months of days)"),
        "top_k": Param(2, min=1, max=50, help="How many symbols to hold"),
        "rebalance_every": Param(21, min=1, max=500, help="Re-rank every N bars (21 ≈ monthly)"),
        "require_positive": Param(True, help="Only hold symbols whose momentum is positive"),
    }

    def warmup(self):
        return self.p["lookback"] + 1

    def on_bar(self, ctx):
        last = ctx.state.get("last_rebalance")
        if last is not None and ctx.bars_since(last) < self.p["rebalance_every"]:
            return None
        ctx.state["last_rebalance"] = str(ctx.time)
        closes = ctx.closes(self.p["lookback"] + 1)
        mom = (closes.iloc[-1] / closes.iloc[0] - 1).dropna().sort_values(ascending=False)
        if self.p["require_positive"]:
            mom = mom[mom > 0]
        picks = list(mom.index[: self.p["top_k"]])
        return {s: (1.0 / self.p["top_k"] if s in picks else 0.0) for s in ctx.symbols}


class BollingerReversion(Strategy):
    key = "bollinger_reversion"
    name = "Bollinger band reversion"
    description = "Buy when price closes below the lower Bollinger band; sell when it gets back to the middle band."
    params = {
        "window": Param(20, min=2, max=200, help="Band window (bars)"),
        "k": Param(2.0, min=0.5, max=4, step=0.1, help="Band width in standard deviations"),
    }

    def warmup(self):
        return self.p["window"] + 1

    def precompute(self, frames):
        self.bands = {s: indicators.bollinger(f["close"], self.p["window"], self.p["k"])
                      for s, f in frames.items()}

    def on_bar(self, ctx):
        held = ctx.state.setdefault("held", {})
        w = _equal(ctx.symbols)
        out = {}
        for s in ctx.symbols:
            mid, _, lower = self.bands[s]
            i = ctx.bar_index
            px = ctx.close(s)
            if not held.get(s) and px < lower.iat[i]:
                held[s] = True
            elif held.get(s) and px >= mid.iat[i]:
                held[s] = False
            out[s] = w if held.get(s) else 0.0
        return out


from .ml import RidgeForecast  # noqa: E402  (imported here to avoid a cycle)

BUILTIN = [BuyAndHold, SmaCrossover, RsiMeanReversion, MomentumRotation, BollingerReversion,
           RidgeForecast]
