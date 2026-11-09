"""API routes for the dashboard: market data, baskets, performance, strategies, backtests, bots."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics
from .backtest import BacktestSettings, run_backtest
from .bots import BOT_TIMEFRAMES, BotRunner
from .broker import NY, BrokerError, HISTORY_PERIODS
from .db import Account, BacktestRun, Basket, BotSnapshot, Setting, StrategyBot, as_utc
from .marketdata import MarketData, estimate_bars, warmup_days
from .schemas import _symbol
from .strategies.registry import PLUGIN_TEMPLATE, Registry

DEFAULT_WATCHLIST = ["SPY", "QQQ", "DIA", "IWM", "AAPL", "MSFT", "NVDA", "AMZN"]


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class SymbolList(BaseModel):
    symbols: list[str] = Field(max_length=100)

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v):
        out = []
        for s in v:
            s = _symbol(s)
            if s not in out:
                out.append(s)
        return out


class BasketItemIn(BaseModel):
    symbol: str
    qty: float = Field(gt=0)

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v):
        return _symbol(v)


class BasketIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    items: list[BasketItemIn] = Field(min_length=1, max_length=50)


class SeriesItem(BaseModel):
    kind: Literal["symbol", "basket"]
    symbol: str | None = None
    basket_id: int | None = None


class SeriesIn(BaseModel):
    account_id: int
    range: str = "6M"
    interval: str | None = None
    indicators: list[str] = Field(default=[], max_length=10)
    items: list[SeriesItem] = Field(min_length=1, max_length=8)


class BacktestIn(BaseModel):
    account_id: int
    strategy_key: str
    params: dict = {}
    symbols: list[str] = Field(min_length=1, max_length=30)
    start: date
    end: date | None = None
    timeframe: Literal["1Day", "1Hour", "1Week"] = "1Day"
    cash: float = Field(default=10_000, gt=0, le=1e9)
    commission_bps: float = Field(default=0, ge=0, le=500)
    slippage_bps: float = Field(default=5, ge=0, le=500)
    benchmark: str = "SPY"

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v):
        return list(dict.fromkeys(_symbol(s) for s in v))

    @field_validator("benchmark")
    @classmethod
    def _bench(cls, v):
        return _symbol(v)


class BotIn(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=200)
    strategy_key: str
    params: dict = {}
    symbols: list[str] = Field(min_length=1, max_length=20)
    timeframe: Literal["1Day", "1Hour", "15Min"] = "1Day"
    run_time: str = "15:45"
    allocation: float = Field(gt=0, le=1e9)
    benchmark: str = "SPY"

    @field_validator("symbols")
    @classmethod
    def _syms(cls, v):
        return list(dict.fromkeys(_symbol(s) for s in v))

    @field_validator("benchmark")
    @classmethod
    def _bench(cls, v):
        return _symbol(v)

    @field_validator("run_time")
    @classmethod
    def _time(cls, v):
        try:
            t = time.fromisoformat(v)
        except ValueError:
            raise ValueError("Run time must be HH:MM") from None
        if not (time(9, 31) <= t <= time(15, 58)):
            raise ValueError("Run time must be during market hours (09:31-15:58 New York time)")
        return v


class StatusIn(BaseModel):
    status: Literal["active", "paused"]


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def register(app: FastAPI, *, engine, db, get_account, broker_call, local_iso,
             registry: Registry, market: MarketData, bots: BotRunner) -> None:

    def broker_of(s: Session, account_id: int):
        acct = get_account(s, account_id)
        return acct, broker_call(engine.broker_for, acct)

    # ---------------------------- market ---------------------------------- #
    @app.post("/api/market/series")
    def market_series(body: SeriesIn, s: Session = Depends(db)):
        acct, broker = broker_of(s, body.account_id)
        items = []
        for it in body.items:
            if it.kind == "symbol":
                if not it.symbol:
                    raise HTTPException(422, "symbol is required")
                items.append({"kind": "symbol", "symbol": _symbol(it.symbol)})
            else:
                b = s.get(Basket, it.basket_id or 0)
                if b is None:
                    raise HTTPException(404, "Basket not found")
                items.append({"kind": "basket", "label": b.name, "basket_id": b.id, "items": b.items})
        try:
            return market.series(acct.id, broker, items, body.range, body.interval, body.indicators)
        except (BrokerError, ValueError) as exc:
            raise HTTPException(502 if isinstance(exc, BrokerError) else 422, str(exc)) from exc

    @app.get("/api/market/snapshots")
    def market_snapshots(account_id: int, symbols: str, s: Session = Depends(db)):
        acct, broker = broker_of(s, account_id)
        syms = [_safe_symbol(x) for x in symbols.split(",") if x.strip()][:100]
        snaps = broker_call(market.snapshots, acct.id, broker, [x for x in syms if x])
        out = []
        for sym in syms:
            sn = snaps.get(sym)
            if not sn:
                out.append({"symbol": sym, "price": None})
                continue
            prev = sn.get("prev_close")
            chg = (sn["price"] - prev) if (sn.get("price") and prev) else None
            out.append(dict(sn, symbol=sym, change=chg, change_pct=(chg / prev) if chg is not None else None))
        return out

    @app.get("/api/market/news")
    def market_news(account_id: int, symbols: str = "", limit: int = 20, s: Session = Depends(db)):
        acct, broker = broker_of(s, account_id)
        syms = [x for x in (_safe_symbol(v) for v in symbols.split(",")) if x][:30]
        return broker_call(market.news, acct.id, broker, syms, max(1, min(limit, 50)))

    @app.get("/api/watchlist")
    def get_watchlist(s: Session = Depends(db)):
        row = s.get(Setting, "watchlist")
        return {"symbols": row.value if row else DEFAULT_WATCHLIST}

    @app.put("/api/watchlist")
    def put_watchlist(body: SymbolList, s: Session = Depends(db)):
        row = s.get(Setting, "watchlist") or Setting(key="watchlist")
        row.value = body.symbols
        s.add(row)
        s.commit()
        return {"symbols": body.symbols}

    # ---------------------------- baskets --------------------------------- #
    def basket_out(b: Basket) -> dict:
        return {"id": b.id, "name": b.name, "items": b.items}

    @app.get("/api/baskets")
    def list_baskets(s: Session = Depends(db)):
        return [basket_out(b) for b in s.scalars(select(Basket).order_by(Basket.name))]

    @app.post("/api/baskets", status_code=201)
    def create_basket(body: BasketIn, s: Session = Depends(db)):
        b = Basket(name=body.name, items=[i.model_dump() for i in body.items])
        s.add(b)
        s.commit()
        return basket_out(b)

    @app.put("/api/baskets/{bid}")
    def update_basket(bid: int, body: BasketIn, s: Session = Depends(db)):
        b = s.get(Basket, bid)
        if b is None:
            raise HTTPException(404, "Basket not found")
        b.name, b.items = body.name, [i.model_dump() for i in body.items]
        s.commit()
        return basket_out(b)

    @app.delete("/api/baskets/{bid}", status_code=204)
    def delete_basket(bid: int, s: Session = Depends(db)):
        b = s.get(Basket, bid)
        if b:
            s.delete(b)
            s.commit()

    # -------------------------- performance ------------------------------- #
    @app.get("/api/accounts/{account_id}/history")
    def portfolio_history(account_id: int, period: str = "1M", benchmark: str = "SPY",
                          s: Session = Depends(db)):
        if period not in HISTORY_PERIODS:
            raise HTTPException(422, f"period must be one of {', '.join(HISTORY_PERIODS)}")
        acct, broker = broker_of(s, account_id)
        points = broker_call(broker.get_portfolio_history, period)
        out = {"period": period, "points": points, "benchmark": None, "benchmark_symbol": None,
               "stats": None}
        if len(points) < 2:
            return out
        tf = HISTORY_PERIODS[period][2]
        eq = pd.Series({pd.Timestamp(p["t"], unit="s", tz="UTC"): p["equity"] for p in points})
        out["stats"] = analytics.equity_stats(eq, tf)
        if benchmark:
            sym = _safe_symbol(benchmark)
            try:
                frames = market.bars(acct.id, broker, [sym], tf, eq.index[0].to_pydatetime() - timedelta(days=5))
                b = frames[sym]["close"].reindex(eq.index, method="ffill").dropna()
                if len(b) > 1:
                    out["benchmark"] = analytics.to_points(b / b.iloc[0] * eq.loc[b.index[0]], 2)
                    out["benchmark_symbol"] = sym
            except (BrokerError, ValueError, KeyError):
                pass  # benchmark is optional
        return out

    @app.get("/api/accounts/{account_id}/trades")
    def trade_performance(account_id: int, s: Session = Depends(db)):
        _, broker = broker_of(s, account_id)
        orders = broker_call(broker.get_orders, "closed", 500)
        fills = []
        for o in orders:
            if not o.get("filled_qty") or not o.get("filled_avg_price"):
                continue
            t = o.get("filled_at") or o.get("submitted_at")
            fills.append({"t": pd.Timestamp(t), "symbol": o["symbol"], "side": o["side"],
                          "qty": float(o["filled_qty"]), "price": float(o["filled_avg_price"])})
        trades = analytics.round_trips(fills)
        return {
            "stats": analytics.trade_stats(trades),
            "by_symbol": analytics.pnl_by_symbol(trades),
            "trades": [dict(t, entry_time=t["entry_time"].isoformat(), exit_time=t["exit_time"].isoformat())
                       for t in sorted(trades, key=lambda x: x["exit_time"], reverse=True)][:200],
            "fills": len(fills),
            "note": "Based on your most recent 500 closed orders." if len(orders) >= 500 else None,
        }

    # --------------------------- strategies ------------------------------- #
    @app.get("/api/strategies")
    def list_strategies():
        return registry.describe()

    @app.post("/api/strategies/reload")
    def reload_strategies():
        registry.reload()
        return registry.describe()

    @app.get("/api/strategies/template")
    def strategy_template():
        return {"filename": "my_strategy.py", "code": PLUGIN_TEMPLATE}

    examples_dir = Path(__file__).parent / "plugin_examples"

    def example_info(path: Path) -> dict:
        doc = path.read_text(encoding="utf-8").split('"""')
        lines = doc[1].strip().splitlines() if len(doc) > 2 else [path.stem]
        return {"file": path.name, "title": lines[0].removeprefix("Example plugin: ").rstrip("."),
                "details": " ".join(x.strip() for x in lines[1:4] if x.strip()),
                "installed": (registry.plugin_dir / path.name).exists()}

    @app.get("/api/strategies/examples")
    def list_examples():
        return [example_info(p) for p in sorted(examples_dir.glob("*.py"))]

    @app.post("/api/strategies/examples/{name}")
    def install_example(name: str):
        src = examples_dir / name
        if src.parent != examples_dir or src.suffix != ".py" or not src.exists():
            raise HTTPException(404, "No such example")
        dest = registry.plugin_dir / name
        if dest.exists():
            raise HTTPException(409, f"{name} is already in your plugins folder - edit or delete it there")
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        registry.reload()
        return dict(registry.describe(), installed=str(dest))

    def build_strategy(key: str, params: dict):
        try:
            return registry.build(key, params)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    # --------------------------- backtests -------------------------------- #
    @app.post("/api/backtests")
    def create_backtest(body: BacktestIn, s: Session = Depends(db)):
        acct, broker = broker_of(s, body.account_id)
        strategy = build_strategy(body.strategy_key, body.params)
        start = datetime.combine(body.start, time(0), NY).astimezone(timezone.utc)
        end = datetime.combine(body.end, time(23, 59), NY).astimezone(timezone.utc) if body.end else None
        if end and end <= start:
            raise HTTPException(422, "The end date must be after the start date")
        days = ((end or datetime.now(timezone.utc)) - start).days + 1
        if estimate_bars(body.timeframe, days) > 60_000:
            raise HTTPException(422, "That period has too many bars - use daily bars or a shorter period")
        fetch_start = start - timedelta(days=warmup_days(body.timeframe, int(strategy.warmup()) + 5))
        symbols = body.symbols + ([body.benchmark] if body.benchmark not in body.symbols else [])
        frames = broker_call(market.bars, acct.id, broker, symbols, body.timeframe, fetch_start, end)
        bench = frames.get(body.benchmark)
        settings = BacktestSettings(start=pd.Timestamp(start), end=pd.Timestamp(end) if end else None,
                                    timeframe=body.timeframe, cash=body.cash,
                                    commission_bps=body.commission_bps, slippage_bps=body.slippage_bps)
        empty = [sym for sym in body.symbols if frames[sym].empty]
        if empty:
            raise HTTPException(422, f"No price data for {', '.join(empty)} in this period")
        try:
            result = run_backtest(strategy, {sym: frames[sym] for sym in body.symbols}, settings, bench)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result["benchmark_symbol"] = body.benchmark
        result["data_source"] = acct.mode
        run = BacktestRun(strategy_key=body.strategy_key, strategy_name=type(strategy).name,
                          params=strategy.p, symbols=body.symbols,
                          settings=body.model_dump(mode="json", exclude={"params", "symbols", "strategy_key"}),
                          summary={"total_return": result["metrics"]["total_return"],
                                   "max_drawdown": result["metrics"]["max_drawdown"],
                                   "sharpe": result["metrics"]["sharpe"],
                                   "benchmark_return": (result.get("benchmark_metrics") or {}).get("total_return"),
                                   "trades": result["trade_stats"]["trades"]},
                          result=result)
        s.add(run)
        s.commit()
        return backtest_out(run, full=True)

    def backtest_out(r: BacktestRun, full: bool = False) -> dict:
        out = {"id": r.id, "created_at": local_iso(r.created_at), "strategy_key": r.strategy_key,
               "strategy_name": r.strategy_name, "params": r.params, "symbols": r.symbols,
               "settings": r.settings, "summary": r.summary}
        if full:
            out["result"] = r.result
        return out

    @app.get("/api/backtests")
    def list_backtests(s: Session = Depends(db)):
        rows = s.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(50))
        return [backtest_out(r) for r in rows]

    @app.get("/api/backtests/{rid}")
    def get_backtest(rid: int, s: Session = Depends(db)):
        r = s.get(BacktestRun, rid)
        if r is None:
            raise HTTPException(404, "Backtest not found")
        return backtest_out(r, full=True)

    @app.delete("/api/backtests/{rid}", status_code=204)
    def delete_backtest(rid: int, s: Session = Depends(db)):
        r = s.get(BacktestRun, rid)
        if r:
            s.delete(r)
            s.commit()

    # ------------------------------ bots ---------------------------------- #
    def bot_out(b: StrategyBot, detail: bool = False) -> dict:
        snaps = b.snapshots
        first = snaps[0] if snaps else None
        last = snaps[-1] if snaps else None
        equity = last.equity if last else b.allocation
        bench_ret = None
        if first and last and first.benchmark and last.benchmark:
            bench_ret = last.benchmark / first.benchmark - 1
        try:
            strat_name = registry.get(b.strategy_key).name
        except ValueError:
            strat_name = f"{b.strategy_key} (missing plugin)"
        out = {
            "id": b.id, "account_id": b.account_id, "name": b.name, "strategy_key": b.strategy_key,
            "strategy_name": strat_name, "params": b.params, "symbols": b.symbols,
            "timeframe": b.timeframe, "timeframe_label": BOT_TIMEFRAMES[b.timeframe],
            "run_time": b.run_time, "benchmark": b.benchmark, "allocation": b.allocation,
            "cash": b.cash, "positions": b.positions, "status": b.status, "equity": equity,
            "return_pct": equity / b.allocation - 1 if b.allocation else None,
            "benchmark_return": bench_ret, "last_run_at": local_iso(b.last_run_at),
            "next_run_at": local_iso(bots.next_run(b.id)) if b.status == "active" else None,
            "last_error": b.last_error, "trade_count": len(b.trades),
        }
        if detail:
            eq = pd.Series({as_utc(x.at): x.equity for x in snaps}, dtype=float)
            out["equity_curve"] = [{"t": int(as_utc(x.at).timestamp()), "v": x.equity} for x in snaps]
            out["benchmark_curve"] = [
                {"t": int(as_utc(x.at).timestamp()), "v": round(x.benchmark / first.benchmark * first.equity, 2)}
                for x in snaps if x.benchmark and first and first.benchmark]
            out["metrics"] = analytics.equity_stats(eq, b.timeframe) if len(eq) > 1 else None
            out["trades"] = [{"at": local_iso(t.at), "symbol": t.symbol, "side": t.side, "qty": t.qty,
                              "price": t.price, "estimated": t.estimated} for t in reversed(b.trades)][:200]
        return out

    @app.get("/api/bots")
    def list_bots(s: Session = Depends(db)):
        return [bot_out(b) for b in s.scalars(select(StrategyBot).order_by(StrategyBot.id))]

    @app.get("/api/bots/{bid}")
    def get_bot(bid: int, s: Session = Depends(db)):
        b = s.get(StrategyBot, bid)
        if b is None:
            raise HTTPException(404, "Bot not found")
        return bot_out(b, detail=True)

    @app.post("/api/bots", status_code=201)
    def create_bot(body: BotIn, s: Session = Depends(db)):
        acct, broker = broker_of(s, body.account_id)
        if acct.mode == "live":
            raise HTTPException(422, "Strategy bots can only trade paper or demo accounts")
        strategy = build_strategy(body.strategy_key, body.params)
        info = broker_call(broker.get_account)
        if body.allocation > (info.get("buying_power") or 0):
            raise HTTPException(422, f"The allocation is more than the account's buying power "
                                     f"(${info.get('buying_power') or 0:,.2f})")
        bench = broker_call(broker.get_latest_prices, [body.benchmark]).get(body.benchmark)
        b = StrategyBot(account_id=acct.id, name=body.name, strategy_key=body.strategy_key,
                        params=strategy.p, symbols=body.symbols, timeframe=body.timeframe,
                        run_time=body.run_time, allocation=body.allocation, cash=body.allocation,
                        benchmark=body.benchmark, positions={}, state={}, benchmark_start=bench)
        s.add(b)
        s.flush()
        s.add(BotSnapshot(bot_id=b.id, equity=body.allocation, benchmark=bench))
        s.commit()
        bots.sync(b)
        engine.record(f"Started bot '{b.name}' ({type(strategy).name}) with ${b.allocation:,.2f}",
                      account_id=acct.id, source="Strategies")
        s.refresh(b)
        return bot_out(b)

    @app.patch("/api/bots/{bid}/status")
    def bot_status(bid: int, body: StatusIn, s: Session = Depends(db)):
        b = s.get(StrategyBot, bid)
        if b is None:
            raise HTTPException(404, "Bot not found")
        b.status = body.status
        s.commit()
        bots.sync(b)
        return bot_out(b)

    @app.post("/api/bots/{bid}/run")
    def run_bot_now(bid: int, s: Session = Depends(db)):
        if s.get(StrategyBot, bid) is None:
            raise HTTPException(404, "Bot not found")
        result = bots.run(bid, manual=True)
        if not result.get("ok"):
            raise HTTPException(422, result.get("message", "Bot run failed"))
        return result

    @app.delete("/api/bots/{bid}", status_code=204)
    def delete_bot(bid: int, s: Session = Depends(db)):
        b = s.get(StrategyBot, bid)
        if b:
            bots.unschedule(bid)
            s.delete(b)
            s.commit()


def _safe_symbol(v: str) -> str | None:
    try:
        return _symbol(v)
    except ValueError:
        return None
