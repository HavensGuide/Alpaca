# Alpaca Manager

A friendly local web app for managing one or more [Alpaca](https://alpaca.markets) brokerage
accounts, with automation for stocks:

- **Overview**: portfolio value, today's change, cash, buying power, positions and orders,
  with one-click buy, sell, sell-all and cancel.
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
  static/     the single-page UI (plain HTML/CSS/JS, no build step)
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

> This software places real trades when connected to a live account. Test schedules and rules
> on a paper account (and with **dry run**) first. Nothing here is financial advice.
