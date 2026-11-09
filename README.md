# Alpaca Manager

A friendly local web app for managing one or more [Alpaca](https://alpaca.markets) brokerage
accounts: a trading dashboard, market charts and news, performance reports, automation for
stocks, and a plugin system for backtesting and paper-trading your own strategies.

- **Dashboard**: portfolio value, today's change, cash and buying power; a portfolio-value chart
  (1D to all time); news for your holdings; positions and orders with one-click buy, sell,
  sell-all and cancel.
- **Markets**: candlestick or line charts for any stock, an index (via ETFs such as SPY, QQQ,
  DIA and IWM), or a **basket** you define.
  - **Rolling averages and indicators**: SMA 20/50/200, EMA 20, Bollinger bands, any custom
    SMA/EMA/Bollinger window, a volume band, and an RSI panel.
  - **Ranges and bar sizes**: ranges from 1D to 5Y, with bars from 5 minutes to 1 week.
  - **Compare** up to six series as % change from the start of the range, with volatility and
    drawdown for each.
  - A **watchlist** with live prices, and **news** from Alpaca's news API for whatever you're
    viewing.
  - A data table for every chart.
- **Performance**: the account's value against a benchmark (SPY by default), with return,
  excess return, max drawdown, Sharpe and volatility. Closed trades are matched first-in,
  first-out to show realized P&L, win rate, profit factor, P&L by symbol and recent round trips.
- **Strategies (plugins)**:
  - **Backtest** any strategy on historical bars. You get return vs a buy-and-hold benchmark,
    CAGR, Sharpe, Sortino, max drawdown, an equity curve, a drawdown chart, P&L by symbol, and
    a trade list. Every run is saved for comparison.
  - **Paper bots** run the same strategy on a schedule against a paper or demo account, with
    their own money allocation, and track value against the benchmark.
  - **Write your own strategy** as a small Python class. See
    [Writing a strategy plugin](#writing-a-strategy-plugin).
- **Trade**: place market or limit orders in shares or dollars. Several stocks can go in at once
  as a basket order, with a review step first.
- **Scheduled orders**: buy or sell at a set time, either once ("sell 5 TSLA on Friday at 3:30 PM")
  or on repeat ("buy $100 of VTI every weekday at 10:00").
- **Price rules (floors and targets)**: when a value rises above or falls below a threshold,
  place orders. The value can be:
  - **one stock's price**: "sell all my AAPL if it falls below $200"
  - **a basket's value**, Σ shares × price over stocks you list: "if 10 AAPL + 5 MSFT is worth
    under $4,000, buy the basket"
  - **the value of your holdings** in a set of stocks: "if my AAPL + MSFT is worth over $20,000,
    sell 10% of it"

  Rules fire once by default. If you choose **repeat**, a rule re-arms after the condition clears
  and waits for its cooldown before firing again.
- **Multiple accounts**: paper, live and a simulated **demo** account. Switch between them from
  the header. Live accounts show a red badge and an extra warning before orders.
- **Activity log**: a record of every order, trigger, skip and error.
- **Safety options**: every schedule and rule can be limited to market hours, or set to
  **dry run**, which logs what would happen without trading.

## Quick start

Requires Python 3.10+.

```bash
pip install -e .
```

```bash
alpaca-manager --demo
```

This opens <http://localhost:8765> with a simulated demo account, so you can try everything
without API keys. When you're ready, open **Accounts → Add account** and paste your Alpaca
**paper** keys. To find them, go to the Alpaca dashboard, choose Paper Trading, then API Keys.
Moving to a live account is a deliberate extra step.

Options:

| Flag / env var | Default | Meaning |
|---|---|---|
| `--port` | `8765` | Port to serve on |
| `--host` | `127.0.0.1` | Interface to bind. The app has no login, so keep it local |
| `--data-dir` / `ALPACA_MANAGER_DATA_DIR` | `~/.alpaca_manager` | Database and key location |
| `--demo` / `ALPACA_MANAGER_DEMO=1` | off | Create a demo account on first start |
| `--no-browser` | off | Don't open a browser tab |
| `ALPACA_MANAGER_POLL_SECONDS` | `15` | How often price rules are checked |
| `ALPACA_MANAGER_TIMEZONE` | `America/New_York` | Timezone used for scheduled times |
| `ALPACA_MANAGER_KEY` | auto-generated | Fernet key used to encrypt API secrets |

## How it works

```
alpaca_manager/
  cli.py      entry point (alpaca-manager / python -m alpaca_manager)
  app.py      FastAPI JSON API + serves the UI
  engine.py   order execution, APScheduler jobs, price-rule evaluation
  broker.py   AlpacaBroker (alpaca-py SDK) and DemoBroker (in-memory simulation)
  db.py       SQLite models: accounts, schedules, rules, activity log
  schemas.py  input validation (order rules such as "dollar orders must be market/day")
  vault.py    encrypts API secrets at rest
  marketdata.py  chart ranges, caching, indicators, baskets, statistics
  indicators.py  SMA / EMA / RSI / Bollinger (pandas)
  analytics.py   FIFO round trips, return/risk statistics
  backtest.py    bar-by-bar backtester (shared rebalance logic with the bots)
  bots.py        scheduled paper-trading bots with their own ledgers
  api_extra.py   API for markets, baskets, performance, strategies, backtests, bots
  strategies/    plugin API (base.py), built-ins, and plugin discovery (registry.py)
  static/     the single-page UI (plain HTML/CSS/JS, no build step)
  static/vendor/ TradingView Lightweight Charts 4.2.3 (Apache-2.0, licence alongside)
```

- **Automation only runs while the app is running.** If the app was off when a one-time schedule
  was due, the schedule is marked **missed** rather than trading late. Recurring schedules carry
  on from their next time.
- Price rules are checked by polling the latest trade price, every 15 seconds by default.
  Free Alpaca plans get **IEX** data. If you have a paid subscription, choose **SIP** on the
  account for consolidated prices.
- "Market hours only" uses Alpaca's market clock. A rule whose condition is met while the market
  is closed stays armed and fires at the next open. A schedule due while the market is closed
  is skipped. Untick the option to send orders anyway; Alpaca queues day orders for the next open.
- Each order in a schedule or rule is submitted separately. If one fails (say, not enough
  buying power), the others still go through, and the failure shows in the Activity log.
- Demo accounts are kept in memory. Their simulated positions reset when the app restarts.

## Strategies, backtests and paper bots

Five strategies are built in: **buy and hold**, a **moving-average crossover** (SMA or EMA),
**RSI mean reversion**, **momentum rotation** (hold the top-K by lookback return), and
**Bollinger band reversion**.

**Backtests** use Alpaca's split- and dividend-adjusted historical bars, with daily, hourly or
weekly bars. They work like this:

- A strategy decides on a bar's close, and its orders fill at the **next bar's open**, so there
  is no look-ahead.
- Fills include slippage, and you can set a commission in basis points.
- Positions are long-only and in whole shares.
- Rebalances smaller than 1% of equity are skipped.

**Paper bots** run once a day (at a time you choose), every hour, or every 15 minutes, only
while the market is open. Each bot keeps its own ledger of cash and shares, so its results can
be measured separately from anything else in the account. Bots place real orders on the paper
account, so the orders show up there too.

For safety, bots refuse to run on live-money accounts. Deleting a bot does not sell what it
bought.

A backtest is a model, not a promise. Real fills, gaps, halts and data differences mean a bot
will not match its backtest exactly.

### Writing a strategy plugin

Drop a `.py` file into `<data-dir>/plugins/` (by default `~/.alpaca_manager/plugins/`). Then
open **Strategies → Plugins → Reload plugins**. The page also shows a template you can copy.

```python
from alpaca_manager.strategies import Param, Strategy, indicators


class PriceAboveAverage(Strategy):
    key = "price_above_average"            # unique id (keep it stable)
    name = "Price above average"
    description = "Hold each symbol while its close is above its N-bar moving average."
    params = {"window": Param(100, min=2, max=500, help="Moving-average window (bars)")}

    def warmup(self):                      # bars of history needed first
        return self.p["window"]

    def on_bar(self, ctx):                 # called once per bar
        weights = {}
        for sym in ctx.symbols:
            close = ctx.history(sym, self.p["window"])["close"]
            avg = indicators.sma(close, self.p["window"]).iloc[-1]
            weights[sym] = 1 / len(ctx.symbols) if close.iloc[-1] > avg else 0.0
        return weights                     # target fraction of equity per symbol
```

- **`on_bar(ctx)`** returns **target weights**: `{symbol: fraction of equity}`. Omit a symbol
  to leave it as it is, and return `None` to change nothing. Weights are long-only, and any total
  over 1 is scaled down.
- **`ctx`** gives you:
  - `history(symbol, bars)`: an OHLCV DataFrame up to the current bar
  - `closes(bars)`: closing prices of every symbol
  - `close(symbol)`, `position(symbol)` and `weight(symbol)`
  - `cash`, `equity`, `time` and `bar_index`
  - `state`: a dict that persists between bars, and between runs for bots
- **`params`** become form fields in the UI. Their values reach the strategy as `self.p[...]`.
- **`precompute(frames)`** is optional. It computes indicators once for the whole history,
  which makes backtests much faster; read a value with `series.iat[ctx.bar_index]`. Use only
  rolling calculations here, so each value depends only on past bars.
- `alpaca_manager.indicators` has `sma`, `ema`, `rsi`, `bollinger` and `returns`.
- Installed packages can also provide strategies through an `alpaca_manager.strategies`
  entry point.

A plugin that fails to import is listed with its error instead of breaking the app. Plugins
are ordinary Python that runs on your computer, so only add code you trust.

## Market data notes

- Charts, backtests and bots use the account's data feed. **IEX** is free and covers part of
  US volume, so its intraday bars can be thin. **SIP** needs a paid subscription and gives
  consolidated data.
- "Indexes" are shown through ETFs that track them: SPY (S&P 500), QQQ (Nasdaq-100),
  DIA (Dow 30), IWM (Russell 2000) and VTI (total market). Alpaca doesn't serve index values
  directly.
- Intraday charts show regular-session bars (9:30-16:00 New York time). Every time on a chart
  is New York time.
- Demo accounts use **simulated** prices, which are repeatable and correlated with a simulated
  market, and **simulated** headlines. The UI labels both.
- Trade analysis uses the most recent 500 closed orders, which is the most Alpaca returns at once.

## Security

- API secrets are encrypted with Fernet before they are stored. The key is in
  `<data-dir>/secret.key` (mode 600) unless you set `ALPACA_MANAGER_KEY`. Secrets are never sent
  back to the browser.
- The server binds to `127.0.0.1` and has **no login**. Don't expose it to a network you don't
  fully trust.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

The tests use the demo broker and a mocked Alpaca SDK client, so they need no network or keys.

Charts are drawn with [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts),
which is bundled in `static/vendor/` under the Apache-2.0 licence.

> This software places real trades when connected to a live account. Test schedules and rules
> on a paper account (and with **dry run**) first. Nothing here is financial advice.
