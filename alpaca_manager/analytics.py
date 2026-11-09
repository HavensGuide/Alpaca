"""Performance analytics: round-trip trade matching and equity-curve statistics."""

from __future__ import annotations

import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd

# Bars per year for annualising, by timeframe.
PERIODS_PER_YEAR = {
    "1Min": 252 * 390, "5Min": 252 * 78, "15Min": 252 * 26, "30Min": 252 * 13,
    "1Hour": 252 * 7, "1Day": 252, "1Week": 52, "1Month": 12,
}


def round_trips(fills: list[dict], allow_short: bool = False) -> list[dict]:
    """Match fills first-in-first-out into closed round-trip trades.

    ``fills``: dicts with ``t`` (sortable), ``symbol``, ``side`` ("buy"/"sell"),
    ``qty`` (> 0), ``price`` and optional ``fee``. With ``allow_short=False`` a sell
    with no matching earlier buy (e.g. shares bought before the history window) is
    ignored rather than treated as opening a short.
    Returns one dict per closed lot-match.
    """
    lots: dict[str, deque] = defaultdict(deque)  # symbol -> [signed_qty, price, t, fee_per_share]
    trades = []
    for f in sorted(fills, key=lambda x: x["t"]):
        sym, price = f["symbol"], float(f["price"])
        qty = float(f["qty"])
        if qty <= 0:
            continue
        sign = 1 if f["side"] == "buy" else -1
        fee_ps = float(f.get("fee") or 0) / qty
        remaining = qty
        book = lots[sym]
        while remaining > 1e-9 and book and (book[0][0] > 0) != (sign > 0):
            lot = book[0]
            take = min(remaining, abs(lot[0]))
            direction = "long" if lot[0] > 0 else "short"
            entry, exit_ = lot[1], price
            pnl = (exit_ - entry) * take if direction == "long" else (entry - exit_) * take
            pnl -= (lot[3] + fee_ps) * take
            trades.append({
                "symbol": sym, "direction": direction, "qty": take,
                "entry_time": lot[2], "exit_time": f["t"], "entry_price": entry,
                "exit_price": exit_, "pnl": pnl,
                "return_pct": pnl / (entry * take) if entry else 0.0,
            })
            lot[0] -= take if lot[0] > 0 else -take
            remaining -= take
            if abs(lot[0]) <= 1e-9:
                book.popleft()
        if remaining > 1e-9 and (sign > 0 or allow_short):
            book.append([sign * remaining, price, f["t"], fee_ps])
    return trades


def trade_stats(trades: list[dict]) -> dict:
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(trades),
        "realized_pnl": sum(t["pnl"] for t in trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "best": max((t["pnl"] for t in trades), default=None),
        "worst": min((t["pnl"] for t in trades), default=None),
    }


def pnl_by_symbol(trades: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for t in trades:
        a = agg.setdefault(t["symbol"], {"symbol": t["symbol"], "pnl": 0.0, "trades": 0, "wins": 0})
        a["pnl"] += t["pnl"]
        a["trades"] += 1
        a["wins"] += t["pnl"] > 0
    return sorted(agg.values(), key=lambda a: a["pnl"], reverse=True)


def _clean(x):
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) or math.isinf(x) else x


def equity_stats(equity: pd.Series, timeframe: str = "1Day") -> dict:
    """Return/risk statistics for an equity curve indexed by timestamp."""
    equity = equity.dropna()
    equity = equity[equity > 0]
    if len(equity) < 2:
        return {"total_return": None, "cagr": None, "volatility": None, "sharpe": None,
                "sortino": None, "max_drawdown": None, "calmar": None,
                "best_period": None, "worst_period": None}
    rets = equity.pct_change().dropna()
    ppy = PERIODS_PER_YEAR.get(timeframe, 252)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    days = (equity.index[-1] - equity.index[0]).total_seconds() / 86400
    cagr = (1 + total) ** (365.25 / days) - 1 if days >= 30 and total > -1 else None
    vol = rets.std(ddof=0) * math.sqrt(ppy) if len(rets) > 1 else None
    sharpe = (rets.mean() / rets.std(ddof=0) * math.sqrt(ppy)) if len(rets) > 1 and rets.std(ddof=0) > 0 else None
    downside = rets[rets < 0]
    dd_std = math.sqrt((downside ** 2).sum() / len(rets)) if len(rets) else 0
    sortino = rets.mean() / dd_std * math.sqrt(ppy) if dd_std > 0 else None
    dd = drawdown(equity)
    max_dd = dd.min()
    calmar = cagr / abs(max_dd) if cagr is not None and max_dd < 0 else None
    return {
        "total_return": _clean(total), "cagr": _clean(cagr), "volatility": _clean(vol),
        "sharpe": _clean(sharpe), "sortino": _clean(sortino), "max_drawdown": _clean(max_dd),
        "calmar": _clean(calmar), "best_period": _clean(rets.max()),
        "worst_period": _clean(rets.min()),
    }


def drawdown(equity: pd.Series) -> pd.Series:
    peak = equity.cummax()
    return equity / peak - 1


def to_points(series: pd.Series, digits: int = 4) -> list[dict]:
    """Series -> [{"t": unix_seconds, "v": value}] skipping NaN, for the charts."""
    out = []
    for ts, v in series.items():
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        out.append({"t": int(pd.Timestamp(ts).timestamp()), "v": round(float(v), digits)})
    return out
