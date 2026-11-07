"""Core automation: placing orders, running schedules and evaluating price rules."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .broker import AlpacaBroker, Broker, BrokerError, DemoBroker, Leg
from .config import Settings
from .db import Account, ActivityLog, PriceRule, ScheduledOrder, as_utc, utcnow
from .vault import SecretBox

log = logging.getLogger("alpaca_manager")

# A one-off schedule whose time passed more than this long ago is not run late.
MISFIRE_GRACE_SECONDS = 300


def compute_metric(metric: str, basket: list[dict], prices: dict[str, float],
                   positions: list[dict] | None = None) -> float:
    """Value that a price rule watches.

    price    - latest price of the first basket symbol
    basket   - sum(qty * price) for each basket entry
    holdings - market value of the account's current positions in the basket symbols
    """
    symbols = [b["symbol"] for b in basket]
    missing = [s for s in symbols if s not in prices]
    if missing:
        raise BrokerError(f"No price available for {', '.join(missing)}")
    if metric == "price":
        return prices[symbols[0]]
    if metric == "basket":
        return sum(float(b.get("qty") or 0) * prices[b["symbol"]] for b in basket)
    if metric == "holdings":
        held = {p["symbol"]: p["qty"] for p in (positions or [])}
        return sum(held.get(s, 0.0) * prices[s] for s in symbols)
    raise ValueError(f"Unknown metric {metric!r}")


def condition_met(direction: str, value: float, threshold: float) -> bool:
    return value >= threshold if direction == "above" else value <= threshold


class Engine:
    def __init__(self, settings: Settings, sessions: sessionmaker, vault: SecretBox):
        self.settings = settings
        self.sessions = sessions
        self.vault = vault
        self.tz = ZoneInfo(settings.timezone)
        self._brokers: dict[int, Broker] = {}
        self._demo_brokers: dict[int, DemoBroker] = {}  # survive account edits
        self._lock = threading.RLock()
        self._rules_lock = threading.Lock()
        self.scheduler = BackgroundScheduler(timezone=self.tz)

    # ------------------------------------------------------------------ #
    # Brokers
    # ------------------------------------------------------------------ #
    def broker_for(self, account: Account) -> Broker:
        with self._lock:
            if account.id in self._brokers:
                return self._brokers[account.id]
            if account.mode == "demo":
                broker = self._demo_brokers.setdefault(account.id, DemoBroker(seed=account.id))
            else:
                broker = AlpacaBroker(
                    account.api_key,
                    self.vault.decrypt(account.secret_enc),
                    paper=account.mode == "paper",
                    data_feed=account.data_feed or "iex",
                )
            self._brokers[account.id] = broker
            return broker

    def forget_broker(self, account_id: int, drop_demo: bool = False) -> None:
        with self._lock:
            self._brokers.pop(account_id, None)
            if drop_demo:
                self._demo_brokers.pop(account_id, None)

    # ------------------------------------------------------------------ #
    # Activity log
    # ------------------------------------------------------------------ #
    def record(self, message: str, *, account_id: int | None = None, level: str = "info",
               source: str = "", session: Session | None = None) -> None:
        log.log(logging.ERROR if level == "error" else logging.INFO, "[%s] %s", source, message)
        entry = ActivityLog(account_id=account_id, level=level, source=source, message=message)
        if session is not None:
            session.add(entry)
            return
        with self.sessions() as s:
            s.add(entry)
            s.commit()

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #
    def execute_legs(self, account: Account, legs: list[dict], *, source: str,
                     dry_run: bool = False, session: Session | None = None) -> list[dict]:
        """Submit each leg independently; one failure does not stop the others."""
        broker = self.broker_for(account)
        results = []
        for raw in legs:
            leg = Leg.from_dict(raw)
            if dry_run:
                self.record(f"DRY RUN - would place: {leg.describe()}", account_id=account.id,
                            level="info", source=source, session=session)
                results.append({"leg": raw, "ok": True, "dry_run": True})
                continue
            try:
                order = broker.submit_order(leg, client_order_id=f"am-{uuid.uuid4().hex[:20]}")
            except BrokerError as exc:
                self.record(f"FAILED {leg.describe()}: {exc}", account_id=account.id,
                            level="error", source=source, session=session)
                results.append({"leg": raw, "ok": False, "error": str(exc)})
                continue
            self.record(f"Placed {leg.describe()} (status: {order.get('status')})",
                        account_id=account.id, level="order", source=source, session=session)
            results.append({"leg": raw, "ok": True, "order": order})
        return results

    def _market_open(self, account: Account) -> bool:
        return bool(self.broker_for(account).get_clock()["is_open"])

    # ------------------------------------------------------------------ #
    # Scheduled orders
    # ------------------------------------------------------------------ #
    def run_schedule(self, schedule_id: int) -> None:
        with self.sessions() as s:
            sched = s.get(ScheduledOrder, schedule_id)
            if sched is None or sched.status != "active":
                return
            account = sched.account
            source = f"Schedule: {sched.name}"
            try:
                if sched.only_when_market_open and not self._market_open(account):
                    self.record("Skipped - market is closed", account_id=account.id,
                                level="warning", source=source, session=s)
                else:
                    self.execute_legs(account, sched.legs, source=source,
                                      dry_run=sched.dry_run, session=s)
            except (BrokerError, ValueError) as exc:
                self.record(f"Error: {exc}", account_id=account.id, level="error",
                            source=source, session=s)
            sched.last_run_at = utcnow()
            if sched.kind == "once":
                sched.status = "done"
            s.commit()

    def _job_id(self, schedule_id: int) -> str:
        return f"schedule-{schedule_id}"

    def sync_schedule(self, sched: ScheduledOrder) -> None:
        """(Re)register the APScheduler job for one schedule from its DB row."""
        job_id = self._job_id(sched.id)
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
        if sched.status != "active":
            return
        if sched.kind == "once":
            trigger = DateTrigger(run_date=as_utc(sched.run_at))
        else:
            hour, minute = (int(x) for x in sched.time_of_day.split(":"))
            trigger = CronTrigger(day_of_week=",".join(sched.days_of_week), hour=hour,
                                  minute=minute, timezone=self.tz)
        self.scheduler.add_job(self.run_schedule, trigger, args=[sched.id], id=job_id,
                               misfire_grace_time=MISFIRE_GRACE_SECONDS, coalesce=True,
                               replace_existing=True)

    def unschedule(self, schedule_id: int) -> None:
        job_id = self._job_id(schedule_id)
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def next_run(self, schedule_id: int) -> datetime | None:
        job = self.scheduler.get_job(self._job_id(schedule_id))
        # Jobs added before the scheduler starts have no computed next_run_time yet.
        return getattr(job, "next_run_time", None) if job else None

    def load_schedules(self) -> None:
        now = utcnow()
        with self.sessions() as s:
            for sched in s.scalars(select(ScheduledOrder).where(ScheduledOrder.status == "active")):
                if sched.kind == "once":
                    run_at = as_utc(sched.run_at)
                    if run_at < now - timedelta(seconds=MISFIRE_GRACE_SECONDS):
                        sched.status = "missed"
                        self.record("Missed - the app was not running at the scheduled time",
                                    account_id=sched.account_id, level="warning",
                                    source=f"Schedule: {sched.name}", session=s)
                        continue
                self.sync_schedule(sched)
            s.commit()

    # ------------------------------------------------------------------ #
    # Price rules
    # ------------------------------------------------------------------ #
    def evaluate_rules(self) -> None:
        # Never let two evaluations overlap (it could double-fire a rule).
        if not self._rules_lock.acquire(blocking=False):
            return
        try:
            with self.sessions() as s:
                rules = list(s.scalars(select(PriceRule).where(PriceRule.status == "active")))
                by_account: dict[int, list[PriceRule]] = {}
                for r in rules:
                    by_account.setdefault(r.account_id, []).append(r)
                for account_id, acct_rules in by_account.items():
                    self._evaluate_account_rules(s, s.get(Account, account_id), acct_rules)
                s.commit()
        finally:
            self._rules_lock.release()

    def _evaluate_account_rules(self, s: Session, account: Account,
                                rules: list[PriceRule]) -> None:
        now = utcnow()
        try:
            broker = self.broker_for(account)
            symbols = sorted({b["symbol"] for r in rules for b in r.basket})
            prices = broker.get_latest_prices(symbols)
            positions = broker.get_positions() if any(r.metric == "holdings" for r in rules) else []
            market_open = None
        except (BrokerError, ValueError) as exc:
            for r in rules:
                r.last_error = str(exc)
                r.last_checked_at = now
            return

        for rule in rules:
            source = f"Rule: {rule.name}"
            try:
                value = compute_metric(rule.metric, rule.basket, prices, positions)
            except (BrokerError, ValueError) as exc:
                rule.last_error = str(exc)
                rule.last_checked_at = now
                continue
            rule.last_value = round(value, 4)
            rule.last_checked_at = now
            rule.last_error = None
            met = condition_met(rule.direction, value, rule.threshold)

            if not met:
                if rule.repeat and not rule.armed:
                    rule.armed = True  # condition cleared - ready to fire again
                continue
            if not rule.armed:
                continue
            last = as_utc(rule.last_triggered_at)
            if rule.repeat and last and now - last < timedelta(minutes=rule.cooldown_minutes):
                continue
            if rule.only_when_market_open:
                if market_open is None:
                    try:
                        market_open = self._market_open(account)
                    except BrokerError as exc:
                        rule.last_error = str(exc)
                        continue
                if not market_open:
                    continue  # stays armed; fires once the market opens

            self.record(
                f"Triggered: value {value:,.2f} is {rule.direction} {rule.threshold:,.2f}",
                account_id=account.id, level="info", source=source, session=s)
            self.execute_legs(account, rule.legs, source=source, dry_run=rule.dry_run, session=s)
            rule.last_triggered_at = now
            rule.armed = False
            if not rule.repeat:
                rule.status = "triggered"

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self.scheduler.add_job(self.evaluate_rules, "interval", seconds=self.settings.poll_seconds,
                               id="evaluate-rules", max_instances=1, coalesce=True,
                               next_run_time=datetime.now(timezone.utc) + timedelta(seconds=2))
        self.load_schedules()
        self.scheduler.start()
        self.record(f"Automation started (price rules checked every "
                    f"{self.settings.poll_seconds}s)", source="System")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
