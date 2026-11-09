"""Chart data: ranges, caching, indicators, baskets and summary statistics."""

from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import analytics, indicators
from .broker import INTRADAY, NY, TF_MINUTES, Broker

# range -> (default bar size, calendar days to show, sessions to keep for intraday ranges)
RANGES = {
    "1D": ("5Min", 6, 1), "5D": ("15Min", 10, 5), "1M": ("1Hour", 31, None),
    "3M": ("1Day", 92, None), "6M": ("1Day", 183, None), "YTD": ("1Day", None, None),
    "1Y": ("1Day", 366, None), "5Y": ("1Week", 366 * 5, None),
}
MAX_BARS = 20_000


def range_window(rng: str, now: datetime) -> tuple[datetime, int | None]:
    if rng not in RANGES:
        raise ValueError(f"Unknown range '{rng}'")
    _, days, sessions = RANGES[rng]
    if rng == "YTD":
        ny = now.astimezone(NY)
        return datetime(ny.year, 1, 1, tzinfo=NY).astimezone(timezone.utc), None
    return now - timedelta(days=days), sessions


def warmup_days(tf: str, bars: int) -> int:
    if bars <= 0:
        return 0
    if tf == "1Week":
        return bars * 7 + 14
    if tf == "1Month":
        return bars * 31 + 31
    if tf == "1Day":
        return int(bars * 1.5) + 10
    per_day = 390 / TF_MINUTES[tf]
    return int(math.ceil(bars / per_day * 1.5)) + 4


def estimate_bars(tf: str, days: float) -> float:
    return days * 5 / 7 * 390 / TF_MINUTES[tf]


def regular_session(frame: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Drop pre/post-market bars from intraday data."""
    if tf not in INTRADAY or frame.empty:
        return frame
    ny = frame.index.tz_convert(NY)
    mins = ny.hour * 60 + ny.minute
    first = 570 - TF_MINUTES[tf] + 1  # a bar that overlaps the 9:30 open
    return frame[(mins >= first) & (mins < 960)]


def last_sessions(index: pd.DatetimeIndex, n: int) -> pd.Timestamp | None:
    if len(index) == 0:
        return None
    dates = sorted(set(index.tz_convert(NY).date))
    keep = dates[-n:][0]
    return pd.Timestamp(keep).tz_localize(NY).tz_convert("UTC")


class MarketData:
    def __init__(self):
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _cached(self, key: tuple, ttl: float, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()
        with self._lock:
            if len(self._cache) > 500:
                self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            self._cache[key] = (now + ttl, value)
        return value

    # ------------------------------------------------------------------ #
    def bars(self, account_id: int, broker: Broker, symbols: list[str], tf: str,
             start: datetime, end: datetime | None = None) -> dict[str, pd.DataFrame]:
        symbols = sorted(set(symbols))
        ttl = 30 if tf in INTRADAY else 300
        bucket = start.replace(second=0, microsecond=0)
        if tf not in INTRADAY:
            bucket = bucket.replace(minute=0)
        key = ("bars", account_id, tuple(symbols), tf, bucket.isoformat(),
               end.isoformat() if end else None)
        frames = self._cached(key, ttl, lambda: broker.get_bars(symbols, tf, start, end))
        return {s: regular_session(f, tf) for s, f in frames.items()}

    def snapshots(self, account_id: int, broker: Broker, symbols: list[str]) -> dict:
        symbols = sorted(set(symbols))
        return self._cached(("snap", account_id, tuple(symbols)), 10,
                            lambda: broker.get_snapshots(symbols))

    def news(self, account_id: int, broker: Broker, symbols: list[str], limit: int) -> list:
        symbols = sorted(set(symbols))
        return self._cached(("news", account_id, tuple(symbols), limit), 120,
                            lambda: broker.get_news(symbols, limit))

    # ------------------------------------------------------------------ #
    def series(self, account_id: int, broker: Broker, items: list[dict], rng: str,
               interval: str | None, specs: list[str]) -> dict:
        """Build chart series for symbols and baskets.

        items: [{"kind": "symbol", "symbol": "AAPL"} |
                {"kind": "basket", "label": "Tech", "items": [{"symbol", "qty"}]}]
        """
        tf = interval or RANGES[rng][0]
        if tf not in TF_MINUTES:
            raise ValueError(f"Unknown bar size '{tf}'")
        for s in specs:
            indicators.parse_spec(s)
        now = datetime.now(timezone.utc)
        display_start, sessions = range_window(rng, now)
        days = (now - display_start).days + 1
        if estimate_bars(tf, days) > MAX_BARS:
            raise ValueError(f"{rng} of {tf} bars is too much data - choose a larger bar size")
        warm = indicators.warmup_bars(specs)
        fetch_start = display_start - timedelta(days=warmup_days(tf, warm))

        symbols = set()
        for it in items:
            if it["kind"] == "symbol":
                symbols.add(it["symbol"])
            else:
                symbols.update(b["symbol"] for b in it["items"])
        frames = self.bars(account_id, broker, sorted(symbols), tf, fetch_start)

        out = []
        for it in items:
            if it["kind"] == "symbol":
                frame = frames.get(it["symbol"])
                label = it["symbol"]
                if frame is None or frame.empty:
                    out.append({"label": label, "kind": "symbol", "error": "No data for this period"})
                    continue
            else:
                label = it.get("label") or "Basket"
                closes = pd.DataFrame({b["symbol"]: frames[b["symbol"]]["close"]
                                       for b in it["items"] if not frames[b["symbol"]].empty})
                missing = [b["symbol"] for b in it["items"] if frames[b["symbol"]].empty]
                if missing or closes.empty:
                    out.append({"label": label, "kind": "basket",
                                "error": f"No data for {', '.join(missing) or 'basket'}"})
                    continue
                closes = closes.ffill().dropna()
                value = sum(closes[b["symbol"]] * float(b["qty"]) for b in it["items"])
                frame = pd.DataFrame({"open": value, "high": value, "low": value, "close": value,
                                      "volume": np.nan})
            out.append(self._one(label, it, frame, tf, specs, display_start, sessions))
        return {"range": rng, "timeframe": tf, "series": out}

    @staticmethod
    def _one(label, item, frame: pd.DataFrame, tf, specs, display_start, sessions) -> dict:
        lines = {}
        for spec in specs:
            for name, s in indicators.compute(spec, frame["close"]).items():
                lines[name] = s
        cut = last_sessions(frame.index, sessions) if sessions else pd.Timestamp(display_start)
        view = frame[frame.index >= cut] if cut is not None else frame
        if view.empty:
            view = frame.tail(1)
        close = view["close"]
        is_basket = item["kind"] == "basket"
        result = {
            "label": label, "kind": item["kind"], "symbol": item.get("symbol"),
            "line": analytics.to_points(close, 4),
            "candles": None if is_basket else [
                {"t": int(ts.timestamp()), "o": round(r.open, 4), "h": round(r.high, 4),
                 "l": round(r.low, 4), "c": round(r.close, 4), "v": float(r.volume)}
                for ts, r in view.iterrows()],
            "indicators": {name: analytics.to_points(s[s.index >= view.index[0]], 4)
                           for name, s in lines.items()},
        }
        rets = np.log(close).diff().dropna()
        ppy = analytics.PERIODS_PER_YEAR.get(tf, 252)
        first, last = float(close.iloc[0]), float(close.iloc[-1])
        result["stats"] = {
            "first": first, "last": last, "change": last - first,
            "change_pct": last / first - 1 if first else None,
            "high": float(view["high"].max()), "low": float(view["low"].min()),
            "avg_volume": None if is_basket else float(view["volume"].mean()),
            "volatility": float(rets.std(ddof=0) * math.sqrt(ppy)) if len(rets) > 2 else None,
            "max_drawdown": float(analytics.drawdown(close).min()),
            "bars": int(len(view)),
        }
        return result
