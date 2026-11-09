/* Strategies tab: backtests, paper-trading bots and the plugin manager. */
"use strict";

let stInfo = { strategies: [], errors: [], plugin_dir: "" };
let stView = "backtest";
let btCharts = [];
let botChart = null;
let lastBacktest = null;

const unixIso = (t) => new Date(t * 1000).toISOString();
const fmtUnixDate = (t) => fmtTime(unixIso(t), { year: "numeric", hour: undefined, minute: undefined });

async function loadStrategies() {
  stInfo = await api("GET", "/api/strategies").catch((e) => { toast(e.message, true); return stInfo; });
}

/* ---------------- shared: strategy + parameter fields ---------------- */
function strategyPicker(selectedKey, values = {}) {
  const wrap = el(`<div>
    <div class="field"><label>Strategy</label><select class="st-key">${stInfo.strategies.map((s) => `<option value="${esc(s.key)}">${esc(s.name)}${s.source !== "built-in" ? " (plugin)" : ""}</option>`).join("")}</select>
      <div class="hint st-desc"></div></div>
    <div class="st-params grid2"></div></div>`);
  const sel = $(".st-key", wrap);
  if (selectedKey && stInfo.strategies.some((s) => s.key === selectedKey)) sel.value = selectedKey;
  const render = (vals) => {
    const s = stInfo.strategies.find((x) => x.key === sel.value);
    $(".st-desc", wrap).textContent = s?.description || "";
    $(".st-params", wrap).innerHTML = Object.entries(s?.params || {}).map(([name, p]) => {
      const v = vals[name] ?? p.default;
      const label = esc(name.replace(/_/g, " "));
      let input;
      if (p.type === "bool") return `<div class="field"><label class="check" style="margin-top:22px"><input type="checkbox" data-param="${esc(name)}" ${v ? "checked" : ""}> ${label}</label><div class="hint">${esc(p.help)}</div></div>`;
      if (p.type === "choice") input = `<select data-param="${esc(name)}">${p.choices.map((c) => `<option ${c === v ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
      else input = `<input data-param="${esc(name)}" type="${p.type === "str" ? "text" : "number"}" value="${esc(v)}" ${p.min != null ? `min="${p.min}"` : ""} ${p.max != null ? `max="${p.max}"` : ""} step="${p.step ?? (p.type === "int" ? 1 : "any")}">`;
      return `<div class="field"><label>${label}</label>${input}<div class="hint">${esc(p.help)}</div></div>`;
    }).join("");
  };
  sel.onchange = () => render({});
  render(values);
  wrap.getKey = () => sel.value;
  wrap.getParams = () => Object.fromEntries($$("[data-param]", wrap).map((i) => [i.dataset.param, i.type === "checkbox" ? i.checked : i.value]));
  return wrap;
}

const parseSymbols = (v) => [...new Set(String(v).toUpperCase().split(/[\s,;]+/).filter(Boolean))];

/* ---------------- backtest form ---------------- */
let btPicker = null;
function renderBacktestForm(prefill = {}) {
  const host = $("#bt-form");
  const threeY = new Date(Date.now() - 3 * 365.25 * 864e5).toISOString().slice(0, 10);
  const form = el(`<form>
    <div class="st-picker"></div>
    <div class="field"><label>Symbols</label><input name="symbols" value="${esc((prefill.symbols || ["AAPL", "MSFT", "NVDA", "AMZN"]).join(", "))}" spellcheck="false">
      <div class="hint"><a href="#" data-fill="watch">Use my watchlist</a> · <a href="#" data-fill="idx">Index ETFs</a> · <a href="#" data-fill="pos">My positions</a></div></div>
    <div class="grid2">
      <div class="field"><label>From</label><input type="date" name="start" value="${esc(prefill.start || threeY)}"></div>
      <div class="field"><label>To</label><input type="date" name="end" value="${esc(prefill.end || "")}"><div class="hint">Blank = today</div></div>
      <div class="field"><label>Bar size</label><select name="timeframe"><option value="1Day">Daily</option><option value="1Hour">Hourly</option><option value="1Week">Weekly</option></select></div>
      <div class="field"><label>Starting cash ($)</label><input type="number" name="cash" min="1" value="${esc(prefill.cash || 10000)}"></div>
      <div class="field"><label>Commission (bps)</label><input type="number" name="commission_bps" min="0" step="0.5" value="${esc(prefill.commission_bps ?? 0)}"><div class="hint">1 bp = 0.01% of each trade</div></div>
      <div class="field"><label>Slippage (bps)</label><input type="number" name="slippage_bps" min="0" step="0.5" value="${esc(prefill.slippage_bps ?? 5)}"></div>
      <div class="field"><label>Benchmark</label><input name="benchmark" value="${esc(prefill.benchmark || "SPY")}" spellcheck="false"></div>
      <div class="field"><label>Price data from</label><select name="account_id">${state.accounts.map((a) => `<option value="${a.id}" ${a.id === state.accountId ? "selected" : ""}>${esc(a.name)} (${a.mode})</option>`).join("")}</select></div>
    </div>
    <button class="btn primary wide" type="submit">Run backtest</button>
  </form>`);
  if (prefill.timeframe) form.timeframe.value = prefill.timeframe;
  btPicker = strategyPicker(prefill.strategy_key, prefill.params || {});
  $(".st-picker", form).append(btPicker);
  form.symbols.addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase(); });
  form.addEventListener("click", async (e) => {
    const f = e.target.closest("[data-fill]");
    if (!f) return;
    e.preventDefault();
    if (f.dataset.fill === "watch") form.symbols.value = (await api("GET", "/api/watchlist")).symbols.join(", ");
    else if (f.dataset.fill === "idx") form.symbols.value = "SPY, QQQ, IWM, AGG, GLD";
    else if (state.accountId) {
      const o = await api("GET", `/api/accounts/${state.accountId}/overview`).catch(() => null);
      if (o?.positions?.length) form.symbols.value = o.positions.map((p) => p.symbol).join(", ");
      else toast("No positions in this account", true);
    }
  });
  form.onsubmit = (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", form);
    guard(async () => {
      const f = new FormData(form);
      const body = {
        account_id: Number(f.get("account_id")), strategy_key: btPicker.getKey(), params: btPicker.getParams(),
        symbols: parseSymbols(f.get("symbols")), start: f.get("start"), end: f.get("end") || null,
        timeframe: f.get("timeframe"), cash: Number(f.get("cash")), commission_bps: Number(f.get("commission_bps")),
        slippage_bps: Number(f.get("slippage_bps")), benchmark: String(f.get("benchmark")).trim().toUpperCase() || "SPY",
      };
      if (!body.symbols.length) throw new Error("Enter at least one symbol");
      btn.textContent = "Running…";
      try {
        const run = await api("POST", "/api/backtests", body);
        renderBacktest(run);
        loadBacktestHistory();
      } finally { btn.textContent = "Run backtest"; }
    }, btn);
  };
  host.innerHTML = "";
  host.append(form);
}

/* ---------------- backtest results ---------------- */
function renderBacktest(run) {
  lastBacktest = run;
  btCharts.forEach((c) => c.destroy());
  btCharts = [];
  const r = run.result, m = r.metrics, bm = r.benchmark_metrics || {};
  const s = run.settings;
  const intraday = s.timeframe === "1Hour";
  const box = $("#bt-results");
  const start = r.equity[0]?.t, end = r.equity[r.equity.length - 1]?.t;
  const paramText = Object.entries(run.params).map(([k, v]) => `${k.replace(/_/g, " ")} ${v}`).join(", ");
  box.innerHTML = `<div class="card">
    <div class="card-head"><div><h2>${esc(run.strategy_name)}</h2>
      <div class="sub-line">${esc(run.symbols.join(", "))} · ${fmtUnixDate(start)} – ${fmtUnixDate(end)} · ${esc({ "1Day": "daily", "1Hour": "hourly", "1Week": "weekly" }[s.timeframe])} bars${paramText ? ` · ${esc(paramText)}` : ""}</div></div>
      <div class="row-gap"><button class="btn small" id="bt-deploy">Deploy as paper bot</button><button class="btn small ghost" id="bt-edit">Edit &amp; re-run</button></div></div>
    ${r.data_source === "demo" ? `<div class="callout warn small-note">Demo account: this backtest used simulated prices. Pick a paper account under “Price data from” for real market history.</div>` : ""}
    <div class="tiles">
      ${tile("Total return", signed(m.total_return), `${esc(r.benchmark_symbol)} ${pct(bm.total_return)}`)}
      ${tile("Annual return", signed(m.cagr), `${esc(r.benchmark_symbol)} ${pct(bm.cagr)}`, "Compound annual growth rate")}
      ${tile("Max drawdown", pctPlain(m.max_drawdown), `${esc(r.benchmark_symbol)} ${pctPlain(bm.max_drawdown)}`)}
      ${tile("Sharpe", num2(m.sharpe), `${esc(r.benchmark_symbol)} ${num2(bm.sharpe)}`, "Annualised return ÷ volatility (risk-free 0)")}
      ${tile("Win rate", pctPlain(r.trade_stats.win_rate, 0), `${r.trade_stats.trades} closed trades`)}
      ${tile("Time invested", pctPlain(r.exposure, 0), `fees ${money(r.trade_stats.fees)}`)}
    </div>
    <h3 class="h3">Equity: strategy vs ${esc(r.benchmark_symbol)} buy-and-hold</h3>
    <div id="bt-equity"></div>
    <h3 class="h3">Drawdown</h3>
    <div id="bt-dd"></div>
    <div class="pf-grid even">
      <div><h3 class="h3">Statistics</h3><div class="table-wrap">${metricsTable(m, bm, r.benchmark_symbol, r)}</div></div>
      <div><h3 class="h3">P&amp;L by symbol</h3>${pnlBars(r.by_symbol)}</div>
    </div>
    <details class="data-table"><summary>Trades (${r.trades.length})</summary><div class="table-wrap">${tradesTable(r.trades.slice().reverse().slice(0, 300), (t) => fmtUnixDate(t))}</div></details>
  </div>`;
  const eq = makeChart($("#bt-equity"), { height: 300, intraday });
  btCharts.push(eq);
  eq.addLine(run.strategy_name, r.equity, { slot: 0 });
  if (r.benchmark) eq.addLine(`${r.benchmark_symbol} buy & hold`, r.benchmark, { slot: 1 });
  eq.fit(); eq.renderLegend();
  const dd = makeChart($("#bt-dd"), { height: 130, intraday, percent: true });
  btCharts.push(dd);
  dd.addArea("Drawdown", r.drawdown.map((p) => ({ t: p.t, v: p.v * 100 })), { color: cssVar("--down"), topAlpha: 0.05 });
  dd.fit(); dd.renderLegend();
  syncTimeScales(eq, dd);
  $("#bt-deploy").onclick = () => { setStView("bots"); botForm({ strategy_key: run.strategy_key, params: run.params, symbols: run.symbols, name: `${run.strategy_name} bot`, benchmark: r.benchmark_symbol }); };
  $("#bt-edit").onclick = () => { renderBacktestForm({ ...run.settings, strategy_key: run.strategy_key, params: run.params, symbols: run.symbols }); $("#bt-form").scrollIntoView({ behavior: "smooth" }); };
}

function metricsTable(m, bm, bsym, r) {
  const rows = [
    ["Total return", pct(m.total_return), pct(bm.total_return)], ["Annual return (CAGR)", pct(m.cagr), pct(bm.cagr)],
    ["Volatility (ann.)", pctPlain(m.volatility), pctPlain(bm.volatility)], ["Sharpe", num2(m.sharpe), num2(bm.sharpe)],
    ["Sortino", num2(m.sortino), num2(bm.sortino)], ["Max drawdown", pctPlain(m.max_drawdown), pctPlain(bm.max_drawdown)],
    ["Calmar", num2(m.calmar), num2(bm.calmar)], ["Best bar", pct(m.best_period), pct(bm.best_period)],
    ["Worst bar", pct(m.worst_period), pct(bm.worst_period)], ["Final value", money(r.final_equity), r.benchmark ? money(r.benchmark[r.benchmark.length - 1].v) : "—"],
    ["Profit factor", num2(r.trade_stats.profit_factor), "—"], ["Orders filled", r.trade_stats.fills, "1"],
  ];
  return `<table><thead><tr><th></th><th class="r">Strategy</th><th class="r">${esc(bsym)}</th></tr></thead><tbody>${rows.map((x) => `<tr><td>${x[0]}</td><td class="r num">${x[1]}</td><td class="r num">${x[2]}</td></tr>`).join("")}</tbody></table>`;
}

async function loadBacktestHistory() {
  const rows = await api("GET", "/api/backtests").catch(() => []);
  const box = $("#bt-history");
  if (!rows.length) { box.innerHTML = `<div class="empty-inline">No backtests yet.</div>`; return; }
  box.innerHTML = `<table><thead><tr><th>Run</th><th>Strategy</th><th>Symbols</th><th>Period</th><th class="r">Return</th><th class="r">Benchmark</th><th class="r">Sharpe</th><th class="r">Max DD</th><th class="r">Trades</th><th></th></tr></thead><tbody>${
    rows.map((r) => `<tr class="clickable" data-bt="${r.id}"><td>${fmtTime(r.created_at)}</td><td><b>${esc(r.strategy_name)}</b></td><td>${esc(r.symbols.slice(0, 5).join(", "))}${r.symbols.length > 5 ? "…" : ""}</td>
      <td>${esc(r.settings.start)} → ${esc(r.settings.end || "today")}</td><td class="r num">${signed(r.summary.total_return)}</td><td class="r num">${pct(r.summary.benchmark_return)}</td>
      <td class="r num">${num2(r.summary.sharpe)}</td><td class="r num">${pctPlain(r.summary.max_drawdown)}</td><td class="r num">${r.summary.trades}</td>
      <td class="r"><button class="icon-btn" data-bt-del="${r.id}" aria-label="Delete">✕</button></td></tr>`).join("")}</tbody></table>`;
}
$("#bt-history").onclick = async (e) => {
  const del = e.target.closest("[data-bt-del]");
  if (del) { e.stopPropagation(); await guard(() => api("DELETE", `/api/backtests/${del.dataset.btDel}`)); loadBacktestHistory(); return; }
  const row = e.target.closest("[data-bt]");
  if (row) guard(async () => { renderBacktest(await api("GET", `/api/backtests/${row.dataset.bt}`)); $("#bt-results").scrollIntoView({ behavior: "smooth" }); });
};

/* ---------------- bots ---------------- */
async function loadBots() {
  const bots = await api("GET", "/api/bots").catch((e) => { toast(e.message, true); return []; });
  const box = $("#bot-list");
  if (!bots.length) {
    box.innerHTML = `<div class="card empty" style="grid-column:1/-1"><h2>No paper bots yet</h2><p>Backtest a strategy, then press <b>Deploy as paper bot</b>, or create one with <b>+ New bot</b>.</p></div>`;
    return;
  }
  box.innerHTML = "";
  bots.forEach((b) => {
    const pos = Object.entries(b.positions || {});
    const card = el(`<div class="card item">
      <div class="item-head"><div><div class="item-title">${esc(b.name)}</div><div class="item-sub">${esc(b.strategy_name)} · ${esc(b.symbols.join(", "))}</div></div>
        <span class="status ${b.last_error ? "error" : esc(b.status)}">${b.last_error ? "error" : esc(b.status)}</span></div>
      <div class="bot-figs">
        <div><div class="tile-label">Value</div><div class="num big">${money(b.equity)}</div></div>
        <div><div class="tile-label">Return</div><div class="num big">${signed(b.return_pct)}</div></div>
        <div><div class="tile-label">${esc(b.benchmark)}</div><div class="num big">${pct(b.benchmark_return)}</div></div>
      </div>
      <div class="meta"><span>Holding: ${pos.length ? pos.map(([s, q]) => `${qty(q)} ${esc(s)}`).join(", ") : "cash only"}</span><span>Cash ${money(b.cash)}</span></div>
      <div class="meta"><span>${esc(b.timeframe_label)}${b.timeframe === "1Day" ? ` at ${esc(b.run_time)}` : ""}</span>
        ${b.next_run_at ? `<span>Next: ${fmtTime(b.next_run_at, { weekday: "short" })}</span>` : ""}
        ${b.last_run_at ? `<span>Last run: ${fmtTime(b.last_run_at)}</span>` : "<span>Not run yet</span>"}
        <span>Account: ${esc(accountName(b.account_id))}</span></div>
      ${b.last_error ? `<div class="error-text">${esc(b.last_error)}</div>` : ""}
      <div class="actions"></div></div>`);
    const a = $(".actions", card);
    a.append(
      button("Details", "small", () => botDetail(b.id)),
      button("Run now", "small", (btn) => guard(async () => { const r = await api("POST", `/api/bots/${b.id}/run`); toast(r.message); loadBots(); }, btn)),
      b.status === "active"
        ? button("Pause", "small", (btn) => guard(async () => { await api("PATCH", `/api/bots/${b.id}/status`, { status: "paused" }); loadBots(); }, btn))
        : button("Resume", "small", (btn) => guard(async () => { await api("PATCH", `/api/bots/${b.id}/status`, { status: "active" }); loadBots(); }, btn)),
      button("Delete", "small danger", async () => {
        if (!(await confirmDialog({ title: "Delete bot?", html: `<p>Stop and delete <b>${esc(b.name)}</b>? Shares it bought stay in the account; sell them from the Dashboard if you want.</p>`, confirmLabel: "Delete", danger: true }))) return;
        await guard(() => api("DELETE", `/api/bots/${b.id}`));
        loadBots();
      }),
    );
    box.append(card);
  });
}

async function botDetail(id) {
  const b = await api("GET", `/api/bots/${id}`).catch((e) => { toast(e.message, true); return null; });
  if (!b) return;
  const m = b.metrics || {};
  const body = el(`<div>
    <div class="tiles">${tile("Value", money(b.equity), `started with ${money(b.allocation)}`)}${tile("Return", signed(b.return_pct))}${tile(esc(b.benchmark), signed(b.benchmark_return))}${tile("Max drawdown", pctPlain(m.max_drawdown))}${tile("Trades", b.trade_count)}</div>
    <h3 class="h3">Value vs ${esc(b.benchmark)} (same starting value)</h3><div id="bot-chart"></div>
    <h3 class="h3">Parameters</h3><p class="muted">${Object.entries(b.params).map(([k, v]) => `${esc(k.replace(/_/g, " "))}: <b>${esc(v)}</b>`).join(" · ") || "none"}</p>
    <h3 class="h3">Orders</h3><div class="table-wrap">${b.trades.length ? `<table><thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th class="r">Shares</th><th class="r">Price</th></tr></thead><tbody>${
      b.trades.map((t) => `<tr><td>${fmtTime(t.at)}</td><td class="sym">${esc(t.symbol)}</td><td class="${t.side === "buy" ? "up" : "down"}">${t.side}</td><td class="r num">${qty(t.qty)}</td><td class="r num">${money(t.price)}${t.estimated ? ' <span class="muted" title="Fill price not reported yet; last trade price used">≈</span>' : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty-inline">No orders yet.</div>`}</div>
  </div>`);
  modal.open({ title: b.name, body, foot: [button("Close", "", modal.close)] });
  if (botChart) { botChart.destroy(); botChart = null; }
  if (b.equity_curve.length > 1) {
    botChart = makeChart($("#bot-chart", body), { height: 240, intraday: true });
    botChart.addLine(b.name, b.equity_curve, { slot: 0 });
    if (b.benchmark_curve.length > 1) botChart.addLine(b.benchmark, b.benchmark_curve, { slot: 1 });
    botChart.fit(); botChart.renderLegend();
  } else {
    $("#bot-chart", body).innerHTML = `<div class="empty-inline">The chart fills in as the bot runs.</div>`;
  }
}

function botForm(prefill = {}) {
  const eligible = state.accounts.filter((a) => a.mode !== "live");
  if (!eligible.length) { toast("Add a paper or demo account first - bots can't trade live accounts", true); return; }
  const acct = eligible.find((a) => a.id === state.accountId) || eligible[0];
  const form = el(`<form>
    <div class="grid2">
      <div class="field"><label>Name</label><input name="name" value="${esc(prefill.name || "")}" placeholder="e.g. Momentum on big tech"></div>
      <div class="field"><label>Account (paper/demo only)</label><select name="account_id">${eligible.map((a) => `<option value="${a.id}" ${a.id === acct.id ? "selected" : ""}>${esc(a.name)} (${a.mode})</option>`).join("")}</select></div>
    </div>
    <div class="st-picker"></div>
    <div class="field"><label>Symbols</label><input name="symbols" value="${esc((prefill.symbols || ["SPY", "QQQ"]).join(", "))}" spellcheck="false"></div>
    <div class="grid2">
      <div class="field"><label>Money for this bot ($)</label><input type="number" name="allocation" min="1" value="${esc(prefill.allocation || 10000)}"><div class="hint">The bot only ever uses this much.</div></div>
      <div class="field"><label>Benchmark</label><input name="benchmark" value="${esc(prefill.benchmark || "SPY")}" spellcheck="false"></div>
      <div class="field"><label>Runs</label><select name="timeframe"><option value="1Day">Once a day (daily bars)</option><option value="1Hour">Every hour (hourly bars)</option><option value="15Min">Every 15 min (15-min bars)</option></select></div>
      <div class="field run-time"><label>Daily run time (${esc(tzShort())})</label><input type="time" name="run_time" value="15:45" min="09:31" max="15:58"><div class="hint">Near the close uses an almost-complete daily bar.</div></div>
    </div>
    <p class="hint">Bots trade whole shares with market orders, only while the market is open. Results won't match a backtest exactly: real fills differ from simulated ones.</p>
  </form>`);
  const picker = strategyPicker(prefill.strategy_key, prefill.params || {});
  $(".st-picker", form).append(picker);
  form.symbols.addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase(); });
  const syncTf = () => $(".run-time", form).classList.toggle("hidden", form.timeframe.value !== "1Day");
  form.timeframe.onchange = syncTf;
  syncTf();
  const save = button("Start bot", "primary", (btn) => guard(async () => {
    const f = new FormData(form);
    const body = {
      name: String(f.get("name")).trim() || `${stInfo.strategies.find((s) => s.key === picker.getKey())?.name} bot`,
      account_id: Number(f.get("account_id")), strategy_key: picker.getKey(), params: picker.getParams(),
      symbols: parseSymbols(f.get("symbols")), allocation: Number(f.get("allocation")),
      benchmark: String(f.get("benchmark")).trim().toUpperCase() || "SPY", timeframe: f.get("timeframe"), run_time: f.get("run_time") || "15:45",
    };
    if (!body.symbols.length) throw new Error("Enter at least one symbol");
    await api("POST", "/api/bots", body);
    modal.close();
    toast("Bot started");
    loadBots();
  }, btn));
  modal.open({ title: "New paper bot", body: form, foot: [button("Cancel", "", modal.close), save] });
}
$("#new-bot").onclick = () => botForm();

/* ---------------- plugins ---------------- */
async function renderPlugins() {
  const tpl = await api("GET", "/api/strategies/template").catch(() => ({ code: "" }));
  const box = $("#st-plugins");
  box.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Installed strategies</h2><button class="btn small" id="pl-reload">↻ Reload plugins</button></div>
      ${stInfo.errors.length ? `<div class="callout live">${stInfo.errors.map((e) => `<div><b>${esc(e.source)}</b>: ${esc(e.error)}</div>`).join("")}</div>` : ""}
      <div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Key</th><th>Source</th><th>Parameters</th></tr></thead><tbody>${
        stInfo.strategies.map((s) => `<tr><td><b>${esc(s.name)}</b><div class="hint" style="white-space:normal;max-width:420px">${esc(s.description)}</div></td><td><code>${esc(s.key)}</code></td><td>${esc(s.source)}</td>
          <td style="white-space:normal">${Object.entries(s.params).map(([k, p]) => `<code>${esc(k)}</code>=${esc(p.default)}`).join(", ") || "—"}</td></tr>`).join("")}</tbody></table></div>
    </div>
    <div class="card">
      <h2>Write your own</h2>
      <ol class="steps">
        <li>Create a <code>.py</code> file in your plugins folder:<br><code class="path">${esc(stInfo.plugin_dir)}</code></li>
        <li>Define a class that extends <code>Strategy</code> and returns target weights from <code>on_bar</code> (template below).</li>
        <li>Press <b>Reload plugins</b>. It appears in the Backtest and Bot strategy lists.</li>
      </ol>
      <p class="muted">Plugins are ordinary Python running on your computer. Only add code you trust. Packages can also register strategies with an <code>alpaca_manager.strategies</code> entry point.</p>
      <div class="code-head"><b>my_strategy.py</b><button class="btn small" id="pl-copy">Copy</button></div>
      <pre class="code"><code>${esc(tpl.code)}</code></pre>
    </div>`;
  $("#pl-reload").onclick = (e) => guard(async () => { stInfo = await api("POST", "/api/strategies/reload"); renderPlugins(); toast(`${stInfo.strategies.length} strategies loaded${stInfo.errors.length ? `, ${stInfo.errors.length} error(s)` : ""}`, stInfo.errors.length > 0); }, e.currentTarget);
  $("#pl-copy").onclick = async () => { try { await navigator.clipboard.writeText(tpl.code); toast("Copied"); } catch { toast("Copy failed - select the text instead", true); } };
}

/* ---------------- view switching ---------------- */
function setStView(v) {
  stView = v;
  $$("#st-view button").forEach((b) => b.classList.toggle("active", b.dataset.v === v));
  $("#st-backtest").classList.toggle("hidden", v !== "backtest");
  $("#st-bots").classList.toggle("hidden", v !== "bots");
  $("#st-plugins").classList.toggle("hidden", v !== "plugins");
  if (v === "bots") loadBots();
  if (v === "plugins") renderPlugins();
}
$("#st-view").onclick = (e) => { const b = e.target.closest("button"); if (b) setStView(b.dataset.v); };

let stLoaded = false;
TABS.strategies = {
  async load() {
    await loadStrategies();
    if (!stLoaded) { renderBacktestForm(); loadBacktestHistory(); stLoaded = true; }
    setStView(stView);
  },
  refresh: () => { if (stView === "bots") loadBots(); },
};
