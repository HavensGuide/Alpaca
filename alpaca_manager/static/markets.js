/* Markets tab: chart explorer, comparisons, baskets, watchlist and news. */
"use strict";

const MK_RANGES = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "5Y"];
const MK_INTRADAY = new Set(["1Min", "5Min", "15Min", "30Min", "1Hour"]);
const MK_INDEXES = [
  ["S&P 500", "SPY"], ["Nasdaq-100", "QQQ"], ["Dow 30", "DIA"], ["Russell 2000", "IWM"],
  ["Total market", "VTI"], ["Bonds", "AGG"], ["Gold", "GLD"],
];
/* Overlay presets; colour slot is fixed per overlay so a line keeps its colour. */
const MK_OVERLAYS = [
  { spec: "sma:20", label: "SMA 20", slot: 0 }, { spec: "sma:50", label: "SMA 50", slot: 1 },
  { spec: "sma:200", label: "SMA 200", slot: 2 }, { spec: "ema:20", label: "EMA 20", slot: 3 },
  { spec: "bb:20", label: "Bollinger 20", slot: 4 },
];
const MAX_COMPARE = 6;

const mk = loadMkState();
let mkCharts = [];
let mkBaskets = [];
let mkWatch = [];
let mkReq = 0;

function loadMkState() {
  const d = { series: [{ kind: "symbol", symbol: "SPY", label: "SPY", slot: 0 }], range: "6M", interval: "", type: "candles",
    overlays: ["sma:50"], custom: null, volume: true, rsi: false };
  try { return { ...d, ...JSON.parse(localStorage.getItem("am-markets") || "{}") }; } catch { return d; }
}
function saveMkState() { try { localStorage.setItem("am-markets", JSON.stringify(mk)); } catch { /* ignore */ } }

/* ---------------- controls ---------------- */
function renderMkControls() {
  $("#mk-quick").innerHTML = `<span class="muted small">Indexes (ETF proxies):</span> ` +
    MK_INDEXES.map(([n, s]) => `<button class="chip" data-sym="${s}" title="${esc(n)} via ${s}">${esc(n)} <span class="muted">${s}</span></button>`).join("");
  $("#mk-range").innerHTML = MK_RANGES.map((r) => `<button data-v="${r}" class="${r === mk.range ? "active" : ""}">${r}</button>`).join("");
  $("#mk-interval").value = mk.interval;
  $$("#mk-type button").forEach((b) => b.classList.toggle("active", b.dataset.v === mk.type));
  const compare = mk.series.length > 1 || mk.series[0]?.kind === "basket";
  $("#mk-type").classList.toggle("hidden", compare);
  const custom = mk.custom ? [{ spec: mk.custom, label: customLabel(mk.custom), slot: 5 }] : [];
  $("#mk-overlays").innerHTML = mk.series.length > 1
    ? `<span class="muted small">Comparing ${mk.series.length} series as % change from the start of the range. Remove series to get indicators back.</span>`
    : `<span class="muted small">Overlays:</span> ` +
      [...MK_OVERLAYS, ...custom].map((o) => `<button class="chip toggle ${mk.overlays.includes(o.spec) ? "on" : ""}" data-ov="${o.spec}"><span class="lg-swatch" style="background:${seriesColor(o.slot)}"></span>${esc(o.label)}</button>`).join("") +
      `<button class="chip" id="mk-custom">${mk.custom ? "Change custom…" : "+ Custom…"}</button>` +
      `<span class="sep"></span>` +
      (mk.series[0]?.kind === "symbol" ? `<button class="chip toggle ${mk.volume ? "on" : ""}" data-flag="volume">Volume</button>` : "") +
      `<button class="chip toggle ${mk.rsi ? "on" : ""}" data-flag="rsi">RSI 14</button>`;
  $("#mk-chips").innerHTML = mk.series.length > 1 ? mk.series.map((s, i) => `<span class="series-chip"><span class="lg-swatch" style="background:${seriesColor(s.slot)}"></span>${esc(s.label)}<button aria-label="Remove ${esc(s.label)}" data-rm="${i}">✕</button></span>`).join("") : "";
  $("#mk-basket").innerHTML = `<option value="">Baskets…</option>` + mkBaskets.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("");
  const cur = mk.series.length === 1 && mk.series[0].kind === "basket" ? mk.series[0].basket_id : "";
  $("#mk-basket").value = cur || "";
  $("#mk-edit-basket").textContent = cur ? "Edit basket" : "+ New basket";
}
const customLabel = (spec) => { const [k, w, x] = spec.split(":"); return k === "bb" ? `Bollinger ${w}${x ? `×${x}` : ""}` : `${k.toUpperCase()} ${w}`; };

function freeSlot() {
  const used = new Set(mk.series.map((s) => s.slot));
  for (let i = 0; i < MAX_COMPARE; i++) if (!used.has(i)) return i;
  return 0;
}

function viewSymbol(sym) {
  sym = String(sym || "").trim().toUpperCase();
  if (!sym) return;
  mk.series = [{ kind: "symbol", symbol: sym, label: sym, slot: 0 }];
  saveMkState();
  loadMarketsChart();
}
function compareSymbol(sym) {
  sym = String(sym || "").trim().toUpperCase();
  if (!sym) return;
  if (mk.series.some((s) => s.kind === "symbol" && s.symbol === sym)) { toast(`${sym} is already on the chart`); return; }
  if (mk.series.length >= MAX_COMPARE) { toast(`Compare up to ${MAX_COMPARE} series at once`, true); return; }
  mk.series.push({ kind: "symbol", symbol: sym, label: sym, slot: freeSlot() });
  saveMkState();
  loadMarketsChart();
}

$("#mk-view").onclick = () => { viewSymbol($("#mk-symbol").value); $("#mk-symbol").value = ""; };
$("#mk-compare").onclick = () => { compareSymbol($("#mk-symbol").value); $("#mk-symbol").value = ""; };
$("#mk-symbol").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#mk-view").click(); });
$("#mk-symbol").addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase(); });
$("#mk-quick").onclick = (e) => { const b = e.target.closest("[data-sym]"); if (b) viewSymbol(b.dataset.sym); };
$("#mk-chips").onclick = (e) => {
  const b = e.target.closest("[data-rm]");
  if (!b) return;
  mk.series.splice(Number(b.dataset.rm), 1);
  if (mk.series.length === 1) mk.series[0].slot = 0;
  saveMkState();
  loadMarketsChart();
};
$("#mk-range").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  mk.range = b.dataset.v;
  mk.interval = ""; // let the range choose a sensible bar size
  saveMkState();
  loadMarketsChart();
};
$("#mk-interval").onchange = (e) => { mk.interval = e.target.value; saveMkState(); loadMarketsChart(); };
$("#mk-type").onclick = (e) => { const b = e.target.closest("button"); if (b) { mk.type = b.dataset.v; saveMkState(); loadMarketsChart(); } };
$("#mk-overlays").onclick = (e) => {
  const ov = e.target.closest("[data-ov]");
  const flag = e.target.closest("[data-flag]");
  if (ov) {
    const s = ov.dataset.ov;
    mk.overlays = mk.overlays.includes(s) ? mk.overlays.filter((x) => x !== s) : [...mk.overlays, s];
  } else if (flag) {
    mk[flag.dataset.flag] = !mk[flag.dataset.flag];
  } else if (e.target.closest("#mk-custom")) {
    customOverlayForm();
    return;
  } else return;
  saveMkState();
  loadMarketsChart();
};

function customOverlayForm() {
  const form = el(`<form><div class="grid2">
    <div class="field"><label>Type</label><select name="kind"><option value="sma">Simple moving average</option><option value="ema">Exponential moving average</option><option value="bb">Bollinger bands</option></select></div>
    <div class="field"><label>Window (bars)</label><input name="w" type="number" min="2" max="500" value="100"></div></div>
    <div class="hint">Shown in violet. ${mk.custom ? `Replaces ${esc(customLabel(mk.custom))}.` : ""}</div></form>`);
  const save = button("Add overlay", "primary", () => {
    const f = new FormData(form);
    const w = Math.round(Number(f.get("w")));
    if (!(w >= 2 && w <= 500)) { toast("Window must be 2-500", true); return; }
    if (mk.custom) mk.overlays = mk.overlays.filter((x) => x !== mk.custom);
    mk.custom = `${f.get("kind")}:${w}`;
    mk.overlays.push(mk.custom);
    saveMkState();
    modal.close();
    loadMarketsChart();
  });
  const foot = [button("Cancel", "", modal.close), save];
  if (mk.custom) foot.unshift(button("Remove custom", "danger", () => { mk.overlays = mk.overlays.filter((x) => x !== mk.custom); mk.custom = null; saveMkState(); modal.close(); loadMarketsChart(); }));
  modal.open({ title: "Custom overlay", body: form, foot, small: true });
}

/* ---------------- baskets ---------------- */
async function loadBaskets() { mkBaskets = await api("GET", "/api/baskets").catch(() => []); }

$("#mk-basket").onchange = (e) => {
  const b = mkBaskets.find((x) => x.id === Number(e.target.value));
  if (!b) return;
  mk.series = [{ kind: "basket", basket_id: b.id, label: b.name, slot: 0 }];
  saveMkState();
  loadMarketsChart();
};
$("#mk-edit-basket").onclick = () => {
  const cur = mk.series.length === 1 && mk.series[0].kind === "basket" ? mkBaskets.find((b) => b.id === mk.series[0].basket_id) : null;
  basketForm(cur);
};

function basketForm(b = null) {
  const form = el(`<form>
    <div class="field"><label>Name</label><input name="name" placeholder="e.g. My tech basket" value="${esc(b?.name || "")}"></div>
    <p class="hint" style="margin-top:0">The basket's value is Σ shares × price. Use your real holdings or a model portfolio.</p>
    <div id="bk-rows"></div>
    <button type="button" class="btn small ghost" id="bk-add">+ Add stock</button>
  </form>`);
  const rows = $("#bk-rows", form);
  const addRow = (it = { symbol: "", qty: 1 }) => {
    const r = el(`<div class="basket-row"><div><label>Symbol</label><input class="b-sym" value="${esc(it.symbol)}" placeholder="AAPL" spellcheck="false"></div>
      <div class="qty"><label>Shares</label><input class="b-qty" type="number" min="0" step="any" value="${esc(it.qty)}"></div>
      <button type="button" class="icon-btn" aria-label="Remove">✕</button></div>`);
    $(".b-sym", r).addEventListener("input", (e) => { e.target.value = e.target.value.toUpperCase(); });
    $(".icon-btn", r).onclick = () => r.remove();
    rows.append(r);
  };
  (b?.items?.length ? b.items : [{ symbol: "", qty: 10 }, { symbol: "", qty: 10 }]).forEach(addRow);
  $("#bk-add", form).onclick = () => addRow();
  const save = button(b ? "Save basket" : "Create basket", "primary", (btn) => guard(async () => {
    const name = form.name.value.trim();
    const items = $$(".basket-row", rows).map((r) => ({ symbol: $(".b-sym", r).value.trim(), qty: Number($(".b-qty", r).value) })).filter((x) => x.symbol);
    if (!name) throw new Error("Give the basket a name");
    if (!items.length) throw new Error("Add at least one stock");
    const saved = await api(b ? "PUT" : "POST", b ? `/api/baskets/${b.id}` : "/api/baskets", { name, items });
    modal.close();
    await loadBaskets();
    mk.series = [{ kind: "basket", basket_id: saved.id, label: saved.name, slot: 0 }];
    saveMkState();
    loadMarketsChart();
    toast(b ? "Basket saved" : "Basket created");
  }, btn));
  const foot = [button("Cancel", "", modal.close), save];
  if (b) foot.unshift(button("Delete", "danger", async () => {
    if (!(await confirmDialog({ title: "Delete basket?", html: `<p>Delete <b>${esc(b.name)}</b>?</p>`, confirmLabel: "Delete", danger: true }))) return;
    await api("DELETE", `/api/baskets/${b.id}`);
    await loadBaskets();
    viewSymbol("SPY");
  }));
  modal.open({ title: b ? "Edit basket" : "New basket", body: form, foot, small: true });
}

/* ---------------- chart ---------------- */
function destroyMkCharts() { mkCharts.forEach((c) => c.destroy()); mkCharts = []; }

async function loadMarketsChart() {
  renderMkControls();
  if (!state.accountId) { $("#mk-chart").innerHTML = `<div class="empty-inline">Add an account to load market data.</div>`; return; }
  if (!mk.series.length) mk.series = [{ kind: "symbol", symbol: "SPY", label: "SPY", slot: 0 }];
  const compare = mk.series.length > 1;
  const single = mk.series[0];
  const indicators = compare ? [] : [...mk.overlays, ...(mk.rsi ? ["rsi:14"] : [])];
  const req = ++mkReq;
  $("#mk-chart").classList.add("loading");
  let data;
  try {
    data = await api("POST", "/api/market/series", {
      account_id: state.accountId, range: mk.range, interval: mk.interval || null, indicators,
      items: mk.series.map((s) => (s.kind === "symbol" ? { kind: "symbol", symbol: s.symbol } : { kind: "basket", basket_id: s.basket_id })),
    });
  } catch (e) {
    if (req !== mkReq) return;
    $("#mk-chart").classList.remove("loading");
    destroyMkCharts();
    $("#mk-chart").innerHTML = `<div class="empty-inline error-text">${esc(e.message)}</div>`;
    $("#mk-head").innerHTML = ""; $("#mk-stats").innerHTML = ""; $("#mk-rsi").classList.add("hidden");
    return;
  }
  if (req !== mkReq) return; // a newer request superseded this one
  $("#mk-chart").classList.remove("loading");
  if (!mk.interval) $("#mk-interval").options[0].textContent = `Auto (${data.timeframe.replace("Min", " min").replace("1Hour", "1 hour").replace("1Day", "1 day").replace("1Week", "1 week")})`;
  destroyMkCharts();
  const intraday = MK_INTRADAY.has(data.timeframe);
  const good = data.series.filter((s) => !s.error);
  data.series.filter((s) => s.error).forEach((s) => toast(`${s.label}: ${s.error}`, true));
  if (!good.length) {
    $("#mk-chart").innerHTML = `<div class="empty-inline">No data for this selection.</div>`;
    $("#mk-head").innerHTML = ""; $("#mk-stats").innerHTML = ""; $("#mk-table").innerHTML = "";
    return;
  }
  if (compare) renderCompare(data, good, intraday);
  else renderSingle(data, good[0], single, intraday);
  loadMkNews();
}

function renderSingle(data, s, item, intraday) {
  const st = s.stats;
  const isSym = item.kind === "symbol";
  $("#mk-head").innerHTML = `<div class="mk-title"><h2>${esc(s.label)}</h2>${isSym ? "" : `<span class="badge">Basket</span>`}
      <span class="mk-price num">${money(st.last)}</span>
      <span class="num">${signed(st.change_pct)} <span class="muted">(${st.change >= 0 ? "+" : ""}${money(st.change)}) · ${esc(data.range)}</span></span></div>
    <div class="row-gap">${isSym ? `<button class="btn small" id="mk-trade">Trade ${esc(s.label)}</button>
      ${mkWatch.includes(s.label) ? "" : `<button class="btn small ghost" id="mk-watch">+ Watchlist</button>`}` : ""}</div>`;
  if (isSym) {
    $("#mk-trade").onclick = () => { showTab("trade"); tradeLegs.setLegs([{ symbol: s.label, side: "buy" }]); };
    const w = $("#mk-watch");
    if (w) w.onclick = () => saveWatchlist([...mkWatch, s.label]);
  }

  const chart = makeChart($("#mk-chart"), { height: 420, intraday });
  mkCharts.push(chart);
  if (isSym && mk.type === "candles") chart.addCandles(s.label, s.candles);
  else chart.addLine(s.label, s.line, { color: cssVar("--text"), width: 2 });
  if (isSym && mk.volume) chart.addVolume(s.candles);
  const ovs = [...MK_OVERLAYS, ...(mk.custom ? [{ spec: mk.custom, label: customLabel(mk.custom), slot: 5 }] : [])];
  for (const o of ovs) {
    if (!mk.overlays.includes(o.spec)) continue;
    for (const [name, pts] of Object.entries(s.indicators)) {
      if (name.startsWith("RSI")) continue;
      const matches = o.spec.startsWith("bb") ? name.startsWith(`BB ${o.spec.split(":")[1]} `) : name === o.label || name === customLabel(o.spec);
      if (!matches) continue;
      const band = name.endsWith("upper") || name.endsWith("lower");
      chart.addLine(band ? name.replace("BB", "Bollinger") : name.replace(" mid", "").replace("BB", "Bollinger"), pts,
        { slot: o.slot, width: band ? 1 : 2, dashed: name.endsWith("mid"), legend: !band });
    }
  }
  chart.fit();
  chart.renderLegend();

  const rsiPts = Object.entries(s.indicators).find(([n]) => n.startsWith("RSI"));
  const rsiHost = $("#mk-rsi");
  if (mk.rsi && rsiPts) {
    rsiHost.classList.remove("hidden");
    const rc = makeChart(rsiHost, { height: 130, intraday, valueFormat: (v) => v.toFixed(1) });
    mkCharts.push(rc);
    const line = rc.addLine(rsiPts[0], rsiPts[1], { color: seriesColor(5) });
    line.createPriceLine({ price: 70, color: cssVar("--muted"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "70" });
    line.createPriceLine({ price: 30, color: cssVar("--muted"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "30" });
    rc.chart.priceScale("right").applyOptions({ autoScale: false, scaleMargins: { top: 0.1, bottom: 0.1 } });
    line.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
    rc.fit();
    rc.renderLegend();
    syncTimeScales(chart, rc);
  } else {
    rsiHost.classList.add("hidden");
    rsiHost.innerHTML = "";
  }

  $("#mk-stats").innerHTML =
    tile(`Change (${esc(data.range)})`, signed(st.change_pct)) +
    tile("Range high", money(st.high)) + tile("Range low", money(st.low)) +
    tile("Volatility", pctPlain(st.volatility), "annualised", "Standard deviation of returns, scaled to a year") +
    tile("Max drawdown", pctPlain(st.max_drawdown), "peak to trough in range") +
    (st.avg_volume != null ? tile("Avg volume", compact(st.avg_volume), "per bar") : tile("Bars", st.bars));

  const cols = Object.keys(s.indicators).filter((n) => !n.includes("upper") && !n.includes("lower"));
  const idx = Object.fromEntries(cols.map((n) => [n, new Map(s.indicators[n].map((p) => [p.t, p.v]))]));
  const rows = (isSym ? s.candles : s.line.map((p) => ({ t: p.t, c: p.v }))).slice(-100).reverse();
  $("#mk-table").innerHTML = `<table><thead><tr><th>Time (NY)</th>${isSym ? '<th class="r">Open</th><th class="r">High</th><th class="r">Low</th>' : ""}<th class="r">${isSym ? "Close" : "Value"}</th>${isSym ? '<th class="r">Volume</th>' : ""}${cols.map((c) => `<th class="r">${esc(c)}</th>`).join("")}</tr></thead><tbody>${
    rows.map((b) => `<tr><td>${fmtChartTime(toChartTime(b.t), intraday)}</td>${isSym ? `<td class="r num">${num2(b.o)}</td><td class="r num">${num2(b.h)}</td><td class="r num">${num2(b.l)}</td>` : ""}<td class="r num">${num2(b.c)}</td>${isSym ? `<td class="r num">${compact(b.v)}</td>` : ""}${cols.map((c) => `<td class="r num">${num2(idx[c].get(b.t))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderCompare(data, good, intraday) {
  $("#mk-head").innerHTML = `<div class="mk-title"><h2>Comparison</h2><span class="muted">% change over ${esc(data.range)}</span></div>`;
  const chart = makeChart($("#mk-chart"), { height: 420, intraday, percent: true });
  mkCharts.push(chart);
  for (const s of good) {
    const item = mk.series.find((x) => x.label === s.label) || { slot: 0 };
    const base = s.line[0]?.v;
    chart.addLine(s.label, s.line.map((p) => ({ t: p.t, v: (p.v / base - 1) * 100 })), { slot: item.slot });
  }
  chart.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
  chart.fit();
  chart.renderLegend();
  $("#mk-rsi").classList.add("hidden");
  $("#mk-stats").innerHTML = `<div class="table-wrap" style="grid-column:1/-1"><table><thead><tr><th>Series</th><th class="r">Change</th><th class="r">Volatility (ann.)</th><th class="r">Max drawdown</th><th class="r">Last</th></tr></thead><tbody>${
    good.map((s) => { const item = mk.series.find((x) => x.label === s.label) || { slot: 0 }; return `<tr><td><span class="lg-swatch" style="background:${seriesColor(item.slot)}"></span> <b>${esc(s.label)}</b></td><td class="r num">${signed(s.stats.change_pct)}</td><td class="r num">${pctPlain(s.stats.volatility)}</td><td class="r num">${pctPlain(s.stats.max_drawdown)}</td><td class="r num">${money(s.stats.last)}</td></tr>`; }).join("")}</tbody></table></div>`;
  const times = good[0].line.slice(-100).reverse();
  const maps = good.map((s) => new Map(s.line.map((p) => [p.t, p.v])));
  $("#mk-table").innerHTML = `<table><thead><tr><th>Time (NY)</th>${good.map((s) => `<th class="r">${esc(s.label)}</th>`).join("")}</tr></thead><tbody>${
    times.map((p) => `<tr><td>${fmtChartTime(toChartTime(p.t), intraday)}</td>${maps.map((m) => `<td class="r num">${num2(m.get(p.t))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

/* ---------------- news ---------------- */
async function loadMkNews() {
  const syms = new Set();
  for (const s of mk.series) {
    if (s.kind === "symbol") syms.add(s.symbol);
    else (mkBaskets.find((b) => b.id === s.basket_id)?.items || []).forEach((i) => syms.add(i.symbol));
  }
  const list = [...syms].slice(0, 10);
  $("#mk-news-for").textContent = list.join(", ");
  try {
    const items = await api("GET", `/api/market/news?account_id=${state.accountId}&symbols=${encodeURIComponent(list.join(","))}&limit=12`);
    $("#mk-news").innerHTML = renderNews(items);
  } catch (e) { $("#mk-news").innerHTML = `<div class="empty-inline error-text">${esc(e.message)}</div>`; }
}

/* ---------------- watchlist ---------------- */
let wlEditing = false;
async function loadWatchlist() {
  mkWatch = (await api("GET", "/api/watchlist").catch(() => ({ symbols: [] }))).symbols;
  await refreshWatchQuotes();
}
async function refreshWatchQuotes() {
  const box = $("#watchlist");
  if (!mkWatch.length) { box.innerHTML = `<div class="empty-inline">Your watchlist is empty. Press Edit to add symbols.</div>`; return; }
  let quotes = [];
  if (state.accountId) {
    try { quotes = await api("GET", `/api/market/snapshots?account_id=${state.accountId}&symbols=${encodeURIComponent(mkWatch.join(","))}`); } catch { /* show without prices */ }
  }
  const q = Object.fromEntries(quotes.map((x) => [x.symbol, x]));
  const active = new Set(mk.series.filter((s) => s.kind === "symbol").map((s) => s.symbol));
  box.innerHTML = `<ul class="wl">${mkWatch.map((s) => {
    const x = q[s] || {};
    return `<li class="${active.has(s) ? "active" : ""}"><button class="wl-row" data-sym="${esc(s)}">
      <span class="sym">${esc(s)}</span><span class="num">${x.price != null ? money(x.price) : "—"}</span>
      <span class="num wl-chg ${signClass(x.change_pct)}">${x.change_pct != null ? `${x.change_pct >= 0 ? "▲" : "▼"} ${pct(x.change_pct).replace("+", "")}` : ""}</span></button>
      ${wlEditing ? `<button class="icon-btn" data-wl-rm="${esc(s)}" aria-label="Remove ${esc(s)}">✕</button>` : `<button class="icon-btn wl-cmp" data-wl-cmp="${esc(s)}" title="Add to comparison" aria-label="Compare ${esc(s)}">+</button>`}</li>`;
  }).join("")}</ul>`;
}
async function saveWatchlist(list) {
  await guard(async () => {
    mkWatch = (await api("PUT", "/api/watchlist", { symbols: list })).symbols;
    refreshWatchQuotes();
    renderMkHeadWatchButton();
  });
}
function renderMkHeadWatchButton() { const w = $("#mk-watch"); if (w && mkWatch.includes(mk.series[0]?.symbol)) w.remove(); }
$("#watchlist").onclick = (e) => {
  const rm = e.target.closest("[data-wl-rm]");
  if (rm) return saveWatchlist(mkWatch.filter((s) => s !== rm.dataset.wlRm));
  const cmp = e.target.closest("[data-wl-cmp]");
  if (cmp) return compareSymbol(cmp.dataset.wlCmp);
  const row = e.target.closest("[data-sym]");
  if (row) viewSymbol(row.dataset.sym);
};
$("#wl-edit").onclick = () => {
  wlEditing = !wlEditing;
  $("#wl-edit").textContent = wlEditing ? "Done" : "Edit";
  $("#wl-add").classList.toggle("hidden", !wlEditing);
  refreshWatchQuotes();
};
$("#wl-add").onsubmit = (e) => {
  e.preventDefault();
  const inp = $("input", e.target);
  const sym = inp.value.trim().toUpperCase();
  if (!sym) return;
  if (!mkWatch.includes(sym)) saveWatchlist([...mkWatch, sym]);
  inp.value = "";
};

/* ---------------- tab hooks ---------------- */
let mkLoaded = false;
TABS.markets = {
  async load() {
    if (!mkLoaded) { await Promise.all([loadBaskets(), loadWatchlist()]); mkLoaded = true; }
    else refreshWatchQuotes();
    loadMarketsChart();
  },
  refresh: () => refreshWatchQuotes(),
};
