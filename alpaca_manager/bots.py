"""Live strategy bots: run a strategy plugin against a paper or demo account.

Each bot has its own ledger (cash + shares) carved out of the account, so its
performance can be measured on its own even when the account holds other things.
For safety, bots refuse to run on live-money accounts.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from .backtest import align, rebalance
from .broker import BrokerError, Leg
from .db import Account, BotSnapshot, BotTrade, StrategyBot, utcnow
from .marketdata import regular_session, warmup_days
from .strategies.base import Context
from .strategies.registry import Registry

log = logging.getLogger("alpaca_manager")

BOT_TIMEFRAMES = {"1Day": "Once a day", "1Hour": "Every hour", "15Min": "Every 15 minutes"}


def _jsonable(state: dict) -> dict:
    return json.loads(json.dumps(state, default=str))


class BotRunner:
    def __init__(self, engine, registry: Registry):
        self.engine = engine
        self.registry = registry
        self._locks: dict[int, threading.Lock] = {}

    # ------------------------------------------------------------------ #
    # Scheduling
    # ------------------------------------------------------------------ #
    def _job_id(self, bot_id: int) -> str:
        return f"bot-{bot_id}"

    def trigger_for(self, bot: StrategyBot) -> CronTrigger:
        tz = self.engine.tz
        if bot.timeframe == "1Day":
            hour, minute = (int(x) for x in bot.run_time.split(":"))
            return CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=tz)
        if bot.timeframe == "1Hour":
            return CronTrigger(day_of_week="mon-fri", hour="10-15", minute=31, timezone=tz)
        return CronTrigger(day_of_week="mon-fri", hour="9-15", minute="1,16,31,46", timezone=tz)

    def sync(self, bot: StrategyBot) -> None:
        sched = self.engine.scheduler
        if sched.get_job(self._job_id(bot.id)):
            sched.remove_job(self._job_id(bot.id))
        if bot.status == "active":
            sched.add_job(self.run, self.trigger_for(bot), args=[bot.id], id=self._job_id(bot.id),
                          misfire_grace_time=300, coalesce=True, replace_existing=True)

    def unschedule(self, bot_id: int) -> None:
        if self.engine.scheduler.get_job(self._job_id(bot_id)):
            self.engine.scheduler.remove_job(self._job_id(bot_id))

    def next_run(self, bot_id: int):
        job = self.engine.scheduler.get_job(self._job_id(bot_id))
        return getattr(job, "next_run_time", None) if job else None

    def load(self) -> None:
        with self.engine.sessions() as s:
            for bot in s.scalars(select(StrategyBot).where(StrategyBot.status == "active")):
                self.sync(bot)

    # ------------------------------------------------------------------ #
    # Running
    # ------------------------------------------------------------------ #
    def run(self, bot_id: int, manual: bool = False) -> dict:
        lock = self._locks.setdefault(bot_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return {"ok": False, "message": "This bot is already running"}
        try:
            return self._run(bot_id, manual)
        finally:
            lock.release()

    def _run(self, bot_id: int, manual: bool) -> dict:
        eng = self.engine
        with eng.sessions() as s:
            bot = s.get(StrategyBot, bot_id)
            if bot is None or (bot.status != "active" and not manual):
                return {"ok": False, "message": "Bot is not active"}
            account = s.get(Account, bot.account_id)
            source = f"Bot: {bot.name}"
            try:
                result = self._step(s, bot, account, source, manual)
                bot.last_error = None
            except (BrokerError, ValueError) as exc:
                bot.last_error = str(exc)
                eng.record(f"Error: {exc}", account_id=account.id, level="error",
                           source=source, session=s)
                result = {"ok": False, "message": str(exc)}
            bot.last_run_at = utcnow()
            s.commit()
            return result

    def _step(self, s, bot: StrategyBot, account: Account, source: str, manual: bool) -> dict:
        eng = self.engine
        if account.mode == "live":
            raise ValueError("Strategy bots only run on paper or demo accounts")
        broker = eng.broker_for(account)
        if account.mode != "demo" and not broker.get_clock()["is_open"]:
            msg = "Market is closed - nothing to do"
            if manual:
                raise ValueError(msg)
            eng.record(f"Skipped: {msg}", account_id=account.id, level="info", source=source, session=s)
            return {"ok": True, "message": msg}

        strategy = self.registry.build(bot.strategy_key, bot.params)
        need = int(strategy.warmup()) + 5
        start = datetime.now(timezone.utc) - timedelta(days=max(10, warmup_days(bot.timeframe, need)))
        raw = broker.get_bars(bot.symbols, bot.timeframe, start)
        frames = {sym: regular_session(f, bot.timeframe) for sym, f in raw.items()}
        frames, index = align(frames)
        if len(index) < strategy.warmup():
            raise ValueError(f"Only {len(index)} bars of history; the strategy needs {strategy.warmup()}")

        strategy.precompute(frames)
        live = broker.get_latest_prices(bot.symbols + [bot.benchmark])
        prices = {sym: live.get(sym) or float(frames[sym]["close"].iat[-1]) for sym in bot.symbols}
        positions = {k: float(v) for k, v in (bot.positions or {}).items() if v}
        ctx = Context(frames, index, state=dict(bot.state or {}))
        ctx._set(len(index) - 1, bot.cash, positions)
        ctx._prices = dict(prices)
        ctx.equity = bot.cash + sum(q * prices[sym] for sym, q in positions.items())

        targets = strategy.on_bar(ctx)
        bot.state = _jsonable(ctx.state)
        placed = 0
        if targets:
            plan, _ = rebalance(targets, prices, bot.cash, dict(positions), fractional=False,
                                min_trade_pct=0.01)
            for f in plan:
                leg = Leg(symbol=f["symbol"], side=f["side"], amount_type="shares",
                          amount=float(f["qty"]))
                try:
                    order = broker.submit_order(leg, client_order_id=None)
                except BrokerError as exc:
                    eng.record(f"FAILED {leg.describe()}: {exc}", account_id=account.id,
                               level="error", source=source, session=s)
                    continue
                fill_px = order.get("filled_avg_price")
                estimated = not fill_px
                px = float(fill_px or prices[f["symbol"]])
                qty = float(f["qty"])
                sign = 1 if f["side"] == "buy" else -1
                bot.cash -= sign * qty * px
                positions[f["symbol"]] = positions.get(f["symbol"], 0.0) + sign * qty
                if abs(positions[f["symbol"]]) < 1e-9:
                    positions.pop(f["symbol"])
                s.add(BotTrade(bot_id=bot.id, symbol=f["symbol"], side=f["side"], qty=qty,
                               price=px, estimated=estimated, order_id=order.get("id")))
                eng.record(f"Placed {leg.describe()} (status: {order.get('status')})",
                           account_id=account.id, level="order", source=source, session=s)
                placed += 1
            bot.positions = positions

        equity = bot.cash + sum(q * prices[sym] for sym, q in positions.items())
        s.add(BotSnapshot(bot_id=bot.id, equity=round(equity, 2), benchmark=live.get(bot.benchmark)))
        msg = f"Ran: {placed} order(s), equity ${equity:,.2f}"
        eng.record(msg, account_id=account.id, level="info", source=source, session=s)
        return {"ok": True, "message": msg, "orders": placed}
