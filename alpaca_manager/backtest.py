"""Bar-by-bar backtester for strategy plugins.

Rules (kept deliberately simple and conservative):
- A strategy decides on a bar's close; orders fill at the NEXT bar's open, plus slippage.
- Long-only. Target weights below 0 are treated as 0; if they sum above 1 they are scaled down.
- Whole shares by default (like the live bots); commission is charged in basis points of notional.
- Small rebalances below ``min_trade_pct`` of equity are skipped to avoid churn
  (exits to zero and entries from zero always go through).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from . import analytics
from .strategies.base import Context, Strategy


@dataclass
class BacktestSettings:
    start: pd.Timestamp
    end: pd.Timestamp | None = None
    timeframe: str = "1Day"
    cash: float = 10_000.0
    commission_bps: float = 0.0
    slippage_bps: float = 5.0
    min_trade_pct: float = 0.01
    fractional: bool = False


def align(frames: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DatetimeIndex]:
    """Put every symbol on one shared time index (forward-filling gaps)."""
    frames = {s: f for s, f in frames.items() if f is not None and not f.empty}
    if not frames:
        raise ValueError("No price data for any of the symbols in this period")
    index = frames[next(iter(frames))].index
    for f in frames.values():
        index = index.union(f.index)
    out = {}
    for s, f in frames.items():
        g = f.reindex(index)
        g["close"] = g["close"].ffill()
        for col in ("open", "high", "low"):
            g[col] = g[col].fillna(g["close"])
        g["volume"] = g["volume"].fillna(0)
        out[s] = g
    valid = pd.concat([g["close"] for g in out.values()], axis=1).notna().all(axis=1)
    if not valid.any():
        raise ValueError("The symbols have no overlapping price history")
    first = valid.idxmax()
    out = {s: g.loc[first:] for s, g in out.items()}
    return out, out[next(iter(out))].index


def rebalance(targets: dict[str, float], prices: dict[str, float], cash: float,
              positions: dict[str, float], *, fractional: bool, min_trade_pct: float,
              slippage_bps: float = 0.0, commission_bps: float = 0.0) -> tuple[list[dict], float]:
    """Turn target weights into fills. Returns (fills, new_cash); mutates ``positions``.

    Shared by the backtester and the live bots (which pass zero slippage/commission
    and then replace fill prices with the broker's actual ones).
    """
    clean = {s: max(0.0, float(w or 0)) for s, w in targets.items() if s in prices}
    total = sum(clean.values())
    if total > 1.0:
        clean = {s: w / total for s, w in clean.items()}
    equity = cash + sum(q * prices[s] for s, q in positions.items() if q)
    if equity <= 0:
        return [], cash
    slip = slippage_bps / 1e4
    fee_rate = commission_bps / 1e4
    plan = []
    for sym, w in clean.items():
        px = prices[sym]
        if not px or px <= 0 or math.isnan(px):
            continue
        current = positions.get(sym, 0.0)
        desired = w * equity / px
        if not fractional:
            desired = math.floor(desired)
        delta = desired - current
        if abs(delta) < 1e-9:
            continue
        opening_or_closing = current == 0 or desired == 0
        if not opening_or_closing and abs(delta) * px < min_trade_pct * equity:
            continue
        plan.append((sym, delta, px))

    fills = []
    for sym, delta, px in sorted(plan, key=lambda x: x[1]):  # sells first
        if delta < 0:
            qty = -delta
            fill_px = px * (1 - slip)
            fee = qty * fill_px * fee_rate
            cash += qty * fill_px - fee
            positions[sym] = positions.get(sym, 0.0) - qty
            fills.append({"symbol": sym, "side": "sell", "qty": qty, "price": fill_px, "fee": fee})
        else:
            fill_px = px * (1 + slip)
            affordable = cash / (fill_px * (1 + fee_rate))
            qty = min(delta, affordable if fractional else math.floor(affordable))
            if qty <= 1e-9:
                continue
            fee = qty * fill_px * fee_rate
            cash -= qty * fill_px + fee
            positions[sym] = positions.get(sym, 0.0) + qty
            fills.append({"symbol": sym, "side": "buy", "qty": qty, "price": fill_px, "fee": fee})
    for sym in [s for s, q in positions.items() if abs(q) < 1e-9]:
        positions.pop(sym)
    return fills, cash


def run_backtest(strategy: Strategy, frames: dict[str, pd.DataFrame], settings: BacktestSettings,
                 benchmark: pd.DataFrame | None = None) -> dict:
    frames, index = align(frames)
    warm = max(1, int(strategy.warmup()))
    start_i = max(warm - 1, int(index.searchsorted(settings.start)))
    if settings.end is not None:
        index_end = int(index.searchsorted(settings.end, side="right"))
    else:
        index_end = len(index)
    if start_i >= index_end - 1:
        raise ValueError(
            f"Not enough bars: the strategy needs {warm} bars of warm-up and the period has "
            f"{index_end} in total. Choose an earlier start date or a shorter-window setting.")

    try:
        strategy.precompute(frames)
    except Exception as exc:
        raise ValueError(f"Strategy setup failed: {type(exc).__name__}: {exc}") from exc
    ctx = Context(frames, index)
    cash, positions = float(settings.cash), {}
    equity_curve, fills, exposure_bars = [], [], 0
    pending: dict[str, float] | None = None
    strategy.on_start(ctx)

    opens = {s: f["open"].to_numpy() for s, f in frames.items()}
    for i in range(start_i, index_end):
        if pending is not None:
            prices = {s: float(opens[s][i]) for s in frames}
            new_fills, cash = rebalance(
                pending, prices, cash, positions, fractional=settings.fractional,
                min_trade_pct=settings.min_trade_pct, slippage_bps=settings.slippage_bps,
                commission_bps=settings.commission_bps)
            ts = index[i]
            fills.extend(dict(f, t=ts) for f in new_fills)
            pending = None
        ctx._set(i, cash, positions)
        equity_curve.append((index[i], ctx.equity))
        if positions:
            exposure_bars += 1
        try:
            targets = strategy.on_bar(ctx)
        except Exception as exc:
            raise ValueError(f"Strategy error on bar {index[i]:%Y-%m-%d %H:%M}: "
                             f"{type(exc).__name__}: {exc}") from exc
        if targets is not None and i < index_end - 1:
            pending = dict(targets)

    equity = pd.Series(dict(equity_curve)).astype(float)
    result = {
        "equity": analytics.to_points(equity, 2),
        "drawdown": analytics.to_points(analytics.drawdown(equity), 5),
        "metrics": analytics.equity_stats(equity, settings.timeframe),
        "final_equity": float(equity.iloc[-1]),
        "exposure": exposure_bars / len(equity) if len(equity) else 0.0,
        "bars": len(equity),
        "positions": {s: q for s, q in positions.items()},
        "cash": cash,
    }
    trades = analytics.round_trips(fills)
    result["trade_stats"] = analytics.trade_stats(trades)
    result["trade_stats"]["fills"] = len(fills)
    result["trade_stats"]["fees"] = sum(f["fee"] for f in fills)
    result["by_symbol"] = analytics.pnl_by_symbol(trades)
    result["fills"] = [
        {"t": int(f["t"].timestamp()), "symbol": f["symbol"], "side": f["side"],
         "qty": round(f["qty"], 6), "price": round(f["price"], 4), "fee": round(f["fee"], 4)}
        for f in fills]
    result["trades"] = [
        dict(t, entry_time=int(t["entry_time"].timestamp()), exit_time=int(t["exit_time"].timestamp()))
        for t in trades]

    if benchmark is not None and not benchmark.empty:
        b = benchmark["close"].reindex(equity.index, method="ffill").dropna()
        if len(b) > 1:
            b_eq = b / b.iloc[0] * settings.cash
            result["benchmark"] = analytics.to_points(b_eq, 2)
            result["benchmark_metrics"] = analytics.equity_stats(b_eq, settings.timeframe)
    return result
