"""Request validation for the HTTP API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _symbol(v: str) -> str:
    v = v.strip().upper()
    if not SYMBOL_RE.match(v):
        raise ValueError(f"'{v}' is not a valid ticker symbol")
    return v


class LegIn(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    amount_type: Literal["shares", "dollars", "all"] = "shares"
    amount: float | None = None
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = None
    time_in_force: Literal["day", "gtc"] = "day"

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _symbol(v)

    @model_validator(mode="after")
    def _check(self):
        if self.amount_type == "all":
            if self.side != "sell":
                raise ValueError("'Entire position' only works for sell orders")
            self.amount = None
            self.order_type = "market"
            self.limit_price = None
        elif self.amount is None or self.amount <= 0:
            raise ValueError(f"{self.symbol}: enter an amount greater than zero")
        if self.amount_type == "dollars":
            if self.order_type != "market" or self.time_in_force != "day":
                raise ValueError(f"{self.symbol}: dollar-amount orders must be market / day orders")
        if self.amount_type == "shares" and self.amount and self.amount != int(self.amount):
            if self.time_in_force != "day":
                raise ValueError(f"{self.symbol}: fractional shares require a 'day' order")
        if self.order_type == "limit":
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError(f"{self.symbol}: limit orders need a limit price")
        else:
            self.limit_price = None
        return self


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["paper", "live", "demo"] = "paper"
    api_key: str = ""
    secret_key: str = ""  # blank on update = keep existing
    data_feed: Literal["iex", "sip"] = "iex"


class OrderIn(BaseModel):
    legs: list[LegIn] = Field(min_length=1, max_length=20)


class BasketItem(BaseModel):
    symbol: str
    qty: float = 1.0

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _symbol(v)


class ScheduleIn(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["once", "recurring"]
    # Local wall-clock time in the app's timezone, e.g. "2026-11-09T09:45"
    run_at: datetime | None = None
    days_of_week: list[str] = []
    time_of_day: str | None = None
    legs: list[LegIn] = Field(min_length=1, max_length=20)
    only_when_market_open: bool = True
    dry_run: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.kind == "once":
            if self.run_at is None:
                raise ValueError("Pick a date and time")
        else:
            days = [d.lower()[:3] for d in self.days_of_week]
            if not days or any(d not in DAYS for d in days):
                raise ValueError("Pick at least one day of the week")
            self.days_of_week = [d for d in DAYS if d in days]
            if not self.time_of_day or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", self.time_of_day):
                raise ValueError("Pick a time of day (HH:MM)")
        return self


class RuleIn(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=200)
    metric: Literal["price", "basket", "holdings"]
    basket: list[BasketItem] = Field(min_length=1, max_length=50)
    direction: Literal["above", "below"]
    threshold: float = Field(gt=0)
    legs: list[LegIn] = Field(min_length=1, max_length=20)
    repeat: bool = False
    cooldown_minutes: int = Field(default=60, ge=1, le=60 * 24 * 30)
    only_when_market_open: bool = True
    dry_run: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.metric == "price":
            self.basket = self.basket[:1]
        if self.metric == "basket" and any(b.qty <= 0 for b in self.basket):
            raise ValueError("Every basket entry needs a share quantity above zero")
        return self


class PreviewIn(BaseModel):
    account_id: int
    metric: Literal["price", "basket", "holdings"]
    basket: list[BasketItem] = Field(min_length=1, max_length=50)


class StatusIn(BaseModel):
    status: Literal["active", "paused"]
