import pytest
from fastapi.testclient import TestClient

from alpaca_manager.app import create_app
from alpaca_manager.config import Settings


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPACA_MANAGER_KEY", raising=False)
    return create_app(Settings(data_dir=tmp_path, run_scheduler=False, poll_seconds=1))


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def engine(app):
    return app.state.engine


@pytest.fixture
def demo(client, engine):
    """A demo account whose simulated market is open and prices don't drift."""
    acct = client.post("/api/accounts", json={"name": "Demo", "mode": "demo"}).json()
    with engine.sessions() as s:
        from alpaca_manager.db import Account

        broker = engine.broker_for(s.get(Account, acct["id"]))
    broker.market_open = True
    broker.volatility = 0.0
    return acct["id"], broker
