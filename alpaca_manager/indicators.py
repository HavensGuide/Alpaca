"""Technical indicators on pandas Series. Shared by charts, strategies and backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(window, min_periods=window).mean()


def ema(close: pd.Series, window: int) -> pd.Series:
    """Exponential moving average (span = window)."""
    return close.ewm(span=window, adjust=False, min_periods=window).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's relative strength index, 0-100."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def bollinger(close: pd.Series, window: int = 20, k: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(middle, upper, lower) Bollinger bands."""
    mid = sma(close, window)
    sd = close.rolling(window, min_periods=window).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def returns(close: pd.Series, periods: int = 1) -> pd.Series:
    return close.pct_change(periods)


def parse_spec(spec: str) -> tuple[str, int, float | None]:
    """'sma:20' -> ('sma', 20, None); 'bb:20:2' -> ('bb', 20, 2.0)."""
    parts = spec.strip().lower().split(":")
    kind = parts[0]
    if kind not in {"sma", "ema", "bb", "rsi"}:
        raise ValueError(f"Unknown indicator '{kind}'")
    window = int(parts[1]) if len(parts) > 1 else (14 if kind == "rsi" else 20)
    if not 2 <= window <= 500:
        raise ValueError("Indicator window must be between 2 and 500")
    k = float(parts[2]) if len(parts) > 2 else (2.0 if kind == "bb" else None)
    return kind, window, k


def compute(spec: str, close: pd.Series) -> dict[str, pd.Series]:
    """Named output lines for one indicator spec."""
    kind, window, k = parse_spec(spec)
    if kind == "sma":
        return {f"SMA {window}": sma(close, window)}
    if kind == "ema":
        return {f"EMA {window}": ema(close, window)}
    if kind == "rsi":
        return {f"RSI {window}": rsi(close, window)}
    mid, up, lo = bollinger(close, window, k)
    return {f"BB {window} mid": mid, f"BB {window} upper": up, f"BB {window} lower": lo}


def warmup_bars(specs: list[str]) -> int:
    """Bars of history needed before the first displayed point."""
    best = 0
    for s in specs:
        kind, window, _ = parse_spec(s)
        best = max(best, window * (3 if kind in {"ema", "rsi"} else 1))
    return best
