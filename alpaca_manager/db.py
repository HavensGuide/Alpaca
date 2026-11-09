"""Database models (SQLite via SQLAlchemy)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # "paper", "live" or "demo" (simulated, no network).
    mode: Mapped[str] = mapped_column(String(10), default="paper")
    api_key: Mapped[str] = mapped_column(String(200), default="")
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    # Market data feed: "iex" (free) or "sip" (paid subscription).
    data_feed: Mapped[str] = mapped_column(String(20), default="iex")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    schedules: Mapped[list["ScheduledOrder"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    rules: Mapped[list["PriceRule"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )


class ScheduledOrder(Base):
    """Buy/sell orders that run at a time, once or on repeat."""

    __tablename__ = "scheduled_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    # "once" (run_at) or "recurring" (days_of_week + time_of_day)
    kind: Mapped[str] = mapped_column(String(20))
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    days_of_week: Mapped[list[str]] = mapped_column(JSON, default=list)  # ["mon", ...]
    time_of_day: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "HH:MM"
    legs: Mapped[list[dict]] = mapped_column(JSON, default=list)
    only_when_market_open: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    # "active", "paused", "done" (one-off ran), "missed" (one-off time passed while app was off)
    status: Mapped[str] = mapped_column(String(20), default="active")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="schedules")


class PriceRule(Base):
    """'When <metric> goes above/below <threshold>, place these orders.'"""

    __tablename__ = "price_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    # "price": price of one symbol (basket has one entry, qty ignored)
    # "basket": sum(qty * price) over the basket entries
    # "holdings": current market value of the account's positions in the basket symbols
    metric: Mapped[str] = mapped_column(String(20))
    basket: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{"symbol":..,"qty":..}]
    direction: Mapped[str] = mapped_column(String(10))  # "above" or "below"
    threshold: Mapped[float] = mapped_column(Float)
    legs: Mapped[list[dict]] = mapped_column(JSON, default=list)
    # Repeat: after firing, re-arm once the condition is no longer true and the cooldown passed.
    repeat: Mapped[bool] = mapped_column(Boolean, default=False)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=60)
    only_when_market_open: Mapped[bool] = mapped_column(Boolean, default=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    # "active", "paused", "triggered" (one-shot rule that fired)
    status: Mapped[str] = mapped_column(String(20), default="active")
    armed: Mapped[bool] = mapped_column(Boolean, default=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_triggered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    account: Mapped[Account] = relationship(back_populates="rules")


class ActivityLog(Base):
    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "info", "order", "warning", "error"
    level: Mapped[str] = mapped_column(String(10), default="info")
    source: Mapped[str] = mapped_column(String(100), default="")
    message: Mapped[str] = mapped_column(Text)


class Setting(Base):
    """Small key/value store (e.g. the watchlist)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)


class Basket(Base):
    """A named group of stocks, charted as one value: sum(qty * price)."""

    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    items: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{"symbol", "qty"}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BacktestRun(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy_key: Mapped[str] = mapped_column(String(100))
    strategy_name: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    # Summary metrics, kept small for the history list.
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    # Full result (equity curve, trades, ...).
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class StrategyBot(Base):
    """A strategy plugin paper-trading an account with its own money allocation."""

    __tablename__ = "bots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    strategy_key: Mapped[str] = mapped_column(String(100))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list)
    timeframe: Mapped[str] = mapped_column(String(10), default="1Day")
    run_time: Mapped[str] = mapped_column(String(5), default="15:45")  # daily bots, NY time
    benchmark: Mapped[str] = mapped_column(String(10), default="SPY")
    allocation: Mapped[float] = mapped_column(Float)
    # The bot's own ledger, separate from anything else in the account.
    cash: Mapped[float] = mapped_column(Float)
    positions: Mapped[dict] = mapped_column(JSON, default=dict)  # symbol -> shares
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # strategy's ctx.state
    benchmark_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | paused
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    snapshots: Mapped[list["BotSnapshot"]] = relationship(cascade="all, delete-orphan",
                                                          order_by="BotSnapshot.id")
    trades: Mapped[list["BotTrade"]] = relationship(cascade="all, delete-orphan",
                                                    order_by="BotTrade.id")


class BotSnapshot(Base):
    __tablename__ = "bot_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    equity: Mapped[float] = mapped_column(Float)
    benchmark: Mapped[float | None] = mapped_column(Float, nullable=True)  # benchmark price


class BotTrade(Base):
    __tablename__ = "bot_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_id: Mapped[int] = mapped_column(ForeignKey("bots.id", ondelete="CASCADE"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    # True when the broker hadn't reported a fill price yet and the last trade price was used.
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


def make_session_factory(db_url: str) -> sessionmaker:
    engine = create_engine(db_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; values are always stored as UTC, so restore it."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
