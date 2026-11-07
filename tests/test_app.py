from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from alpaca_manager.broker import AlpacaBroker, Leg
from alpaca_manager.db import Account, ActivityLog, PriceRule, ScheduledOrder
from alpaca_manager.engine import compute_metric
from alpaca_manager.schemas import LegIn

BUY_2_AAPL = {"symbol": "AAPL", "side": "buy", "amount_type": "shares", "amount": 2}


# --------------------------------------------------------------------------- #
# Validation & metrics
# --------------------------------------------------------------------------- #
def test_leg_validation_rules():
    assert LegIn(symbol=" msft ", side="buy", amount=1).symbol == "MSFT"
    with pytest.raises(ValidationError, match="Entire position"):
        LegIn(symbol="AAPL", side="buy", amount_type="all")
    with pytest.raises(ValidationError, match="market / day"):
        LegIn(symbol="AAPL", side="buy", amount_type="dollars", amount=50, order_type="limit", limit_price=1)
    with pytest.raises(ValidationError, match="limit price"):
        LegIn(symbol="AAPL", side="buy", amount=1, order_type="limit")
    with pytest.raises(ValidationError, match="fractional"):
        LegIn(symbol="AAPL", side="buy", amount=0.5, time_in_force="gtc")
    with pytest.raises(ValidationError, match="greater than zero"):
        LegIn(symbol="AAPL", side="buy", amount=0)
    with pytest.raises(ValidationError, match="ticker"):
        LegIn(symbol="not a ticker", side="buy", amount=1)


def test_compute_metric():
    prices = {"AAPL": 100.0, "MSFT": 50.0}
    basket = [{"symbol": "AAPL", "qty": 2}, {"symbol": "MSFT", "qty": 4}]
    assert compute_metric("price", basket, prices) == 100.0
    assert compute_metric("basket", basket, prices) == 400.0
    positions = [{"symbol": "MSFT", "qty": 10}, {"symbol": "TSLA", "qty": 3}]
    assert compute_metric("holdings", basket, prices, positions) == 500.0


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
def test_account_secret_is_encrypted_and_never_returned(client, engine):
    r = client.post("/api/accounts", json={"name": "P", "mode": "paper",
                                           "api_key": "PKTESTKEY1234", "secret_key": "supersecret"})
    assert r.status_code == 201
    body = r.json()
    assert "supersecret" not in r.text and body["has_secret"] and body["api_key_masked"] == "PKTE…34"
    with engine.sessions() as s:
        acct = s.get(Account, body["id"])
        assert acct.secret_enc and "supersecret" not in acct.secret_enc
        assert engine.vault.decrypt(acct.secret_enc) == "supersecret"
    # Updating without a secret keeps the old one.
    client.put(f"/api/accounts/{body['id']}", json={"name": "Renamed", "mode": "paper"})
    with engine.sessions() as s:
        assert engine.vault.decrypt(s.get(Account, body["id"]).secret_enc) == "supersecret"


def test_paper_account_requires_keys(client):
    r = client.post("/api/accounts", json={"name": "P", "mode": "paper"})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Manual trading (demo broker)
# --------------------------------------------------------------------------- #
def test_manual_orders_and_close_position(client, demo):
    acct_id, broker = demo
    broker.set_price("NVDA", 100.0)
    r = client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [
        {"symbol": "NVDA", "side": "buy", "amount": 3},
        {"symbol": "NVDA", "side": "buy", "amount_type": "dollars", "amount": 50},
    ]})
    assert all(x["ok"] for x in r.json()["results"])
    pos = {p["symbol"]: p for p in client.get(f"/api/accounts/{acct_id}/overview").json()["positions"]}
    assert pos["NVDA"]["qty"] == pytest.approx(3.5)

    r = client.post(f"/api/accounts/{acct_id}/positions/NVDA/close")
    assert r.json()["results"][0]["ok"]
    pos = {p["symbol"] for p in client.get(f"/api/accounts/{acct_id}/overview").json()["positions"]}
    assert "NVDA" not in pos


def test_failed_leg_is_reported_and_others_still_run(client, demo):
    acct_id, _ = demo
    r = client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [
        {"symbol": "AAPL", "side": "buy", "amount": 10_000_000},  # can't afford
        BUY_2_AAPL,
    ]})
    results = r.json()["results"]
    assert not results[0]["ok"] and "buying power" in results[0]["error"]
    assert results[1]["ok"]


def test_limit_order_rests_and_can_be_cancelled(client, demo):
    acct_id, broker = demo
    broker.set_price("AMD", 150.0)
    client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [
        {"symbol": "AMD", "side": "buy", "amount": 1, "order_type": "limit", "limit_price": 140}]})
    open_orders = client.get(f"/api/accounts/{acct_id}/orders?status=open").json()
    assert len(open_orders) == 1
    assert client.delete(f"/api/accounts/{acct_id}/orders/{open_orders[0]['id']}").status_code == 204
    assert client.get(f"/api/accounts/{acct_id}/orders?status=open").json() == []


# --------------------------------------------------------------------------- #
# Price rules
# --------------------------------------------------------------------------- #
def _rule(acct_id, **kw):
    body = {"account_id": acct_id, "name": "Floor", "metric": "price",
            "basket": [{"symbol": "AAPL"}], "direction": "below", "threshold": 200,
            "legs": [{"symbol": "AAPL", "side": "sell", "amount_type": "all"}]}
    body.update(kw)
    return body


def _rule_row(engine, rid) -> PriceRule:
    with engine.sessions() as s:
        return s.get(PriceRule, rid)


def test_floor_rule_sells_once(client, engine, demo):
    acct_id, broker = demo
    broker.set_price("AAPL", 210.0)
    rid = client.post("/api/rules", json=_rule(acct_id)).json()["id"]

    engine.evaluate_rules()
    row = _rule_row(engine, rid)
    assert row.status == "active" and row.last_value == 210.0
    assert "AAPL" in broker.positions

    broker.set_price("AAPL", 195.0)
    engine.evaluate_rules()
    row = _rule_row(engine, rid)
    assert row.status == "triggered"
    assert "AAPL" not in broker.positions

    # A one-shot rule never fires again.
    broker.set_price("AAPL", 190.0)
    client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [BUY_2_AAPL]})
    engine.evaluate_rules()
    assert broker.positions["AAPL"]["qty"] == 2


def test_rule_waits_for_market_open(client, engine, demo):
    acct_id, broker = demo
    broker.set_price("AAPL", 150.0)
    broker.market_open = False
    rid = client.post("/api/rules", json=_rule(acct_id)).json()["id"]
    engine.evaluate_rules()
    assert _rule_row(engine, rid).status == "active" and "AAPL" in broker.positions
    broker.market_open = True
    engine.evaluate_rules()
    assert _rule_row(engine, rid).status == "triggered" and "AAPL" not in broker.positions


def test_repeating_basket_rule_rearms_after_condition_clears(client, engine, demo):
    acct_id, broker = demo
    broker.set_price("AAPL", 100.0)
    broker.set_price("MSFT", 100.0)
    body = _rule(acct_id, metric="basket", direction="below", threshold=250,
                 basket=[{"symbol": "AAPL", "qty": 1}, {"symbol": "MSFT", "qty": 2}],  # = 300
                 legs=[{"symbol": "MSFT", "side": "buy", "amount": 1}],
                 repeat=True, cooldown_minutes=1)
    rid = client.post("/api/rules", json=body).json()["id"]
    start = broker.positions["MSFT"]["qty"]

    broker.set_price("MSFT", 70.0)  # basket = 240 -> fire
    engine.evaluate_rules()
    engine.evaluate_rules()  # still below, but already fired: must not double-buy
    assert broker.positions["MSFT"]["qty"] == start + 1
    assert _rule_row(engine, rid).armed is False

    broker.set_price("MSFT", 100.0)  # clears -> re-arms
    engine.evaluate_rules()
    assert _rule_row(engine, rid).armed is True

    broker.set_price("MSFT", 70.0)  # within cooldown -> no fire yet
    engine.evaluate_rules()
    assert broker.positions["MSFT"]["qty"] == start + 1

    with engine.sessions() as s:  # pretend the cooldown has passed
        s.get(PriceRule, rid).last_triggered_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        s.commit()
    engine.evaluate_rules()
    assert broker.positions["MSFT"]["qty"] == start + 2


def test_holdings_rule_and_dry_run(client, engine, demo):
    acct_id, broker = demo
    broker.set_price("SPY", 500.0)  # demo holds 20 SPY -> $10,000
    rid = client.post("/api/rules", json=_rule(
        acct_id, metric="holdings", basket=[{"symbol": "SPY"}], direction="above",
        threshold=9_000, legs=[{"symbol": "SPY", "side": "sell", "amount": 5}], dry_run=True,
    )).json()["id"]
    engine.evaluate_rules()
    row = _rule_row(engine, rid)
    assert row.status == "triggered" and row.last_value == 10_000
    assert broker.positions["SPY"]["qty"] == 20  # dry run: nothing sold
    with engine.sessions() as s:
        msgs = [a.message for a in s.query(ActivityLog).all()]
    assert any("DRY RUN" in m for m in msgs)


def test_rule_preview(client, demo):
    acct_id, broker = demo
    broker.set_price("AAPL", 100.0)
    broker.set_price("MSFT", 10.0)
    r = client.post("/api/rules/preview", json={"account_id": acct_id, "metric": "basket",
                                                "basket": [{"symbol": "AAPL", "qty": 2},
                                                           {"symbol": "MSFT", "qty": 3}]})
    assert r.json()["value"] == 230.0


# --------------------------------------------------------------------------- #
# Schedules
# --------------------------------------------------------------------------- #
def _future_local(engine, minutes=60) -> str:
    return (datetime.now(engine.tz) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M")


def test_one_time_schedule_runs_and_completes(client, engine, demo):
    acct_id, broker = demo
    r = client.post("/api/schedules", json={
        "account_id": acct_id, "name": "Buy AAPL", "kind": "once",
        "run_at": _future_local(engine), "legs": [BUY_2_AAPL]})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert engine.scheduler.get_job(f"schedule-{sid}") is not None
    before = broker.positions["AAPL"]["qty"]

    engine.run_schedule(sid)
    assert broker.positions["AAPL"]["qty"] == before + 2
    with engine.sessions() as s:
        assert s.get(ScheduledOrder, sid).status == "done"
    engine.run_schedule(sid)  # a finished schedule never runs twice
    assert broker.positions["AAPL"]["qty"] == before + 2


def test_schedule_time_is_interpreted_in_app_timezone(client, engine, demo):
    acct_id, _ = demo
    local = _future_local(engine, 120)
    sid = client.post("/api/schedules", json={
        "account_id": acct_id, "name": "x", "kind": "once", "run_at": local,
        "legs": [BUY_2_AAPL]}).json()["id"]
    with engine.sessions() as s:
        stored = s.get(ScheduledOrder, sid).run_at.replace(tzinfo=timezone.utc)
    expected = datetime.strptime(local, "%Y-%m-%dT%H:%M").replace(tzinfo=ZoneInfo(engine.settings.timezone))
    assert stored == expected


def test_recurring_schedule_skips_when_market_closed(client, engine, demo):
    acct_id, broker = demo
    sid = client.post("/api/schedules", json={
        "account_id": acct_id, "name": "DCA", "kind": "recurring",
        "days_of_week": ["fri", "mon"], "time_of_day": "09:45", "legs": [BUY_2_AAPL]}).json()["id"]
    with engine.sessions() as s:
        assert s.get(ScheduledOrder, sid).days_of_week == ["mon", "fri"]
    before = broker.positions["AAPL"]["qty"]
    broker.market_open = False
    engine.run_schedule(sid)
    assert broker.positions["AAPL"]["qty"] == before
    broker.market_open = True
    engine.run_schedule(sid)
    assert broker.positions["AAPL"]["qty"] == before + 2
    with engine.sessions() as s:
        assert s.get(ScheduledOrder, sid).status == "active"


def test_schedule_validation(client, engine, demo):
    acct_id, _ = demo
    past = (datetime.now(engine.tz) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    r = client.post("/api/schedules", json={"account_id": acct_id, "name": "x", "kind": "once",
                                            "run_at": past, "legs": [BUY_2_AAPL]})
    assert r.status_code == 422 and "past" in r.text
    r = client.post("/api/schedules", json={"account_id": acct_id, "name": "x", "kind": "recurring",
                                            "days_of_week": [], "time_of_day": "09:30",
                                            "legs": [BUY_2_AAPL]})
    assert r.status_code == 422


def test_missed_one_time_schedule_is_not_run_late(client, engine, demo):
    acct_id, broker = demo
    with engine.sessions() as s:
        s.add(ScheduledOrder(account_id=acct_id, name="old", kind="once", legs=[BUY_2_AAPL],
                             run_at=datetime.now(timezone.utc) - timedelta(hours=3)))
        s.commit()
    engine.load_schedules()
    with engine.sessions() as s:
        assert s.query(ScheduledOrder).filter_by(name="old").one().status == "missed"


# --------------------------------------------------------------------------- #
# Real Alpaca adapter builds valid SDK requests (no network)
# --------------------------------------------------------------------------- #
def test_alpaca_broker_builds_sdk_requests():
    broker = AlpacaBroker("PKFAKE", "secret", paper=True)
    sent = []

    def fake_submit(order_data):
        sent.append(order_data)
        return SimpleNamespace(id="1", client_order_id="c", symbol=order_data.symbol,
                               side=order_data.side, order_type=order_data.type, type=order_data.type,
                               qty=order_data.qty, notional=order_data.notional,
                               limit_price=getattr(order_data, "limit_price", None), filled_qty=0,
                               filled_avg_price=None, status="accepted",
                               time_in_force=order_data.time_in_force, submitted_at=None)

    broker._trading.submit_order = fake_submit
    out = broker.submit_order(Leg("AAPL", "buy", "dollars", 125.5))
    assert sent[-1].notional == 125.5 and sent[-1].qty is None and out["side"] == "buy"
    broker.submit_order(Leg("MSFT", "sell", "shares", 0.25))
    assert sent[-1].qty == 0.25
    out = broker.submit_order(Leg("TSLA", "buy", "shares", 3, "limit", 199.999, "gtc"))
    assert sent[-1].limit_price == 200.0 and out["type"] == "limit" and out["time_in_force"] == "gtc"
    assert broker._trading._base_url.value.startswith("https://paper-api")


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Alpaca Manager" in r.text
    assert client.get("/static/app.js").status_code == 200
