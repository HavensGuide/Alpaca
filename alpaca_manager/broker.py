"""Broker abstraction.

``AlpacaBroker`` talks to Alpaca through the official ``alpaca-py`` SDK.
``DemoBroker`` is an in-memory simulation (random-walk prices, instant market
fills) so the app can be explored without API keys or risk.

Both return plain dicts so the rest of the app never depends on SDK models.
"""

from __future__ import annotations

import random
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo


class BrokerError(Exception):
    """A broker call failed; the message is safe to show to the user."""


@dataclass
class Leg:
    """One order to place.

    amount_type:
      "shares"  - ``amount`` is a share quantity (fractions allowed for market/day)
      "dollars" - ``amount`` is a dollar value (market orders only, Alpaca "notional")
      "all"     - sell the whole current position (``amount`` ignored)
    """

    symbol: str
    side: str  # "buy" | "sell"
    amount_type: str = "shares"
    amount: float | None = None
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    time_in_force: str = "day"  # "day" | "gtc"

    @classmethod
    def from_dict(cls, d: dict) -> "Leg":
        return cls(
            symbol=str(d["symbol"]).upper().strip(),
            side=d["side"],
            amount_type=d.get("amount_type", "shares"),
            amount=d.get("amount"),
            order_type=d.get("order_type", "market"),
            limit_price=d.get("limit_price"),
            time_in_force=d.get("time_in_force", "day"),
        )

    def describe(self) -> str:
        if self.amount_type == "all":
            size = "entire position of"
        elif self.amount_type == "dollars":
            size = f"${self.amount:,.2f} of"
        else:
            size = f"{self.amount:g} sh"
        price = f" @ limit ${self.limit_price:,.2f}" if self.order_type == "limit" else " @ market"
        return f"{self.side.upper()} {size} {self.symbol}{price}"


class Broker(Protocol):
    def get_account(self) -> dict: ...
    def get_positions(self) -> list[dict]: ...
    def get_orders(self, status: str = "open", limit: int = 50) -> list[dict]: ...
    def cancel_order(self, order_id: str) -> None: ...
    def submit_order(self, leg: Leg, client_order_id: str | None = None) -> dict: ...
    def close_position(self, symbol: str) -> dict: ...
    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]: ...
    def get_clock(self) -> dict: ...


def _f(value) -> float | None:
    return None if value is None else float(value)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _enum(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", str(value))


# --------------------------------------------------------------------------- #
# Real Alpaca
# --------------------------------------------------------------------------- #


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool, data_feed: str = "iex"):
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._feed = DataFeed(data_feed)

    @staticmethod
    def _wrap(fn, *args, **kwargs):
        from alpaca.common.exceptions import APIError

        try:
            return fn(*args, **kwargs)
        except APIError as exc:
            raise BrokerError(f"Alpaca: {exc}") from exc
        except Exception as exc:  # network errors etc.
            raise BrokerError(f"{type(exc).__name__}: {exc}") from exc

    def get_account(self) -> dict:
        a = self._wrap(self._trading.get_account)
        return {
            "account_number": a.account_number,
            "status": _enum(a.status),
            "equity": _f(a.equity),
            "last_equity": _f(a.last_equity),
            "cash": _f(a.cash),
            "buying_power": _f(a.buying_power),
            "portfolio_value": _f(a.portfolio_value),
            "long_market_value": _f(a.long_market_value),
            "pattern_day_trader": bool(a.pattern_day_trader),
            "trading_blocked": bool(a.trading_blocked),
        }

    def get_positions(self) -> list[dict]:
        out = []
        for p in self._wrap(self._trading.get_all_positions):
            out.append(
                {
                    "symbol": p.symbol,
                    "qty": _f(p.qty),
                    "avg_entry_price": _f(p.avg_entry_price),
                    "current_price": _f(p.current_price),
                    "market_value": _f(p.market_value),
                    "cost_basis": _f(p.cost_basis),
                    "unrealized_pl": _f(p.unrealized_pl),
                    "unrealized_plpc": _f(p.unrealized_plpc),
                    "change_today": _f(p.change_today),
                }
            )
        return out

    def get_orders(self, status: str = "open", limit: int = 50) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus(status), limit=limit)
        return [_order_to_dict(o) for o in self._wrap(self._trading.get_orders, filter=req)]

    def cancel_order(self, order_id: str) -> None:
        self._wrap(self._trading.cancel_order_by_id, order_id)

    def submit_order(self, leg: Leg, client_order_id: str | None = None) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        if leg.amount_type == "all":
            return self.close_position(leg.symbol)
        common = dict(
            symbol=leg.symbol,
            side=OrderSide(leg.side),
            time_in_force=TimeInForce(leg.time_in_force),
            client_order_id=client_order_id,
        )
        if leg.amount_type == "dollars":
            common["notional"] = round(float(leg.amount), 2)
        else:
            common["qty"] = float(leg.amount)
        if leg.order_type == "limit":
            req = LimitOrderRequest(limit_price=round(float(leg.limit_price), 2), **common)
        else:
            req = MarketOrderRequest(**common)
        return _order_to_dict(self._wrap(self._trading.submit_order, order_data=req))

    def close_position(self, symbol: str) -> dict:
        return _order_to_dict(self._wrap(self._trading.close_position, symbol))

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        from alpaca.data.requests import StockLatestTradeRequest

        if not symbols:
            return {}
        req = StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=self._feed)
        trades = self._wrap(self._data.get_stock_latest_trade, req)
        return {sym: float(t.price) for sym, t in trades.items()}

    def get_clock(self) -> dict:
        c = self._wrap(self._trading.get_clock)
        return {
            "is_open": bool(c.is_open),
            "next_open": _iso(c.next_open),
            "next_close": _iso(c.next_close),
        }


def _order_to_dict(o) -> dict:
    return {
        "id": str(o.id),
        "client_order_id": o.client_order_id,
        "symbol": o.symbol,
        "side": _enum(o.side),
        "type": _enum(o.order_type or o.type),
        "qty": _f(o.qty),
        "notional": _f(o.notional),
        "limit_price": _f(o.limit_price),
        "filled_qty": _f(o.filled_qty),
        "filled_avg_price": _f(o.filled_avg_price),
        "status": _enum(o.status),
        "time_in_force": _enum(o.time_in_force),
        "submitted_at": _iso(o.submitted_at),
    }


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #

_DEMO_PRICES = {
    "AAPL": 228.0,
    "MSFT": 415.0,
    "NVDA": 135.0,
    "AMZN": 198.0,
    "GOOGL": 172.0,
    "META": 560.0,
    "TSLA": 250.0,
    "SPY": 575.0,
    "QQQ": 495.0,
    "VTI": 285.0,
    "AMD": 150.0,
    "NFLX": 700.0,
}


class DemoBroker:
    """A self-contained fake brokerage. Prices drift a little on every read."""

    def __init__(self, seed: int | None = None, starting_cash: float = 100_000.0):
        self._lock = threading.RLock()
        self._rng = random.Random(seed)
        self._prices = dict(_DEMO_PRICES)
        self._open_prices = dict(_DEMO_PRICES)
        self.cash = starting_cash
        self._start_equity = starting_cash
        self.positions: dict[str, dict] = {}  # symbol -> {"qty", "cost"}
        self.orders: list[dict] = []
        self.market_open: bool | None = None  # None => follow the real NYSE clock roughly
        self.volatility = 0.002
        for sym, qty in (("AAPL", 40), ("MSFT", 15), ("SPY", 20)):
            px = self._prices[sym]
            self.positions[sym] = {"qty": float(qty), "cost": qty * px * 0.92}
            self.cash -= qty * px * 0.92
        self._start_equity = self._equity()

    # -- price simulation ------------------------------------------------- #
    def _price(self, symbol: str) -> float:
        if symbol not in self._prices:
            # Unknown tickers get a stable pseudo-price so anything can be tried.
            self._prices[symbol] = 20 + (abs(hash(symbol)) % 400)
            self._open_prices[symbol] = self._prices[symbol]
        return self._prices[symbol]

    def _tick(self) -> None:
        for sym, px in self._prices.items():
            self._prices[sym] = round(max(0.5, px * (1 + self._rng.gauss(0, self.volatility))), 2)
        self._match_limits()

    def set_price(self, symbol: str, price: float) -> None:
        """Test hook: pin a price."""
        with self._lock:
            self._price(symbol)
            self._prices[symbol] = price
            self._match_limits()

    def _equity(self) -> float:
        return self.cash + sum(p["qty"] * self._price(s) for s, p in self.positions.items())

    # -- order matching --------------------------------------------------- #
    def _fill(self, order: dict, price: float) -> None:
        qty = order["qty"] if order["qty"] is not None else round(order["notional"] / price, 6)
        sym = order["symbol"]
        pos = self.positions.get(sym, {"qty": 0.0, "cost": 0.0})
        if order["side"] == "buy":
            if qty * price > self.cash + 1e-6:
                order["status"] = "rejected"
                return
            self.cash -= qty * price
            pos["qty"] += qty
            pos["cost"] += qty * price
        else:
            if qty > pos["qty"] + 1e-9:
                order["status"] = "rejected"
                return
            avg = pos["cost"] / pos["qty"] if pos["qty"] else 0
            self.cash += qty * price
            pos["qty"] -= qty
            pos["cost"] -= avg * qty
        if pos["qty"] <= 1e-9:
            self.positions.pop(sym, None)
        else:
            self.positions[sym] = pos
        order.update(
            status="filled", filled_qty=qty, filled_avg_price=price, qty=qty
        )

    def _match_limits(self) -> None:
        for o in self.orders:
            if o["status"] != "new" or o["type"] != "limit":
                continue
            px = self._prices.get(o["symbol"])
            if px is None:
                continue
            if (o["side"] == "buy" and px <= o["limit_price"]) or (
                o["side"] == "sell" and px >= o["limit_price"]
            ):
                self._fill(o, o["limit_price"])

    # -- Broker interface ------------------------------------------------- #
    def get_account(self) -> dict:
        with self._lock:
            self._tick()
            lmv = sum(p["qty"] * self._price(s) for s, p in self.positions.items())
            eq = self.cash + lmv
            return {
                "account_number": "DEMO-0001",
                "status": "ACTIVE",
                "equity": round(eq, 2),
                "last_equity": round(self._start_equity, 2),
                "cash": round(self.cash, 2),
                "buying_power": round(self.cash, 2),
                "portfolio_value": round(eq, 2),
                "long_market_value": round(lmv, 2),
                "pattern_day_trader": False,
                "trading_blocked": False,
            }

    def get_positions(self) -> list[dict]:
        with self._lock:
            out = []
            for sym, p in sorted(self.positions.items()):
                px = self._price(sym)
                mv = p["qty"] * px
                out.append(
                    {
                        "symbol": sym,
                        "qty": round(p["qty"], 6),
                        "avg_entry_price": round(p["cost"] / p["qty"], 2),
                        "current_price": px,
                        "market_value": round(mv, 2),
                        "cost_basis": round(p["cost"], 2),
                        "unrealized_pl": round(mv - p["cost"], 2),
                        "unrealized_plpc": (mv - p["cost"]) / p["cost"] if p["cost"] else 0,
                        "change_today": px / self._open_prices[sym] - 1,
                    }
                )
            return out

    def get_orders(self, status: str = "open", limit: int = 50) -> list[dict]:
        with self._lock:
            if status == "open":
                rows = [o for o in self.orders if o["status"] == "new"]
            elif status == "closed":
                rows = [o for o in self.orders if o["status"] != "new"]
            else:
                rows = list(self.orders)
            return [dict(o) for o in reversed(rows)][:limit]

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            for o in self.orders:
                if o["id"] == order_id and o["status"] == "new":
                    o["status"] = "canceled"
                    return
            raise BrokerError("Order not found or not open")

    def submit_order(self, leg: Leg, client_order_id: str | None = None) -> dict:
        with self._lock:
            if leg.amount_type == "all":
                return self.close_position(leg.symbol)
            price = self._price(leg.symbol)
            order = {
                "id": str(uuid.uuid4()),
                "client_order_id": client_order_id or str(uuid.uuid4()),
                "symbol": leg.symbol,
                "side": leg.side,
                "type": leg.order_type,
                "qty": float(leg.amount) if leg.amount_type == "shares" else None,
                "notional": float(leg.amount) if leg.amount_type == "dollars" else None,
                "limit_price": leg.limit_price,
                "filled_qty": 0.0,
                "filled_avg_price": None,
                "status": "new",
                "time_in_force": leg.time_in_force,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }
            self.orders.append(order)
            if leg.order_type == "market":
                self._fill(order, price)
            else:
                self._match_limits()
            if order["status"] == "rejected":
                raise BrokerError(
                    "Demo: insufficient buying power"
                    if leg.side == "buy"
                    else "Demo: insufficient shares to sell"
                )
            return dict(order)

    def close_position(self, symbol: str) -> dict:
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                raise BrokerError(f"No open position in {symbol}")
            return self.submit_order(Leg(symbol=symbol, side="sell", amount=pos["qty"]))

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        with self._lock:
            self._tick()
            return {s: self._price(s) for s in symbols}

    def get_clock(self) -> dict:
        now = datetime.now(ZoneInfo("America/New_York"))
        if self.market_open is not None:
            is_open = self.market_open
        else:
            mins = now.hour * 60 + now.minute
            is_open = now.weekday() < 5 and 570 <= mins < 960
        next_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if next_open <= now:
            next_open += timedelta(days=1)
        while next_open.weekday() >= 5:
            next_open += timedelta(days=1)
        next_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        if next_close <= now:
            next_close = next_open.replace(hour=16, minute=0)
        return {
            "is_open": is_open,
            "next_open": next_open.isoformat(),
            "next_close": next_close.isoformat(),
        }
