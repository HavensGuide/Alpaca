"""Tests for market data, analytics, the strategy plugin system, backtests and bots."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from alpaca_manager import analytics, indicators
from alpaca_manager.backtest import BacktestSettings, rebalance, run_backtest
from alpaca_manager.broker import AlpacaBroker
from alpaca_manager.strategies import Param, Strategy
from alpaca_manager.strategies.builtin import BUILTIN, BuyAndHold
from alpaca_manager.strategies.registry import Registry


def frame(closes, opens=None, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="B", tz="UTC")
    c = np.asarray(closes, dtype=float)
    o = np.asarray(opens if opens is not None else closes, dtype=float)
    return pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c),
                         "close": c, "volume": 1000.0}, index=idx)


# --------------------------------------------------------------------------- #
# Indicators & analytics
# --------------------------------------------------------------------------- #
def test_indicators():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert indicators.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert np.isnan(indicators.sma(s, 3).iloc[1])
    assert indicators.rsi(pd.Series(range(1, 40), dtype=float), 14).iloc[-1] == 100
    mid, up, lo = indicators.bollinger(pd.Series([2.0] * 25), 20)
    assert mid.iloc[-1] == up.iloc[-1] == lo.iloc[-1] == 2.0
    assert indicators.parse_spec("bb:20:2.5") == ("bb", 20, 2.5)
    with pytest.raises(ValueError):
        indicators.parse_spec("macd:12")
    assert indicators.warmup_bars(["sma:200", "ema:20"]) == 200


def test_round_trips_fifo_and_ignores_unmatched_sells():
    t = pd.Timestamp("2024-01-01", tz="UTC")
    fills = [
        {"t": t, "symbol": "X", "side": "sell", "qty": 5, "price": 50},  # bought before window
        {"t": t + pd.Timedelta(days=1), "symbol": "X", "side": "buy", "qty": 10, "price": 10},
        {"t": t + pd.Timedelta(days=2), "symbol": "X", "side": "buy", "qty": 10, "price": 20},
        {"t": t + pd.Timedelta(days=3), "symbol": "X", "side": "sell", "qty": 15, "price": 30},
    ]
    trades = analytics.round_trips(fills)
    assert [(tr["qty"], tr["entry_price"], tr["pnl"]) for tr in trades] == [(10, 10, 200), (5, 20, 50)]
    stats = analytics.trade_stats(trades)
    assert stats["realized_pnl"] == 250 and stats["win_rate"] == 1.0
    shorts = analytics.round_trips(fills, allow_short=True)
    assert shorts[0]["direction"] == "short"


def test_equity_stats():
    eq = pd.Series([100, 120, 90, 110], index=pd.date_range("2024-01-01", periods=4, tz="UTC"), dtype=float)
    st = analytics.equity_stats(eq)
    assert st["total_return"] == pytest.approx(0.10)
    assert st["max_drawdown"] == pytest.approx(-0.25)


# --------------------------------------------------------------------------- #
# Backtester
# --------------------------------------------------------------------------- #
def test_rebalance_sells_first_whole_shares_and_never_overspends():
    positions = {"A": 10.0}
    fills, cash = rebalance({"A": 0.0, "B": 1.0}, {"A": 10.0, "B": 33.0}, 0.0, positions,
                            fractional=False, min_trade_pct=0.01)
    assert [f["side"] for f in fills] == ["sell", "buy"]
    assert positions == {"B": 3.0} and cash == pytest.approx(1.0)
    # Tiny drift below the threshold is ignored.
    fills, _ = rebalance({"B": 0.99}, {"B": 33.0}, cash, positions, fractional=False, min_trade_pct=0.05)
    assert fills == []


class BuyOnBar(Strategy):
    """Goes all-in on bar 2 - used to prove fills happen at the NEXT bar's open."""

    def on_bar(self, ctx):
        return {"X": 1.0} if ctx.bar_index == 2 else None


def test_backtest_fills_at_next_open_without_lookahead():
    closes = [10, 10, 10, 20, 20]
    opens = [10, 10, 10, 15, 20]  # bar 3 opens at 15 - the fill price
    res = run_backtest(BuyOnBar(), {"X": frame(closes, opens)},
                       BacktestSettings(start=pd.Timestamp("2023-01-01", tz="UTC"), cash=1500,
                                        slippage_bps=0, min_trade_pct=0))
    assert res["fills"][0]["price"] == 15 and res["fills"][0]["qty"] == 100
    assert res["final_equity"] == pytest.approx(2000)  # 100 shares * 20


def test_buy_and_hold_matches_price_change_and_benchmark():
    closes = list(np.linspace(100, 150, 60))
    res = run_backtest(BuyAndHold(), {"X": frame(closes)},
                       BacktestSettings(start=pd.Timestamp("2023-01-01", tz="UTC"), cash=10_000,
                                        slippage_bps=0, fractional=True), benchmark=frame(closes))
    # Decided on bar 0's close, bought at bar 1's open; the benchmark starts at bar 0's close.
    assert res["metrics"]["total_return"] == pytest.approx(closes[-1] / closes[1] - 1)
    assert res["benchmark_metrics"]["total_return"] == pytest.approx(0.5)
    assert res["trade_stats"]["fills"] == 1


@pytest.mark.parametrize("cls", BUILTIN)
def test_every_builtin_strategy_runs(cls):
    rng = np.random.default_rng(1)
    frames = {s: frame(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 800)))) for s in ("A", "B", "C")}
    res = run_backtest(cls(), frames, BacktestSettings(start=pd.Timestamp("2023-01-01", tz="UTC")))
    assert res["bars"] > 0 and res["final_equity"] > 0


def test_strategy_errors_are_reported_with_the_bar():
    class Broken(Strategy):
        def on_bar(self, ctx):
            raise KeyError("oops")

    with pytest.raises(ValueError, match="Strategy error on bar"):
        run_backtest(Broken(), {"X": frame([1, 2, 3])},
                     BacktestSettings(start=pd.Timestamp("2023-01-01", tz="UTC")))


def test_param_coercion():
    p = Param(20, min=2, max=50)
    assert p.type == "int" and p.coerce("w", "30") == 30 and p.coerce("w", "") == 20
    with pytest.raises(ValueError, match="at least"):
        p.coerce("w", 1)
    assert Param(True).coerce("b", "false") is False
    with pytest.raises(ValueError):
        Param("sma", choices=["sma", "ema"]).coerce("k", "wma")


# --------------------------------------------------------------------------- #
# Plugin registry
# --------------------------------------------------------------------------- #
def test_registry_loads_plugins_and_reports_broken_ones(tmp_path):
    (tmp_path / "good.py").write_text(
        "from alpaca_manager.strategies import Strategy, Param\n"
        "class Mine(Strategy):\n"
        "    key = 'mine'\n    name = 'Mine'\n    params = {'n': Param(3)}\n"
        "    def on_bar(self, ctx):\n        return None\n")
    (tmp_path / "bad.py").write_text("import does_not_exist\n")
    (tmp_path / "dupe.py").write_text(
        "from alpaca_manager.strategies import Strategy\n"
        "class Again(Strategy):\n    key = 'buy_and_hold'\n    def on_bar(self, ctx):\n        return None\n")
    reg = Registry(tmp_path)
    keys = {s["key"]: s["source"] for s in reg.describe()["strategies"]}
    assert keys["mine"] == "good.py" and keys["buy_and_hold"] == "built-in"
    errors = {e["source"]: e["error"] for e in reg.errors}
    assert "ModuleNotFoundError" in errors["bad.py"] and "Duplicate" in errors["dupe.py"]
    assert reg.build("mine", {"n": "7"}).p == {"n": 7}
    assert (tmp_path / "example_strategy.py.txt").exists()  # template, not auto-loaded


# --------------------------------------------------------------------------- #
# API (demo broker)
# --------------------------------------------------------------------------- #
def test_market_series_symbol_basket_and_indicators(client, demo):
    acct_id, _ = demo
    r = client.post("/api/market/series", json={"account_id": acct_id, "range": "1Y",
                                                "indicators": ["sma:50", "bb:20"],
                                                "items": [{"kind": "symbol", "symbol": "aapl"}]})
    assert r.status_code == 200, r.text
    s = r.json()["series"][0]
    assert s["label"] == "AAPL" and len(s["candles"]) > 200
    # Warm-up history is fetched, so the overlay covers the whole visible range.
    assert len(s["indicators"]["SMA 50"]) == len(s["candles"])
    assert set(s["indicators"]) == {"SMA 50", "BB 20 mid", "BB 20 upper", "BB 20 lower"}

    b = client.post("/api/baskets", json={"name": "Duo", "items": [{"symbol": "AAPL", "qty": 2},
                                                                   {"symbol": "MSFT", "qty": 1}]}).json()
    r = client.post("/api/market/series", json={"account_id": acct_id, "range": "3M",
                                                "items": [{"kind": "basket", "basket_id": b["id"]}]})
    s = r.json()["series"][0]
    assert s["kind"] == "basket" and s["candles"] is None
    q = client.get(f"/api/accounts/{acct_id}/quotes?symbols=AAPL,MSFT").json()
    assert s["stats"]["last"] == pytest.approx(2 * q["AAPL"] + q["MSFT"], rel=0.01)

    r = client.post("/api/market/series", json={"account_id": acct_id, "range": "5Y", "interval": "5Min",
                                                "items": [{"kind": "symbol", "symbol": "SPY"}]})
    assert r.status_code == 422 and "too much data" in r.text


def test_watchlist_news_and_snapshots(client, demo):
    acct_id, _ = demo
    assert "SPY" in client.get("/api/watchlist").json()["symbols"]
    assert client.put("/api/watchlist", json={"symbols": ["tsla", "TSLA", "amd"]}).json()["symbols"] == ["TSLA", "AMD"]
    assert client.put("/api/watchlist", json={"symbols": ["bad symbol"]}).status_code == 422
    snaps = client.get(f"/api/market/snapshots?account_id={acct_id}&symbols=TSLA,AMD").json()
    assert {x["symbol"] for x in snaps} == {"TSLA", "AMD"} and snaps[0]["price"] > 0
    news = client.get(f"/api/market/news?account_id={acct_id}&symbols=TSLA&limit=3").json()
    assert len(news) == 3 and news[0]["simulated"]


def test_performance_history_and_trades(client, demo):
    acct_id, broker = demo
    broker.set_price("NVDA", 100.0)
    client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [{"symbol": "NVDA", "side": "buy", "amount": 10}]})
    broker.set_price("NVDA", 110.0)
    client.post(f"/api/accounts/{acct_id}/orders", json={"legs": [{"symbol": "NVDA", "side": "sell", "amount": 10}]})
    t = client.get(f"/api/accounts/{acct_id}/trades").json()
    assert t["stats"]["trades"] == 1 and t["stats"]["realized_pnl"] == pytest.approx(100)
    h = client.get(f"/api/accounts/{acct_id}/history?period=3M&benchmark=SPY").json()
    assert len(h["points"]) > 30 and h["benchmark_symbol"] == "SPY"
    assert h["benchmark"][0]["v"] == pytest.approx(h["points"][0]["equity"], rel=1e-3)


def test_backtest_api_and_history(client, demo):
    acct_id, _ = demo
    r = client.post("/api/backtests", json={"account_id": acct_id, "strategy_key": "sma_crossover",
                                            "params": {"fast": 10, "slow": 30},
                                            "symbols": ["aapl", "MSFT"], "start": "2024-01-01"})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["params"] == {"fast": 10, "slow": 30, "kind": "sma"}
    assert run["result"]["benchmark_symbol"] == "SPY" and run["result"]["equity"]
    assert client.get("/api/backtests").json()[0]["id"] == run["id"]
    bad = client.post("/api/backtests", json={"account_id": acct_id, "strategy_key": "sma_crossover",
                                              "params": {"fast": 1}, "symbols": ["AAPL"], "start": "2024-01-01"})
    assert bad.status_code == 422 and "fast" in bad.text
    assert client.post("/api/backtests", json={"account_id": acct_id, "strategy_key": "nope",
                                               "symbols": ["AAPL"], "start": "2024-01-01"}).status_code == 422


def test_bot_lifecycle(client, engine, demo):
    acct_id, broker = demo
    r = client.post("/api/bots", json={"account_id": acct_id, "name": "B", "strategy_key": "buy_and_hold",
                                       "symbols": ["AMD", "NFLX"], "allocation": 10_000})
    assert r.status_code == 201, r.text
    bot = r.json()
    assert engine.scheduler.get_job(f"bot-{bot['id']}") is not None
    res = client.post(f"/api/bots/{bot['id']}/run").json()
    assert res["orders"] == 2
    detail = client.get(f"/api/bots/{bot['id']}").json()
    px = client.get(f"/api/accounts/{acct_id}/quotes?symbols=AMD,NFLX").json()
    assert detail["positions"]["AMD"] == int(5000 // px["AMD"]) or detail["positions"]["AMD"] > 0
    assert broker.positions["AMD"]["qty"] == detail["positions"]["AMD"]
    assert detail["cash"] >= 0 and len(detail["trades"]) == 2
    # Buy and hold doesn't trade again.
    assert client.post(f"/api/bots/{bot['id']}/run").json()["orders"] == 0
    client.patch(f"/api/bots/{bot['id']}/status", json={"status": "paused"})
    assert engine.scheduler.get_job(f"bot-{bot['id']}") is None
    assert client.delete(f"/api/bots/{bot['id']}").status_code == 204


def test_bots_refuse_live_accounts_and_over_allocation(client, demo):
    acct_id, _ = demo
    r = client.post("/api/bots", json={"account_id": acct_id, "name": "B", "strategy_key": "buy_and_hold",
                                       "symbols": ["AMD"], "allocation": 10_000_000})
    assert r.status_code == 422 and "buying power" in r.text
    live = client.post("/api/accounts", json={"name": "L", "mode": "live", "api_key": "AKXXXXXXXX",
                                              "secret_key": "s"}).json()
    r = client.post("/api/bots", json={"account_id": live["id"], "name": "B", "strategy_key": "buy_and_hold",
                                       "symbols": ["AMD"], "allocation": 100})
    assert r.status_code == 422 and "paper or demo" in r.text


# --------------------------------------------------------------------------- #
# Alpaca adapter: market-data mapping (no network)
# --------------------------------------------------------------------------- #
def test_alpaca_market_data_mapping():
    broker = AlpacaBroker("PKFAKE", "secret", paper=True)
    ts = datetime(2026, 1, 5, 5, tzinfo=timezone.utc)
    bar = SimpleNamespace(timestamp=ts, open=1.0, high=2.0, low=0.5, close=1.5, volume=100.0)
    seen = {}

    def fake_bars(req):
        seen["req"] = req
        return SimpleNamespace(data={"AAPL": [bar]})

    broker._data.get_stock_bars = fake_bars
    out = broker.get_bars(["AAPL", "MSFT"], "15Min", ts - timedelta(days=1))
    assert out["AAPL"]["close"].iloc[0] == 1.5 and out["MSFT"].empty
    assert seen["req"].timeframe.amount_value == 15 and seen["req"].feed.value == "iex"

    snap = SimpleNamespace(latest_trade=SimpleNamespace(price=10.0),
                           daily_bar=SimpleNamespace(open=9, high=11, low=8, volume=5, close=10),
                           previous_daily_bar=SimpleNamespace(close=8.0))
    broker._data.get_stock_snapshot = lambda req: {"AAPL": snap}
    assert broker.get_snapshots(["AAPL"])["AAPL"] == {"price": 10.0, "prev_close": 8.0, "open": 9.0,
                                                      "high": 11.0, "low": 8.0, "volume": 5.0}

    article = SimpleNamespace(id=1, headline="H", summary="S", source="benzinga", author="A",
                              url="https://x", created_at=ts, symbols=["AAPL"],
                              images=[SimpleNamespace(size="thumb", url="https://img")])
    broker._news = SimpleNamespace(get_news=lambda req: SimpleNamespace(data={"news": [article]}))
    n = broker.get_news(["AAPL"], 5)[0]
    assert n["headline"] == "H" and n["image"] == "https://img" and n["source"] == "benzinga"

    hist = SimpleNamespace(timestamp=[1, 2, 3], equity=[0, 100.0, 110.0], profit_loss=[0, 0, 10])
    broker._trading.get_portfolio_history = lambda history_filter=None, **kw: hist
    assert broker.get_portfolio_history("1M") == [{"t": 2, "equity": 100.0, "pl": 0.0},
                                                  {"t": 3, "equity": 110.0, "pl": 10.0}]
