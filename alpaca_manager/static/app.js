/* Alpaca Manager UI — plain JavaScript, no build step. */
"use strict";

const state = {
  accounts: [],
  accountId: null,
  status: { timezone: "America/New_York", poll_seconds: 15 },
  ordersStatus: "open",
  tab: "overview",
};

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

const money = (v, digits = 2) =>
  v == null || isNaN(v) ? "—" : Number(v).toLocaleString(undefined, { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (v) => (v == null || isNaN(v) ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`);
const qty = (v) => (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: 6 }));
const signClass = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

function fmtTime(iso, opts = {}) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    timeZone: state.status.timezone, month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit", ...opts,
  });
}
function tzShort() {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: state.status.timezone, timeZoneName: "short" })
      .formatToParts(new Date()).find((p) => p.type === "timeZoneName").value;
  } catch { return state.status.timezone; }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    let msg = data?.detail ?? `Request failed (${res.status})`;
    if (Array.isArray(msg)) msg = msg.map((d) => String(d.msg || d).replace(/^Value error, /, "")).join("; ");
    throw new Error(msg);
  }
  return data;
}

function toast(msg, isError = false) {
  const t = el(`<div class="toast ${isError ? "error" : ""}"></div>`);
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 7000 : 3500);
}

async function guard(fn, btn) {
  if (btn) btn.disabled = true;
  try { return await fn(); }
  catch (e) { toast(e.message, true); }
  finally { if (btn) btn.disabled = false; }
}

const currentAccount = () => state.accounts.find((a) => a.id === state.accountId);

/* ------------------------------------------------------------------ */
/* Modal                                                               */
/* ------------------------------------------------------------------ */
const modal = {
  open({ title, body, foot = [], small = false }) {
    $("#modal-title").textContent = title;
    const b = $("#modal-body");
    b.innerHTML = "";
    typeof body === "string" ? (b.innerHTML = body) : b.append(body);
    const f = $("#modal-foot");
    f.innerHTML = "";
    foot.forEach((x) => f.append(x));
    $(".modal-box").classList.toggle("small", small);
    $("#modal").classList.remove("hidden");
    setTimeout(() => $("#modal-body input, #modal-body select")?.focus(), 30);
  },
  close() { $("#modal").classList.add("hidden"); },
};
$("#modal-close").onclick = modal.close;
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") modal.close(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") modal.close(); });

function button(label, cls, onClick) {
  const b = el(`<button class="btn ${cls}">${esc(label)}</button>`);
  b.onclick = () => onClick(b);
  return b;
}

function confirmDialog({ title, html, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    modal.open({
      title, body: html, small: true,
      foot: [
        button("Cancel", "", () => { modal.close(); resolve(false); }),
        button(confirmLabel, danger ? "danger solid" : "primary", () => { modal.close(); resolve(true); }),
      ],
    });
  });
}

/* ------------------------------------------------------------------ */
/* Order legs editor (shared by Trade, Schedules and Rules)            */
/* ------------------------------------------------------------------ */
function describeLeg(l) {
  const size = l.amount_type === "all" ? "entire position of"
    : l.amount_type === "dollars" ? `${money(l.amount)} of` : `${qty(l.amount)} sh`;
  const px = l.order_type === "limit" ? ` @ limit ${money(l.limit_price)}` : " @ market";
  return `${l.side.toUpperCase()} ${size} ${l.symbol}${px}`;
}

function legsEditor(initial = [], { withQuotes = true } = {}) {
  const root = el(`<div class="legs"></div>`);
  const list = el(`<div class="legs"></div>`);
  const add = el(`<button type="button" class="btn small ghost leg-add">+ Add another stock</button>`);
  root.append(list, add);

  function addRow(leg = {}) {
    const l = { symbol: "", side: "buy", amount_type: "shares", amount: "", order_type: "market", limit_price: "", time_in_force: "day", ...leg };
    const row = el(`
      <div class="leg">
        <div><label>Symbol</label><input class="f-symbol" placeholder="AAPL" autocomplete="off" spellcheck="false" value="${esc(l.symbol)}"><div class="quote"></div></div>
        <div><label>Action</label><div class="seg buy-sell"><button type="button" data-v="buy">Buy</button><button type="button" data-v="sell">Sell</button></div></div>
        <div><label>Amount in</label><select class="f-amount-type">
          <option value="shares">Shares</option><option value="dollars">Dollars</option><option value="all">All shares</option></select></div>
        <div><label>Amount</label><input class="f-amount" type="number" min="0" step="any" placeholder="10" value="${esc(l.amount ?? "")}"></div>
        <div><label>Order type</label><select class="f-order-type"><option value="market">Market</option><option value="limit">Limit</option></select></div>
        <div><label>Limit price</label><input class="f-limit" type="number" min="0" step="0.01" placeholder="—" value="${esc(l.limit_price ?? "")}"></div>
        <div><label>Good for</label><select class="f-tif"><option value="day">Day</option><option value="gtc">Until canceled</option></select></div>
        <button type="button" class="icon-btn rm" title="Remove" aria-label="Remove">✕</button>
      </div>`);
    let side = l.side;
    const seg = $(".seg", row);
    const at = $(".f-amount-type", row), ot = $(".f-order-type", row), tif = $(".f-tif", row);
    at.value = l.amount_type; ot.value = l.order_type; tif.value = l.time_in_force;

    function sync() {
      $$("button", seg).forEach((b) => b.classList.toggle("active", b.dataset.v === side));
      $('option[value="all"]', at).disabled = side !== "sell";
      if (side !== "sell" && at.value === "all") at.value = "shares";
      const all = at.value === "all", dollars = at.value === "dollars";
      $(".f-amount", row).disabled = all;
      if (all || dollars) { ot.value = "market"; }
      if (dollars) tif.value = "day";
      ot.disabled = all || dollars;
      tif.disabled = all || dollars;
      $(".f-limit", row).disabled = ot.value !== "limit";
      $(".f-amount", row).placeholder = dollars ? "$500" : all ? "all" : "10";
    }
    seg.onclick = (e) => { if (e.target.dataset.v) { side = e.target.dataset.v; sync(); } };
    at.onchange = ot.onchange = sync;
    $(".rm", row).onclick = () => { row.remove(); if (!list.children.length) addRow(); };
    const symInput = $(".f-symbol", row);
    symInput.addEventListener("input", () => { symInput.value = symInput.value.toUpperCase(); });
    if (withQuotes) symInput.addEventListener("change", () => lookupQuote(symInput.value, $(".quote", row)));
    row.getLeg = () => ({
      symbol: symInput.value.trim().toUpperCase(), side, amount_type: at.value,
      amount: at.value === "all" ? null : numOrNull($(".f-amount", row).value),
      order_type: ot.value, limit_price: ot.value === "limit" ? numOrNull($(".f-limit", row).value) : null,
      time_in_force: tif.value,
    });
    sync();
    list.append(row);
    if (withQuotes && l.symbol) lookupQuote(l.symbol, $(".quote", row));
    return row;
  }

  add.onclick = () => $(".f-symbol", addRow()).focus();
  (initial.length ? initial : [{}]).forEach(addRow);

  root.getLegs = () => $$(".leg", list).map((r) => r.getLeg()).filter((l) => l.symbol);
  root.setLegs = (legs) => { list.innerHTML = ""; (legs.length ? legs : [{}]).forEach(addRow); };
  return root;
}

const numOrNull = (v) => (v === "" || v == null ? null : Number(v));

async function lookupQuote(symbol, target) {
  target.textContent = "";
  if (!symbol || !state.accountId) return;
  try {
    const q = await api("GET", `/api/accounts/${state.accountId}/quotes?symbols=${encodeURIComponent(symbol)}`);
    const px = q[symbol.toUpperCase()];
    target.textContent = px ? `Last ${money(px)}` : "No price found";
  } catch { target.textContent = "No price found"; }
}

function validateLegs(legs) {
  if (!legs.length) throw new Error("Add at least one stock");
  for (const l of legs) {
    if (l.amount_type !== "all" && !(l.amount > 0)) throw new Error(`${l.symbol}: enter an amount`);
    if (l.order_type === "limit" && !(l.limit_price > 0)) throw new Error(`${l.symbol}: enter a limit price`);
  }
}

/* ------------------------------------------------------------------ */
/* Tabs and account selection                                          */
/* ------------------------------------------------------------------ */
function showTab(name) {
  state.tab = name;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  try { localStorage.setItem("am-tab", name); } catch { /* storage unavailable */ }
  refreshTab();
}
$$(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-goto]");
  if (go) showTab(go.dataset.goto);
});

function refreshTab() {
  ({ overview: loadOverview, schedules: loadSchedules, rules: loadRules, activity: loadActivity, accounts: loadAccounts }[state.tab] || (() => {}))();
}

async function loadAccountsList() {
  state.accounts = await api("GET", "/api/accounts");
  let saved = null;
  try { saved = Number(localStorage.getItem("am-account")); } catch { /* ignore */ }
  if (!state.accounts.find((a) => a.id === state.accountId)) {
    state.accountId = (state.accounts.find((a) => a.id === saved) || state.accounts[0])?.id ?? null;
  }
  const sel = $("#account-select");
  sel.innerHTML = state.accounts.length
    ? state.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")
    : `<option value="">No accounts yet</option>`;
  sel.disabled = !state.accounts.length;
  if (state.accountId) sel.value = state.accountId;
  renderModeBadge();
}

function renderModeBadge() {
  const a = currentAccount();
  const b = $("#mode-badge");
  b.className = `badge ${a?.mode || ""}`;
  b.textContent = a ? { live: "LIVE MONEY", paper: "Paper", demo: "Demo" }[a.mode] : "";
}

$("#account-select").onchange = (e) => {
  state.accountId = Number(e.target.value);
  try { localStorage.setItem("am-account", state.accountId); } catch { /* ignore */ }
  renderModeBadge();
  refreshTab();
};

/* ------------------------------------------------------------------ */
/* Overview                                                            */
/* ------------------------------------------------------------------ */
async function loadOverview() {
  const none = !state.accountId;
  $("#no-account").classList.toggle("hidden", !none);
  $("#overview-body").classList.toggle("hidden", none);
  if (none) { $("#market-pill").textContent = "—"; return; }
  try {
    const data = await api("GET", `/api/accounts/${state.accountId}/overview`);
    renderStats(data.account);
    renderPositions(data.positions);
    renderClock(data.clock);
  } catch (e) {
    $("#stats").innerHTML = `<div class="card" style="grid-column:1/-1"><b>Couldn't reach this account.</b>
      <p class="error-text">${esc(e.message)}</p><p class="muted">Check the API keys on the Accounts tab, and that paper keys are used with a paper account.</p></div>`;
    $("#positions").innerHTML = "";
  }
  loadOrders();
}

function renderClock(c) {
  const p = $("#market-pill");
  p.classList.toggle("open", c.is_open);
  p.textContent = c.is_open ? `Market open · closes ${fmtTime(c.next_close, { month: undefined, day: undefined })}` : `Market closed · opens ${fmtTime(c.next_open, { weekday: "short", month: undefined, day: undefined })}`;
}

function renderStats(a) {
  const change = a.equity - a.last_equity;
  const changePct = a.last_equity ? change / a.last_equity : 0;
  const stat = (label, value, sub = "") => `<div class="stat"><div class="label">${label}</div><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $("#stats").innerHTML =
    stat("Portfolio value", money(a.equity), `<span class="${signClass(change)}">${change >= 0 ? "▲" : "▼"} ${money(Math.abs(change))} (${pct(changePct)}) today</span>`) +
    stat("Cash", money(a.cash)) +
    stat("Buying power", money(a.buying_power)) +
    stat("Invested", money(a.long_market_value), a.trading_blocked ? `<span class="down">Trading blocked</span>` : `<span class="muted">${esc(a.status)}</span>`);
}

function renderPositions(positions) {
  const box = $("#positions");
  if (!positions.length) { box.innerHTML = `<div class="empty-inline">No positions yet. Head to <a href="#" data-goto="trade">Trade</a> to buy something.</div>`; return; }
  box.innerHTML = `<table><thead><tr><th>Symbol</th><th class="r">Shares</th><th class="r">Price</th><th class="r">Today</th><th class="r">Value</th><th class="r">Avg cost</th><th class="r">Total gain</th><th></th></tr></thead><tbody>${
    positions.map((p) => `<tr>
      <td><span class="sym">${esc(p.symbol)}</span></td>
      <td class="r num">${qty(p.qty)}</td>
      <td class="r num">${money(p.current_price)}</td>
      <td class="r num ${signClass(p.change_today)}">${pct(p.change_today)}</td>
      <td class="r num">${money(p.market_value)}</td>
      <td class="r num">${money(p.avg_entry_price)}</td>
      <td class="r num ${signClass(p.unrealized_pl)}">${money(p.unrealized_pl)} <small>(${pct(p.unrealized_plpc)})</small></td>
      <td class="r"><button class="btn small" data-trade="${esc(p.symbol)}" data-side="buy">Buy</button>
        <button class="btn small" data-trade="${esc(p.symbol)}" data-side="sell">Sell</button>
        <button class="btn small danger" data-close="${esc(p.symbol)}">Sell all</button></td>
    </tr>`).join("")}</tbody></table>`;
}

$("#positions").addEventListener("click", async (e) => {
  const t = e.target.closest("[data-trade]");
  if (t) {
    showTab("trade");
    tradeLegs.setLegs([{ symbol: t.dataset.trade, side: t.dataset.side }]);
    return;
  }
  const c = e.target.closest("[data-close]");
  if (c) {
    const sym = c.dataset.close;
    const ok = await confirmDialog({ title: `Sell all ${sym}?`, html: `${liveWarning()}<p>This places a market order to sell your entire ${esc(sym)} position.</p>`, confirmLabel: `Sell all ${sym}`, danger: true });
    if (!ok) return;
    await guard(async () => {
      const r = await api("POST", `/api/accounts/${state.accountId}/positions/${encodeURIComponent(sym)}/close`);
      reportResults(r.results);
      loadOverview();
    });
  }
});

async function loadOrders() {
  const box = $("#orders");
  try {
    const orders = await api("GET", `/api/accounts/${state.accountId}/orders?status=${state.ordersStatus}&limit=50`);
    if (!orders.length) { box.innerHTML = `<div class="empty-inline">${state.ordersStatus === "open" ? "No open orders." : "No recent orders."}</div>`; return; }
    box.innerHTML = `<table><thead><tr><th>Submitted</th><th>Symbol</th><th>Side</th><th>Type</th><th class="r">Amount</th><th class="r">Filled</th><th>Status</th><th></th></tr></thead><tbody>${
      orders.map((o) => `<tr>
        <td>${fmtTime(o.submitted_at)}</td>
        <td><span class="sym">${esc(o.symbol)}</span></td>
        <td class="${o.side === "buy" ? "up" : "down"}">${esc(o.side)}</td>
        <td>${esc(o.type)}${o.limit_price ? ` ${money(o.limit_price)}` : ""}</td>
        <td class="r num">${o.notional ? money(o.notional) : qty(o.qty)}</td>
        <td class="r num">${o.filled_qty ? `${qty(o.filled_qty)} @ ${money(o.filled_avg_price)}` : "—"}</td>
        <td>${esc(String(o.status).replace(/_/g, " "))}</td>
        <td class="r">${["new", "accepted", "pending_new", "partially_filled", "held"].includes(o.status) ? `<button class="btn small danger" data-cancel="${esc(o.id)}">Cancel</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>`;
  } catch (e) { box.innerHTML = `<div class="empty-inline error-text">${esc(e.message)}</div>`; }
}

$("#orders").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cancel]");
  if (b) guard(async () => { await api("DELETE", `/api/accounts/${state.accountId}/orders/${b.dataset.cancel}`); toast("Order cancelled"); loadOverview(); }, b);
});
$("#orders-filter").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.ordersStatus = b.dataset.status;
  $$("#orders-filter button").forEach((x) => x.classList.toggle("active", x === b));
  loadOrders();
};
$("#refresh-overview").onclick = loadOverview;

/* ------------------------------------------------------------------ */
/* Trade                                                               */
/* ------------------------------------------------------------------ */
const tradeLegs = legsEditor();
$("#trade-legs").append(tradeLegs);

function liveWarning() {
  return currentAccount()?.mode === "live" ? `<div class="callout live">⚠ This is a LIVE account — real money will be used.</div>` : "";
}

function reportResults(results) {
  const ok = results.filter((r) => r.ok).length, failed = results.filter((r) => !r.ok);
  if (ok) toast(`${ok} order${ok > 1 ? "s" : ""} placed`);
  failed.forEach((r) => toast(`${r.leg.symbol}: ${r.error}`, true));
}

$("#trade-review").onclick = () => guard(async () => {
  if (!state.accountId) throw new Error("Add an account first");
  const legs = tradeLegs.getLegs();
  validateLegs(legs);
  const syms = [...new Set(legs.map((l) => l.symbol))].join(",");
  let quotes = {};
  try { quotes = await api("GET", `/api/accounts/${state.accountId}/quotes?symbols=${encodeURIComponent(syms)}`); } catch { /* estimate unavailable */ }
  const est = (l) => {
    if (l.amount_type === "dollars") return l.amount;
    if (l.amount_type === "all") return null;
    const px = l.order_type === "limit" ? l.limit_price : quotes[l.symbol];
    return px ? px * l.amount : null;
  };
  const items = legs.map((l) => { const v = est(l); return `<li><b>${esc(describeLeg(l))}</b>${v ? ` <span class="muted">≈ ${money(v)}</span>` : ""}${l.time_in_force === "gtc" ? ' <span class="muted">(until canceled)</span>' : ""}</li>`; }).join("");
  const ok = await confirmDialog({
    title: "Review order",
    html: `${liveWarning()}<p>Account: <b>${esc(currentAccount().name)}</b></p><ul class="review-list">${items}</ul><p class="muted">Market orders fill at the best available price, which can differ from the estimate.</p>`,
    confirmLabel: legs.length > 1 ? `Place ${legs.length} orders` : "Place order",
    danger: currentAccount().mode === "live",
  });
  if (!ok) return;
  const r = await api("POST", `/api/accounts/${state.accountId}/orders`, { legs });
  reportResults(r.results);
  if (r.results.every((x) => x.ok)) tradeLegs.setLegs([]);
}, $("#trade-review"));

/* ------------------------------------------------------------------ */
/* Schedules                                                           */
/* ------------------------------------------------------------------ */
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

function scheduleWhen(x) {
  if (x.kind === "once") return `Once · ${fmtTime(x.run_at, { weekday: "short", year: "numeric" })}`;
  const d = x.days_of_week;
  const days = d.length === 7 ? "Every day" : d.join() === "mon,tue,wed,thu,fri" ? "Every weekday" : `Every ${d.map((k) => DAY_LABEL[k]).join(", ")}`;
  const [h, m] = x.time_of_day.split(":").map(Number);
  const t = new Date(2000, 0, 1, h, m).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${days} at ${t} ${tzShort()}`;
}

const accountName = (id) => state.accounts.find((a) => a.id === id)?.name ?? "?";

async function loadSchedules() {
  const box = $("#schedule-list");
  const items = await api("GET", "/api/schedules").catch((e) => { toast(e.message, true); return []; });
  if (!items.length) {
    box.innerHTML = `<div class="card empty" style="grid-column:1/-1"><h2>No schedules yet</h2><p>Example: buy $100 of VTI every Monday at 10:00, or sell 5 TSLA next Friday at 3:30 PM.</p></div>`;
    return;
  }
  box.innerHTML = "";
  items.forEach((x) => {
    const card = el(`<div class="card item">
      <div class="item-head"><div><div class="item-title">${esc(x.name)}</div><div class="item-sub">${esc(scheduleWhen(x))}</div></div>
        <span class="status ${esc(x.status)}">${esc(x.status)}</span></div>
      <ul>${x.legs_text.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      <div class="meta"><span>Account: ${esc(accountName(x.account_id))}</span>
        ${x.next_run_at ? `<span>Next: ${fmtTime(x.next_run_at, { weekday: "short" })}</span>` : ""}
        ${x.last_run_at ? `<span>Last ran: ${fmtTime(x.last_run_at)}</span>` : ""}
        ${x.only_when_market_open ? "<span>Market hours only</span>" : ""}
        ${x.dry_run ? '<span class="tag">Dry run</span>' : ""}</div>
      <div class="actions"></div></div>`);
    const actions = $(".actions", card);
    if (x.status === "active") actions.append(button("Pause", "small", (b) => setStatus("schedules", x.id, "paused", b)));
    else if (x.status === "paused") actions.append(button("Resume", "small", (b) => setStatus("schedules", x.id, "active", b)));
    actions.append(
      button("Run now", "small", (b) => runScheduleNow(x, b)),
      button("Edit", "small", () => scheduleForm(x)),
      button("Delete", "small danger", () => deleteItem("schedules", x.id, x.name)),
    );
    box.append(card);
  });
}

async function setStatus(kind, id, status, btn) {
  await guard(async () => {
    await api("PATCH", `/api/${kind}/${id}/status`, { status });
    toast(status === "paused" ? "Paused" : "Resumed");
    kind === "schedules" ? loadSchedules() : loadRules();
  }, btn);
}

async function deleteItem(kind, id, name) {
  if (!(await confirmDialog({ title: "Delete?", html: `<p>Delete <b>${esc(name)}</b>? This can't be undone.</p>`, confirmLabel: "Delete", danger: true }))) return;
  await guard(async () => {
    await api("DELETE", `/api/${kind}/${id}`);
    toast("Deleted");
    kind === "schedules" ? loadSchedules() : loadRules();
  });
}

async function runScheduleNow(x, btn) {
  const acct = state.accounts.find((a) => a.id === x.account_id);
  const ok = await confirmDialog({
    title: "Run now?",
    html: `${acct?.mode === "live" ? '<div class="callout live">⚠ LIVE account — real money.</div>' : ""}<p>Place these orders right now${x.dry_run ? " (dry run — nothing will be sent)" : ""}?</p><ul class="review-list">${x.legs_text.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`,
    confirmLabel: "Run now",
  });
  if (!ok) return;
  await guard(async () => { const r = await api("POST", `/api/schedules/${x.id}/run`); x.dry_run ? toast("Dry run logged in Activity") : reportResults(r.results); loadSchedules(); }, btn);
}

function accountOptions(selected) {
  return state.accounts.map((a) => `<option value="${a.id}" ${a.id === selected ? "selected" : ""}>${esc(a.name)} (${a.mode})</option>`).join("");
}

function defaultRunAt() {
  // Next weekday (from tomorrow) at 10:00 in the app's timezone, as "YYYY-MM-DDTHH:MM".
  const fmt = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: state.status.timezone, year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" }).formatToParts(d);
  for (let i = 1; i < 8; i++) {
    const p = Object.fromEntries(fmt(new Date(Date.now() + i * 864e5)).map((x) => [x.type, x.value]));
    if (p.weekday !== "Sat" && p.weekday !== "Sun") return `${p.year}-${p.month}-${p.day}T10:00`;
  }
  return "";
}

function scheduleForm(x = null) {
  if (!state.accounts.length) { toast("Add an account first", true); return; }
  const v = x || { name: "", kind: "recurring", account_id: state.accountId, days_of_week: ["mon", "tue", "wed", "thu", "fri"], time_of_day: "10:00", run_at_local: defaultRunAt(), legs: [], only_when_market_open: true, dry_run: false };
  const form = el(`<form>
    <div class="grid2">
      <div class="field"><label>Name</label><input name="name" required placeholder="e.g. Weekly VTI buy" value="${esc(v.name)}"></div>
      <div class="field"><label>Account</label><select name="account_id">${accountOptions(v.account_id)}</select></div>
    </div>
    <fieldset><legend>When</legend>
      <div class="field"><div class="seg" id="kind"><button type="button" data-v="once">One time</button><button type="button" data-v="recurring">Repeat</button></div></div>
      <div class="field when-once"><label>Date & time (${esc(tzShort())})</label><input type="datetime-local" name="run_at" value="${esc(v.run_at_local || defaultRunAt())}"></div>
      <div class="when-recurring">
        <div class="field"><label>Days</label><div class="days">${DAYS.map((d) => `<button type="button" data-d="${d}" class="${v.days_of_week.includes(d) ? "on" : ""}">${DAY_LABEL[d]}</button>`).join("")}</div>
          <div class="hint"><a href="#" data-preset="weekdays">Weekdays</a> · <a href="#" data-preset="all">Every day</a></div></div>
        <div class="field" style="max-width:200px"><label>Time (${esc(tzShort())})</label><input type="time" name="time_of_day" value="${esc(v.time_of_day || "10:00")}"></div>
      </div>
    </fieldset>
    <fieldset><legend>Orders to place</legend><div class="legs-slot"></div><div style="height:10px"></div></fieldset>
    <label class="check"><input type="checkbox" name="only_when_market_open" ${v.only_when_market_open ? "checked" : ""}> Only run if the market is open (otherwise skip)</label>
    <div class="hint" style="margin:0 0 10px 26px">Untick to send orders anyway — Alpaca queues day orders for the next open.</div>
    <label class="check"><input type="checkbox" name="dry_run" ${v.dry_run ? "checked" : ""}> Dry run — just log what would happen, don't trade</label>
  </form>`);
  const legs = legsEditor(v.legs);
  $(".legs-slot", form).append(legs);
  let kind = v.kind;
  const syncKind = () => {
    $$("#kind button", form).forEach((b) => b.classList.toggle("active", b.dataset.v === kind));
    $(".when-once", form).classList.toggle("hidden", kind !== "once");
    $(".when-recurring", form).classList.toggle("hidden", kind !== "recurring");
  };
  $("#kind", form).onclick = (e) => { if (e.target.dataset.v) { kind = e.target.dataset.v; syncKind(); } };
  $(".days", form).onclick = (e) => { const b = e.target.closest("[data-d]"); if (b) b.classList.toggle("on"); };
  form.addEventListener("click", (e) => {
    const p = e.target.closest("[data-preset]");
    if (!p) return;
    e.preventDefault();
    const on = p.dataset.preset === "all" ? DAYS : DAYS.slice(0, 5);
    $$(".days button", form).forEach((b) => b.classList.toggle("on", on.includes(b.dataset.d)));
  });
  syncKind();

  const save = button(x ? "Save changes" : "Create schedule", "primary", (b) => guard(async () => {
    const f = new FormData(form);
    const legList = legs.getLegs();
    validateLegs(legList);
    const body = {
      name: f.get("name").trim(), account_id: Number(f.get("account_id")), kind,
      run_at: kind === "once" ? f.get("run_at") : null,
      days_of_week: $$(".days button.on", form).map((d) => d.dataset.d),
      time_of_day: kind === "recurring" ? f.get("time_of_day") : null,
      legs: legList, only_when_market_open: f.get("only_when_market_open") === "on", dry_run: f.get("dry_run") === "on",
    };
    if (!body.name) throw new Error("Give the schedule a name");
    await api(x ? "PUT" : "POST", x ? `/api/schedules/${x.id}` : "/api/schedules", body);
    modal.close();
    toast(x ? "Schedule updated" : "Schedule created");
    loadSchedules();
  }, b));
  modal.open({ title: x ? "Edit schedule" : "New scheduled order", body: form, foot: [button("Cancel", "", modal.close), save] });
}
$("#new-schedule").onclick = () => scheduleForm();

/* ------------------------------------------------------------------ */
/* Price rules                                                         */
/* ------------------------------------------------------------------ */
const METRICS = {
  price: { label: "One stock's price", help: "Watch the share price of a single stock." },
  basket: { label: "A basket's value", help: "Watch the combined value of a set of stocks: Σ shares × price. Handy for a model portfolio you track." },
  holdings: { label: "My holdings' value", help: "Watch the current market value of what this account actually holds in these stocks." },
};

function ruleSubject(r) {
  const syms = r.basket.map((b) => b.symbol);
  if (r.metric === "price") return `<b>${esc(syms[0])}</b> price`;
  if (r.metric === "basket") return `Basket value (${r.basket.map((b) => `${qty(b.qty)} ${esc(b.symbol)}`).join(" + ")})`;
  return `the value of my <b>${esc(syms.join(", "))}</b>`;
}

async function loadRules() {
  $("#poll-seconds").textContent = state.status.poll_seconds;
  const box = $("#rule-list");
  const items = await api("GET", "/api/rules").catch((e) => { toast(e.message, true); return []; });
  if (!items.length) {
    box.innerHTML = `<div class="card empty" style="grid-column:1/-1"><h2>No price rules yet</h2><p>Set a floor to protect gains, or a target to buy the dip.</p></div>`;
    return;
  }
  box.innerHTML = "";
  items.forEach((r) => {
    const v = r.last_value;
    const dist = v != null ? (r.direction === "below" ? (v - r.threshold) / v : (r.threshold - v) / v) : null;
    const reached = dist != null && dist <= 0;
    const fill = dist == null ? 0 : Math.max(4, Math.min(100, 100 - Math.abs(dist) * 1000));
    const card = el(`<div class="card item">
      <div class="item-head"><div class="item-title">${esc(r.name)}</div>
        <span class="status ${r.last_error ? "error" : esc(r.status)}">${r.last_error ? "error" : r.status === "active" && !r.armed ? "waiting to reset" : esc(r.status)}</span></div>
      <div class="sentence">When ${ruleSubject(r)} ${r.direction === "above" ? "rises above" : "falls below"} <b>${money(r.threshold)}</b>:</div>
      <ul>${r.legs_text.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>
      ${v != null ? `<div><div class="meta" style="justify-content:space-between"><span>Now: <b class="num" style="color:var(--text)">${money(v)}</b></span>
        <span>${reached ? "Condition met" : `${(Math.abs(dist) * 100).toFixed(1)}% away`}</span></div>
        <div class="gauge ${Math.abs(dist) < 0.02 ? "near" : ""}"><span style="width:${reached ? 100 : fill}%"></span></div></div>` : ""}
      ${r.last_error ? `<div class="error-text">${esc(r.last_error)}</div>` : ""}
      <div class="meta"><span>Account: ${esc(accountName(r.account_id))}</span>
        ${r.repeat ? `<span>Repeats (cooldown ${r.cooldown_minutes} min)</span>` : "<span>Fires once</span>"}
        ${r.only_when_market_open ? "<span>Market hours only</span>" : ""}
        ${r.last_triggered_at ? `<span>Last fired: ${fmtTime(r.last_triggered_at)}</span>` : ""}
        ${r.dry_run ? '<span class="tag">Dry run</span>' : ""}</div>
      <div class="actions"></div></div>`);
    const actions = $(".actions", card);
    if (r.status === "active") actions.append(button("Pause", "small", (b) => setStatus("rules", r.id, "paused", b)));
    else actions.append(button(r.status === "triggered" ? "Re-arm" : "Resume", "small", (b) => setStatus("rules", r.id, "active", b)));
    actions.append(button("Edit", "small", () => ruleForm(r)), button("Delete", "small danger", () => deleteItem("rules", r.id, r.name)));
    box.append(card);
  });
}

$("#check-rules").onclick = (e) => guard(async () => { await api("POST", "/api/rules/evaluate"); await loadRules(); toast("Rules checked"); }, e.currentTarget);

function ruleForm(r = null) {
  if (!state.accounts.length) { toast("Add an account first", true); return; }
  const v = r || { name: "", account_id: state.accountId, metric: "price", basket: [{ symbol: "", qty: 1 }], direction: "below", threshold: "", legs: [], repeat: false, cooldown_minutes: 60, only_when_market_open: true, dry_run: false };
  const form = el(`<form>
    <div class="grid2">
      <div class="field"><label>Name</label><input name="name" placeholder="e.g. Protect AAPL gains" value="${esc(v.name)}"></div>
      <div class="field"><label>Account</label><select name="account_id">${accountOptions(v.account_id)}</select></div>
    </div>
    <fieldset><legend>1. What to watch</legend>
      <div class="field"><div class="seg" id="metric">${Object.entries(METRICS).map(([k, m]) => `<button type="button" data-v="${k}">${m.label}</button>`).join("")}</div>
        <div class="hint" id="metric-help"></div></div>
      <div id="basket"></div>
      <button type="button" class="btn small ghost" id="add-basket">+ Add stock</button>
      <div class="field" style="margin-top:12px"><button type="button" class="btn small" id="preview">Check current value</button> <span id="preview-out" class="muted"></span></div>
    </fieldset>
    <fieldset><legend>2. Condition</legend>
      <div class="grid2">
        <div class="field"><label>When the value</label><select name="direction"><option value="below">falls to or below</option><option value="above">rises to or above</option></select></div>
        <div class="field"><label>Threshold ($)</label><input type="number" name="threshold" min="0" step="0.01" placeholder="200.00" value="${esc(v.threshold)}"></div>
      </div>
    </fieldset>
    <fieldset><legend>3. Then place these orders</legend>
      <div class="row-gap" style="margin-bottom:10px"><span class="hint" style="margin:0;align-self:center">Quick fill:</span>
        <button type="button" class="btn small" data-fill="sell-all">Sell all of the watched stocks</button>
        <button type="button" class="btn small" data-fill="buy-basket">Buy the basket</button></div>
      <div class="legs-slot"></div><div style="height:10px"></div>
    </fieldset>
    <label class="check"><input type="checkbox" name="repeat" ${v.repeat ? "checked" : ""}> Repeat — fire again each time the condition is met again</label>
    <div class="field cooldown" style="margin:8px 0 10px 26px;max-width:260px"><label>Wait at least (minutes) between fires</label><input type="number" name="cooldown_minutes" min="1" value="${esc(v.cooldown_minutes)}"></div>
    <label class="check"><input type="checkbox" name="only_when_market_open" ${v.only_when_market_open ? "checked" : ""}> Only fire while the market is open</label>
    <div class="hint" style="margin:0 0 10px 26px">If the condition is met while closed, the rule waits and fires at the next open.</div>
    <label class="check"><input type="checkbox" name="dry_run" ${v.dry_run ? "checked" : ""}> Dry run — just log what would happen, don't trade</label>
  </form>`);
  $('[name="direction"]', form).value = v.direction;
  const legs = legsEditor(v.legs);
  $(".legs-slot", form).append(legs);
  let metric = v.metric;
  const basketBox = $("#basket", form);

  function addBasketRow(b = { symbol: "", qty: 1 }) {
    const row = el(`<div class="basket-row"><div><label>Symbol</label><input class="b-sym" placeholder="AAPL" value="${esc(b.symbol)}" spellcheck="false" autocomplete="off"></div>
      <div class="qty"><label>Shares in basket</label><input class="b-qty" type="number" min="0" step="any" value="${esc(b.qty ?? 1)}"></div>
      <button type="button" class="icon-btn" title="Remove" aria-label="Remove">✕</button></div>`);
    $(".b-sym", row).addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase(); });
    $(".icon-btn", row).onclick = () => { row.remove(); if (!basketBox.children.length) addBasketRow(); };
    basketBox.append(row);
    syncMetric();
  }
  function syncMetric() {
    $$("#metric button", form).forEach((b) => b.classList.toggle("active", b.dataset.v === metric));
    $("#metric-help", form).textContent = METRICS[metric].help;
    const rows = $$(".basket-row", basketBox);
    rows.forEach((row, i) => { row.classList.toggle("no-qty", metric !== "basket"); row.classList.toggle("hidden", metric === "price" && i > 0); });
    $("#add-basket", form).classList.toggle("hidden", metric === "price");
    $('[data-fill="buy-basket"]', form).classList.toggle("hidden", metric !== "basket");
  }
  const basketItems = () => $$(".basket-row", basketBox).filter((r) => !r.classList.contains("hidden"))
    .map((r) => ({ symbol: $(".b-sym", r).value.trim().toUpperCase(), qty: Number($(".b-qty", r).value) || 0 })).filter((b) => b.symbol);

  $("#metric", form).onclick = (e) => { if (e.target.dataset.v) { metric = e.target.dataset.v; syncMetric(); $("#preview-out", form).textContent = ""; } };
  $("#add-basket", form).onclick = () => addBasketRow();
  v.basket.forEach(addBasketRow);
  const syncCooldown = () => $(".cooldown", form).classList.toggle("hidden", !$('[name="repeat"]', form).checked);
  $('[name="repeat"]', form).onchange = syncCooldown;
  syncCooldown();

  $("#preview", form).onclick = (e) => guard(async () => {
    const basket = basketItems();
    if (!basket.length) throw new Error("Add a stock to watch");
    const res = await api("POST", "/api/rules/preview", { account_id: Number($('[name="account_id"]', form).value), metric, basket });
    $("#preview-out", form).innerHTML = `Currently <b class="num" style="color:var(--text)">${money(res.value)}</b>`;
    const t = $('[name="threshold"]', form);
    if (!t.value) t.placeholder = Number(res.value).toFixed(2);
  }, e.currentTarget);

  form.addEventListener("click", (e) => {
    const f = e.target.closest("[data-fill]");
    if (!f) return;
    const basket = basketItems();
    if (!basket.length) { toast("Add a stock to watch first", true); return; }
    legs.setLegs(f.dataset.fill === "sell-all"
      ? basket.map((b) => ({ symbol: b.symbol, side: "sell", amount_type: "all" }))
      : basket.map((b) => ({ symbol: b.symbol, side: "buy", amount_type: "shares", amount: b.qty })));
  });

  const save = button(r ? "Save changes" : "Create rule", "primary", (b) => guard(async () => {
    const f = new FormData(form);
    const legList = legs.getLegs();
    const basket = basketItems();
    if (!basket.length) throw new Error("Add a stock to watch");
    if (!(Number(f.get("threshold")) > 0)) throw new Error("Enter a threshold value");
    validateLegs(legList);
    const body = {
      name: f.get("name").trim() || `${basket.map((x) => x.symbol).join("+")} ${f.get("direction")} ${f.get("threshold")}`,
      account_id: Number(f.get("account_id")), metric, basket, direction: f.get("direction"),
      threshold: Number(f.get("threshold")), legs: legList, repeat: f.get("repeat") === "on",
      cooldown_minutes: Number(f.get("cooldown_minutes")) || 60,
      only_when_market_open: f.get("only_when_market_open") === "on", dry_run: f.get("dry_run") === "on",
    };
    await api(r ? "PUT" : "POST", r ? `/api/rules/${r.id}` : "/api/rules", body);
    modal.close();
    toast(r ? "Rule updated" : "Rule created — it's being watched now");
    loadRules();
  }, b));
  modal.open({ title: r ? "Edit price rule" : "New price rule", body: form, foot: [button("Cancel", "", modal.close), save] });
}
$("#new-rule").onclick = () => ruleForm();

/* ------------------------------------------------------------------ */
/* Activity                                                            */
/* ------------------------------------------------------------------ */
async function loadActivity() {
  const only = $("#activity-this-account").checked && state.accountId;
  const rows = await api("GET", `/api/activity?limit=200${only ? `&account_id=${state.accountId}` : ""}`).catch((e) => { toast(e.message, true); return []; });
  $("#activity").innerHTML = rows.length
    ? `<table><thead><tr><th>Time</th><th>Type</th><th>Source</th><th>Account</th><th>Details</th></tr></thead><tbody>${rows.map((r) => `<tr>
        <td>${fmtTime(r.at, { second: "2-digit" })}</td><td><span class="lvl ${esc(r.level)}">${esc(r.level)}</span></td>
        <td>${esc(r.source)}</td><td>${r.account_id ? esc(accountName(r.account_id)) : ""}</td><td class="msg">${esc(r.message)}</td></tr>`).join("")}</tbody></table>`
    : `<div class="empty-inline">Nothing yet.</div>`;
}
$("#activity-this-account").onchange = loadActivity;

/* ------------------------------------------------------------------ */
/* Accounts                                                            */
/* ------------------------------------------------------------------ */
async function loadAccounts() {
  await loadAccountsList();
  const box = $("#account-list");
  if (!state.accounts.length) {
    box.innerHTML = `<div class="card empty" style="grid-column:1/-1"><h2>No accounts yet</h2><p>Add a paper account with your Alpaca paper keys, or a demo account to explore with simulated money.</p></div>`;
    return;
  }
  box.innerHTML = "";
  state.accounts.forEach((a) => {
    const card = el(`<div class="card item">
      <div class="item-head"><div class="item-title">${esc(a.name)}</div><span class="badge ${a.mode}">${{ live: "LIVE MONEY", paper: "Paper", demo: "Demo" }[a.mode]}</span></div>
      <div class="meta">${a.mode === "demo" ? "<span>Simulated — no real orders</span>" : `<span>Key ${esc(a.api_key_masked)}</span><span>Data feed: ${a.data_feed.toUpperCase()}</span>`}</div>
      <div class="actions"></div></div>`);
    $(".actions", card).append(
      button("Edit", "small", () => accountForm(a)),
      button("Remove", "small danger", async () => {
        if (!(await confirmDialog({ title: "Remove account?", html: `<p>Remove <b>${esc(a.name)}</b> and delete its schedules and price rules from this app? Your Alpaca account itself is not affected.</p>`, confirmLabel: "Remove", danger: true }))) return;
        await guard(async () => { await api("DELETE", `/api/accounts/${a.id}`); toast("Account removed"); loadAccounts(); });
      }),
    );
    box.append(card);
  });
}

function accountForm(a = null) {
  const v = a || { name: "", mode: "paper", data_feed: "iex" };
  const form = el(`<form>
    <div class="field"><label>Nickname</label><input name="name" placeholder="e.g. Retirement (paper)" value="${esc(v.name)}"></div>
    <div class="field"><label>Type</label><select name="mode">
      <option value="paper">Paper trading — practice money</option>
      <option value="live">Live trading — real money</option>
      <option value="demo">Demo — simulated, no keys needed</option></select></div>
    <div class="keys">
      <div class="callout live live-note hidden">Live accounts place real trades with real money. Consider testing your schedules and rules on a paper account first.</div>
      <div class="field"><label>API key ID</label><input name="api_key" autocomplete="off" spellcheck="false" placeholder="${a?.api_key_masked ? esc(a.api_key_masked) + " (leave blank to keep)" : "PK…"}"></div>
      <div class="field"><label>Secret key</label><input name="secret_key" type="password" autocomplete="new-password" placeholder="${a?.has_secret ? "•••••• (leave blank to keep)" : ""}"></div>
      <div class="field"><label>Market data feed</label><select name="data_feed"><option value="iex">IEX (free)</option><option value="sip">SIP (paid subscription)</option></select>
        <div class="hint">Free Alpaca plans only include IEX data.</div></div>
    </div>
  </form>`);
  $('[name="mode"]', form).value = v.mode;
  $('[name="data_feed"]', form).value = v.data_feed;
  const sync = () => {
    const mode = $('[name="mode"]', form).value;
    $(".keys", form).classList.toggle("hidden", mode === "demo");
    $(".live-note", form).classList.toggle("hidden", mode !== "live");
  };
  $('[name="mode"]', form).onchange = sync;
  sync();
  const save = button(a ? "Save" : "Add account", "primary", (b) => guard(async () => {
    const f = Object.fromEntries(new FormData(form));
    if (!f.name.trim()) throw new Error("Give the account a nickname");
    const res = await api(a ? "PUT" : "POST", a ? `/api/accounts/${a.id}` : "/api/accounts", f);
    modal.close();
    toast(a ? "Account updated" : "Account added");
    state.accountId = res.id;
    try { localStorage.setItem("am-account", res.id); } catch { /* ignore */ }
    loadAccounts();
  }, b));
  modal.open({ title: a ? "Edit account" : "Add account", body: form, foot: [button("Cancel", "", modal.close), save], small: true });
}
$("#new-account").onclick = () => accountForm();

/* ------------------------------------------------------------------ */
/* Boot                                                                */
/* ------------------------------------------------------------------ */
(async function boot() {
  try { state.status = await api("GET", "/api/status"); } catch { /* defaults */ }
  $$(".tz").forEach((x) => (x.textContent = `${tzShort()} (${state.status.timezone})`));
  await loadAccountsList().catch((e) => toast(e.message, true));
  let tab = "overview";
  try { tab = localStorage.getItem("am-tab") || tab; } catch { /* ignore */ }
  showTab($(`.tab[data-tab="${tab}"]`) ? tab : "overview");
  // Keep the visible tab fresh (not while a form is open).
  setInterval(() => {
    if (document.hidden || !$("#modal").classList.contains("hidden")) return;
    if (["overview", "rules", "activity", "schedules"].includes(state.tab)) refreshTab();
  }, 10000);
})();
