/* Performance tab: account equity vs a benchmark, and closed-trade analysis. */
"use strict";

let pfPeriod = "3M";
let pfChart = null;

async function loadPerformance() {
  if (!state.accountId) { $("#pf-tiles").innerHTML = `<div class="empty-inline" style="grid-column:1/-1">Add an account first.</div>`; return; }
  loadPfHistory();
  loadPfTrades();
}

async function loadPfHistory() {
  const bench = $("#pf-bench").checked ? $("#pf-bench-sym").value.trim().toUpperCase() : "";
  const host = $("#pf-chart");
  let h;
  try {
    h = await api("GET", `/api/accounts/${state.accountId}/history?period=${pfPeriod}&benchmark=${encodeURIComponent(bench)}`);
  } catch (e) {
    host.innerHTML = `<div class="empty-inline error-text">${esc(e.message)}</div>`;
    $("#pf-tiles").innerHTML = "";
    return;
  }
  if (pfChart) { pfChart.destroy(); pfChart = null; }
  if (h.points.length < 2) {
    host.innerHTML = `<div class="empty-inline">Not enough account history for this period yet.</div>`;
    $("#pf-tiles").innerHTML = "";
    return;
  }
  const st = h.stats || {};
  const first = h.points[0].equity, last = h.points[h.points.length - 1].equity;
  let benchRet = null;
  if (h.benchmark && h.benchmark.length > 1) benchRet = h.benchmark[h.benchmark.length - 1].v / h.benchmark[0].v - 1;
  const excess = benchRet != null && st.total_return != null ? st.total_return - benchRet : null;
  $("#pf-tiles").innerHTML =
    tile("Return", signed(st.total_return), `${money(last - first)}`) +
    (h.benchmark_symbol ? tile(`vs ${esc(h.benchmark_symbol)}`, signed(excess), `${esc(h.benchmark_symbol)} ${pct(benchRet)}`, "Your return minus the benchmark's") : "") +
    tile("Max drawdown", pctPlain(st.max_drawdown), "largest peak-to-trough fall") +
    tile("Sharpe ratio", num2(st.sharpe), "return per unit of risk", "Annualised mean return ÷ volatility, risk-free rate 0") +
    tile("Volatility", pctPlain(st.volatility), "annualised") +
    tile("Best / worst period", `${signed(st.best_period)} <span class="muted">/</span> ${signed(st.worst_period)}`);
  pfChart = makeChart(host, { height: 340, intraday: pfPeriod === "1W" });
  pfChart.addLine("Portfolio", h.points.map((p) => ({ t: p.t, v: p.equity })), { slot: 0 });
  if (h.benchmark) pfChart.addLine(`${h.benchmark_symbol} (same starting value)`, h.benchmark, { slot: 1 });
  pfChart.fit();
  pfChart.renderLegend();
}

async function loadPfTrades() {
  let d;
  try { d = await api("GET", `/api/accounts/${state.accountId}/trades`); } catch (e) {
    $("#pf-trade-tiles").innerHTML = `<div class="empty-inline error-text" style="grid-column:1/-1">${esc(e.message)}</div>`;
    return;
  }
  const st = d.stats;
  $("#pf-note").textContent = d.note || `${d.fills} filled orders analysed`;
  $("#pf-trade-tiles").innerHTML =
    tile("Realized P&L", `<span class="${signClass(st.realized_pnl)}">${money(st.realized_pnl)}</span>`) +
    tile("Closed trades", st.trades) +
    tile("Win rate", pctPlain(st.win_rate, 0)) +
    tile("Avg win / loss", `${money(st.avg_win)} <span class="muted">/</span> ${money(st.avg_loss)}`) +
    tile("Profit factor", num2(st.profit_factor), "gross wins ÷ gross losses");
  $("#pf-by-symbol").innerHTML = pnlBars(d.by_symbol);
  $("#pf-trades").innerHTML = tradesTable(d.trades.slice(0, 50), (t) => fmtTime(t));
}

/* Horizontal diverging bars: gains right of the zero line, losses left. */
function pnlBars(rows) {
  if (!rows.length) return `<div class="empty-inline">No closed trades yet. A trade closes when you sell shares you bought.</div>`;
  const max = Math.max(...rows.map((r) => Math.abs(r.pnl))) || 1;
  // Zero line in the middle only when there are both gains and losses.
  const mixed = rows.some((r) => r.pnl < 0) && rows.some((r) => r.pnl > 0);
  const zero = mixed ? 50 : rows.every((r) => r.pnl <= 0) ? 100 : 0;
  const span = mixed ? 50 : 100;
  return `<div class="pnl-bars">${rows.slice(0, 15).map((r) => {
    const w = (Math.abs(r.pnl) / max) * span;
    return `<div class="pnl-row" title="${esc(r.symbol)}: ${r.trades} trade(s), ${r.wins} win(s)">
      <span class="sym">${esc(r.symbol)}</span>
      <span class="pnl-track"><span class="pnl-bar ${r.pnl >= 0 ? "pos" : "neg"}" style="${r.pnl >= 0 ? `left:${zero}%;width:${w}%` : `right:${100 - zero}%;width:${w}%`}"></span><span class="pnl-zero" style="left:${zero}%"></span></span>
      <span class="num ${signClass(r.pnl)}">${money(r.pnl)}</span></div>`;
  }).join("")}</div>`;
}

function tradesTable(trades, fmtT) {
  if (!trades.length) return `<div class="empty-inline">No closed trades.</div>`;
  return `<table><thead><tr><th>Symbol</th><th class="r">Shares</th><th>Bought</th><th class="r">Entry</th><th>Sold</th><th class="r">Exit</th><th class="r">P&amp;L</th></tr></thead><tbody>${
    trades.map((t) => `<tr><td><span class="sym">${esc(t.symbol)}</span></td><td class="r num">${qty(t.qty)}</td>
      <td>${fmtT(t.entry_time)}</td><td class="r num">${money(t.entry_price)}</td><td>${fmtT(t.exit_time)}</td><td class="r num">${money(t.exit_price)}</td>
      <td class="r num ${signClass(t.pnl)}">${money(t.pnl)} <small>(${pct(t.return_pct)})</small></td></tr>`).join("")}</tbody></table>`;
}

$("#pf-period").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  pfPeriod = b.dataset.p;
  $$("#pf-period button").forEach((x) => x.classList.toggle("active", x === b));
  loadPfHistory();
};
$("#pf-bench").onchange = loadPfHistory;
$("#pf-bench-sym").addEventListener("change", (e) => { e.target.value = e.target.value.toUpperCase(); loadPfHistory(); });

TABS.performance = { load: loadPerformance, refresh: null };
