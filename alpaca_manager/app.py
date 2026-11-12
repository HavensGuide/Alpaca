"""FastAPI application: JSON API + the single-page UI."""

from __future__ import annotations

import hashlib
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import api_extra
from .bots import BotRunner
from .broker import AlpacaBroker, BrokerError, Leg
from .config import Settings
from .db import (Account, ActivityLog, PriceRule, ScheduledOrder, StrategyBot, as_utc,
                 make_session_factory, utcnow)
from .engine import Engine, compute_metric
from .marketdata import MarketData
from .strategies.registry import Registry
from .schemas import AccountIn, OrderIn, PreviewIn, RuleIn, ScheduleIn, StatusIn
from .vault import SecretBox

STATIC = Path(__file__).parent / "static"


def ui_version() -> str:
    """Short hash of the UI files' names, sizes and modification times."""
    h = hashlib.sha1()
    for f in sorted(STATIC.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    return h.hexdigest()[:10]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_dirs()
    sessions = make_session_factory(settings.db_url)
    vault = SecretBox(settings.data_dir)
    engine = Engine(settings, sessions, vault)
    registry = Registry(settings.data_dir / "plugins")
    market = MarketData()
    bots = BotRunner(engine, registry)

    if settings.seed_demo:
        with sessions() as s:
            if not s.scalar(select(Account).limit(1)):
                s.add(Account(name="Demo portfolio", mode="demo"))
                s.commit()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if settings.run_scheduler:
            engine.start()
            bots.load()
        yield
        engine.shutdown()

    app = FastAPI(title="Alpaca Manager", lifespan=lifespan)
    app.state.engine = engine
    app.state.bots = bots
    app.state.registry = registry
    app.state.settings = settings

    def db():
        with sessions() as s:
            yield s

    def get_account(s: Session, account_id: int) -> Account:
        acct = s.get(Account, account_id)
        if acct is None:
            raise HTTPException(404, "Account not found")
        return acct

    def broker_call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (BrokerError, ValueError) as exc:
            raise HTTPException(502, str(exc)) from exc

    def local_iso(dt: datetime | None) -> str | None:
        dt = as_utc(dt)
        return dt.astimezone(engine.tz).isoformat() if dt else None

    def to_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=engine.tz)
        return dt.astimezone(timezone.utc)

    # ------------------------------------------------------------------ #
    # Serializers
    # ------------------------------------------------------------------ #
    def account_out(a: Account) -> dict:
        masked = (a.api_key[:4] + "…" + a.api_key[-2:]) if len(a.api_key) > 6 else ""
        return {"id": a.id, "name": a.name, "mode": a.mode, "data_feed": a.data_feed,
                "api_key_masked": masked, "has_secret": bool(a.secret_enc)}

    def schedule_out(x: ScheduledOrder) -> dict:
        run_at = as_utc(x.run_at)
        return {
            "id": x.id, "account_id": x.account_id, "name": x.name, "kind": x.kind,
            "run_at": local_iso(run_at),
            "run_at_local": run_at.astimezone(engine.tz).strftime("%Y-%m-%dT%H:%M") if run_at else None,
            "days_of_week": x.days_of_week, "time_of_day": x.time_of_day, "legs": x.legs,
            "legs_text": [Leg.from_dict(leg).describe() for leg in x.legs],
            "only_when_market_open": x.only_when_market_open, "dry_run": x.dry_run,
            "status": x.status, "last_run_at": local_iso(x.last_run_at),
            "next_run_at": local_iso(engine.next_run(x.id)) if x.status == "active" else None,
        }

    def rule_out(r: PriceRule) -> dict:
        return {
            "id": r.id, "account_id": r.account_id, "name": r.name, "metric": r.metric,
            "basket": r.basket, "direction": r.direction, "threshold": r.threshold,
            "legs": r.legs, "legs_text": [Leg.from_dict(leg).describe() for leg in r.legs],
            "repeat": r.repeat, "cooldown_minutes": r.cooldown_minutes,
            "only_when_market_open": r.only_when_market_open, "dry_run": r.dry_run,
            "status": r.status, "armed": r.armed, "last_value": r.last_value,
            "last_checked_at": local_iso(r.last_checked_at),
            "last_triggered_at": local_iso(r.last_triggered_at), "last_error": r.last_error,
        }

    # ------------------------------------------------------------------ #
    # General
    # ------------------------------------------------------------------ #
    @app.get("/api/status")
    def status():
        return {"timezone": settings.timezone, "poll_seconds": settings.poll_seconds,
                "now": datetime.now(engine.tz).isoformat(),
                "scheduler_running": engine.scheduler.running}

    @app.get("/api/activity")
    def activity(limit: int = 100, account_id: int | None = None, s: Session = Depends(db)):
        q = select(ActivityLog).order_by(ActivityLog.id.desc()).limit(min(limit, 500))
        if account_id is not None:
            q = q.where(ActivityLog.account_id == account_id)
        return [{"id": e.id, "at": local_iso(e.at), "account_id": e.account_id,
                 "level": e.level, "source": e.source, "message": e.message}
                for e in s.scalars(q)]

    # ------------------------------------------------------------------ #
    # Accounts
    # ------------------------------------------------------------------ #
    @app.get("/api/accounts")
    def list_accounts(s: Session = Depends(db)):
        return [account_out(a) for a in s.scalars(select(Account).order_by(Account.id))]

    def key_shape_hints(mode: str, api_key: str, secret: str) -> str:
        """Spot common copy/paste mix-ups (only shown when Alpaca has rejected the keys)."""
        key, sec = api_key.strip(), secret.strip()
        hints = []
        if key.upper().startswith("PA") and len(key) <= 14:
            hints.append(f"The key ID you entered ({key[:3]}...) looks like your paper ACCOUNT NUMBER, "
                         "not an API key ID.")
        elif len(key) < 16:
            hints.append(f"The key ID is only {len(key)} characters - API key IDs are about 20.")
        if len(sec) < 30:
            hints.append(f"The secret is only {len(sec)} characters - secrets are about 40. "
                         "It may be the key ID pasted into the wrong box.")
        if mode == "live" and key.upper().startswith("PK"):
            hints.append("The key ID starts with PK, which is usually a PAPER key, but the type is Live.")
        if mode == "paper" and key.upper().startswith("AK"):
            hints.append("The key ID starts with AK, which is usually a LIVE key, but the type is Paper.")
        return " ".join(hints)

    def check_keys(mode: str, api_key: str, secret: str, data_feed: str) -> str | None:
        """Try the keys against Alpaca so bad keys are caught on the Accounts screen.

        Blocks the save only when Alpaca definitely rejects the keys (401/403). If Alpaca
        can't be reached, the keys are saved anyway and a warning is returned.
        """
        if not settings.verify_keys:
            return None
        try:
            AlpacaBroker(api_key.strip(), secret.strip(), paper=mode == "paper",
                         data_feed=data_feed).get_account()
        except BrokerError as exc:
            if exc.status in (401, 403):
                hints = key_shape_hints(mode, api_key, secret)
                raise HTTPException(422, (hints + " " if hints else "") + str(exc)) from exc
            return f"Saved, but the keys couldn't be tested right now ({exc})."
        return None

    @app.post("/api/accounts", status_code=201)
    def create_account(body: AccountIn, s: Session = Depends(db)):
        if body.mode != "demo" and (not body.api_key or not body.secret_key):
            raise HTTPException(422, "API key and secret are required for paper/live accounts")
        warning = None
        if body.mode != "demo":
            warning = check_keys(body.mode, body.api_key, body.secret_key, body.data_feed)
        acct = Account(name=body.name, mode=body.mode, api_key=body.api_key.strip(),
                       secret_enc=vault.encrypt(body.secret_key.strip()) if body.secret_key else "",
                       data_feed=body.data_feed)
        s.add(acct)
        s.commit()
        engine.record(f"Account '{acct.name}' added ({acct.mode})", account_id=acct.id,
                      source="Accounts")
        return dict(account_out(acct), warning=warning)

    @app.put("/api/accounts/{account_id}")
    def update_account(account_id: int, body: AccountIn, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        new_key = body.api_key.strip()
        # Keys come in pairs: never combine a new key ID with the old stored secret, and
        # paper and live accounts never share keys.
        if new_key and new_key != acct.api_key and not body.secret_key:
            raise HTTPException(422, "You entered a new API key ID - also enter the secret key that "
                                     "was generated with it (the old secret won't work with it).")
        if {body.mode, acct.mode} == {"paper", "live"} and not (new_key and body.secret_key):
            raise HTTPException(422, "Paper and live accounts use different API keys - enter both the "
                                     f"key ID and secret for your {body.mode} account.")
        # Only re-test with Alpaca when the keys or the account type change, so an account
        # can always be renamed or have its data feed changed.
        keys_changed = bool(body.secret_key) or (new_key and new_key != acct.api_key) or body.mode != acct.mode
        acct.name = body.name
        acct.data_feed = body.data_feed
        if body.mode != acct.mode:
            acct.mode = body.mode
        if body.api_key:
            acct.api_key = body.api_key.strip()
        if body.secret_key:
            acct.secret_enc = vault.encrypt(body.secret_key.strip())
        if acct.mode != "demo" and (not acct.api_key or not acct.secret_enc):
            raise HTTPException(422, "API key and secret are required for paper/live accounts")
        warning = None
        if acct.mode != "demo" and keys_changed:
            warning = check_keys(acct.mode, acct.api_key, vault.decrypt(acct.secret_enc), acct.data_feed)
        s.commit()
        engine.forget_broker(acct.id)
        return dict(account_out(acct), warning=warning)

    @app.delete("/api/accounts/{account_id}", status_code=204)
    def delete_account(account_id: int, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        for sched in acct.schedules:
            engine.unschedule(sched.id)
        for bot_id in s.scalars(select(StrategyBot.id).where(StrategyBot.account_id == account_id)):
            bots.unschedule(bot_id)
        name = acct.name
        s.delete(acct)
        s.commit()
        engine.forget_broker(account_id, drop_demo=True)
        engine.record(f"Account '{name}' removed", source="Accounts")

    @app.get("/api/accounts/{account_id}/overview")
    def overview(account_id: int, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        broker = broker_call(engine.broker_for, acct)
        return {"account": broker_call(broker.get_account),
                "positions": broker_call(broker.get_positions),
                "clock": broker_call(broker.get_clock)}

    @app.get("/api/accounts/{account_id}/orders")
    def orders(account_id: int, status: str = "open", limit: int = 50, s: Session = Depends(db)):
        if status not in {"open", "closed", "all"}:
            raise HTTPException(422, "status must be open, closed or all")
        acct = get_account(s, account_id)
        return broker_call(engine.broker_for(acct).get_orders, status, min(limit, 500))

    @app.post("/api/accounts/{account_id}/orders")
    def place_orders(account_id: int, body: OrderIn, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        results = broker_call(engine.execute_legs, acct, [leg.model_dump() for leg in body.legs],
                              source="Manual order", session=s)
        s.commit()
        return {"results": results}

    @app.delete("/api/accounts/{account_id}/orders/{order_id}", status_code=204)
    def cancel_order(account_id: int, order_id: str, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        broker_call(engine.broker_for(acct).cancel_order, order_id)
        engine.record(f"Cancelled order {order_id[:8]}", account_id=acct.id, source="Manual order")

    @app.post("/api/accounts/{account_id}/positions/{symbol}/close")
    def close_position(account_id: int, symbol: str, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        results = broker_call(engine.execute_legs, acct,
                              [{"symbol": symbol.upper(), "side": "sell", "amount_type": "all"}],
                              source="Manual order", session=s)
        s.commit()
        return {"results": results}

    @app.get("/api/accounts/{account_id}/quotes")
    def quotes(account_id: int, symbols: str, s: Session = Depends(db)):
        acct = get_account(s, account_id)
        syms = sorted({x.strip().upper() for x in symbols.split(",") if x.strip()})[:50]
        return broker_call(engine.broker_for(acct).get_latest_prices, syms)

    # ------------------------------------------------------------------ #
    # Scheduled orders
    # ------------------------------------------------------------------ #
    def apply_schedule(x: ScheduledOrder, body: ScheduleIn) -> None:
        x.account_id = body.account_id
        x.name = body.name
        x.kind = body.kind
        x.legs = [leg.model_dump() for leg in body.legs]
        x.only_when_market_open = body.only_when_market_open
        x.dry_run = body.dry_run
        x.status = "active"
        if body.kind == "once":
            run_at = to_utc(body.run_at)
            if run_at <= utcnow():
                raise HTTPException(422, "The scheduled time is in the past")
            x.run_at, x.days_of_week, x.time_of_day = run_at, [], None
        else:
            x.run_at, x.days_of_week, x.time_of_day = None, body.days_of_week, body.time_of_day

    @app.get("/api/schedules")
    def list_schedules(s: Session = Depends(db)):
        return [schedule_out(x) for x in s.scalars(select(ScheduledOrder).order_by(ScheduledOrder.id))]

    @app.post("/api/schedules", status_code=201)
    def create_schedule(body: ScheduleIn, s: Session = Depends(db)):
        get_account(s, body.account_id)
        x = ScheduledOrder()
        apply_schedule(x, body)
        s.add(x)
        s.commit()
        engine.sync_schedule(x)
        engine.record(f"Created schedule '{x.name}'", account_id=x.account_id, source="Schedules")
        return schedule_out(x)

    @app.put("/api/schedules/{sid}")
    def update_schedule(sid: int, body: ScheduleIn, s: Session = Depends(db)):
        x = s.get(ScheduledOrder, sid) or _404()
        get_account(s, body.account_id)
        apply_schedule(x, body)
        s.commit()
        engine.sync_schedule(x)
        return schedule_out(x)

    @app.patch("/api/schedules/{sid}/status")
    def schedule_status(sid: int, body: StatusIn, s: Session = Depends(db)):
        x = s.get(ScheduledOrder, sid) or _404()
        if body.status == "active" and x.kind == "once" and as_utc(x.run_at) <= utcnow():
            raise HTTPException(422, "This one-time schedule's time has passed - edit it to pick a new time")
        x.status = body.status
        s.commit()
        engine.sync_schedule(x)
        return schedule_out(x)

    @app.post("/api/schedules/{sid}/run")
    def run_schedule_now(sid: int, s: Session = Depends(db)):
        x = s.get(ScheduledOrder, sid) or _404()
        results = broker_call(engine.execute_legs, x.account, x.legs,
                              source=f"Schedule: {x.name} (run now)", dry_run=x.dry_run, session=s)
        s.commit()
        return {"results": results}

    @app.delete("/api/schedules/{sid}", status_code=204)
    def delete_schedule(sid: int, s: Session = Depends(db)):
        x = s.get(ScheduledOrder, sid) or _404()
        engine.unschedule(sid)
        s.delete(x)
        s.commit()

    # ------------------------------------------------------------------ #
    # Price rules
    # ------------------------------------------------------------------ #
    def apply_rule(r: PriceRule, body: RuleIn) -> None:
        for field in ("account_id", "name", "metric", "direction", "threshold", "repeat",
                      "cooldown_minutes", "only_when_market_open", "dry_run"):
            setattr(r, field, getattr(body, field))
        r.basket = [b.model_dump() for b in body.basket]
        r.legs = [leg.model_dump() for leg in body.legs]
        r.status, r.armed, r.last_error = "active", True, None

    @app.get("/api/rules")
    def list_rules(s: Session = Depends(db)):
        return [rule_out(r) for r in s.scalars(select(PriceRule).order_by(PriceRule.id))]

    @app.post("/api/rules", status_code=201)
    def create_rule(body: RuleIn, s: Session = Depends(db)):
        get_account(s, body.account_id)
        r = PriceRule()
        apply_rule(r, body)
        s.add(r)
        s.commit()
        engine.record(f"Created rule '{r.name}'", account_id=r.account_id, source="Rules")
        return rule_out(r)

    @app.put("/api/rules/{rid}")
    def update_rule(rid: int, body: RuleIn, s: Session = Depends(db)):
        r = s.get(PriceRule, rid) or _404()
        get_account(s, body.account_id)
        apply_rule(r, body)
        s.commit()
        return rule_out(r)

    @app.patch("/api/rules/{rid}/status")
    def rule_status(rid: int, body: StatusIn, s: Session = Depends(db)):
        r = s.get(PriceRule, rid) or _404()
        r.status = body.status
        if body.status == "active":
            r.armed = True
        s.commit()
        return rule_out(r)

    @app.delete("/api/rules/{rid}", status_code=204)
    def delete_rule(rid: int, s: Session = Depends(db)):
        r = s.get(PriceRule, rid) or _404()
        s.delete(r)
        s.commit()

    @app.post("/api/rules/preview")
    def preview_rule(body: PreviewIn, s: Session = Depends(db)):
        acct = get_account(s, body.account_id)
        broker = broker_call(engine.broker_for, acct)
        basket = [b.model_dump() for b in body.basket]
        if body.metric == "price":
            basket = basket[:1]
        prices = broker_call(broker.get_latest_prices, [b["symbol"] for b in basket])
        positions = broker_call(broker.get_positions) if body.metric == "holdings" else []
        value = broker_call(compute_metric, body.metric, basket, prices, positions)
        return {"value": value, "prices": prices}

    @app.post("/api/rules/evaluate")
    def evaluate_now():
        engine.evaluate_rules()
        return {"ok": True}

    api_extra.register(app, engine=engine, db=db, get_account=get_account, broker_call=broker_call,
                       local_iso=local_iso, registry=registry, market=market, bots=bots)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def no_stale_ui(request: Request, call_next):
        # Make the browser re-check the UI files on each load, so an update is
        # never hidden behind an old cached copy (it still gets fast 304s).
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index(_: Request):
        # Tag every script/stylesheet URL with a hash of the UI files, so after an
        # update the browser must fetch the new code instead of an old cached copy.
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        html = re.sub(r'((?:src|href)="/static/[^"?]+)"', rf'\1?v={ui_version()}"', html)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    return app


def _404():
    raise HTTPException(404, "Not found")
