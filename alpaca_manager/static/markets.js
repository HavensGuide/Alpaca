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
    overlays: ["sma:50"], custom: null, volume: true, rsi: false,
    basketMode: "combined", corr: true, corrWindow: 60 };
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
  const oneBasket = mk.series.length === 1 && mk.series[0].kind === "basket";
  const n = viewItems().length;
  const basketSeg = oneBasket ? `<div class="seg small-seg" id="mk-bmode" title="Show the basket as one line or each stock separately">
      <button data-v="combined" class="${mk.basketMode === "combined" ? "active" : ""}">Combined value</button>
      <button data-v="separate" class="${mk.basketMode === "separate" ? "active" : ""}">Each stock</button></div><span class="sep"></span>` : "";
  $("#mk-overlays").innerHTML = isMultiView()
    ? basketSeg + `<span class="muted small">${n} series as % change from the start of the range.</span><span class="sep"></span>` +
      `<button class="chip toggle ${mk.corr ? "on" : ""}" data-flag="corr" title="Rolling correlation of returns, shown in a panel under the chart">Correlation</button>` +
      `<label class="inline">over <select id="mk-corr-win">${[20, 60, 120].map((w) => `<option value="${w}" ${w === mk.corrWindow ? "selected" : ""}>${w} bars</option>`).join("")}</select></label>` +
      `<button class="chip" id="mk-matrix">Correlation matrix…</button>`
    : basketSeg + `<span class="muted small">Overlays:</span> ` +
      [...MK_OVERLAYS, ...custom].map((o) => `<button class="chip toggle ${mk.overlays.includes(o.spec) ? "on" : ""}" data-ov="${o.spec}"><span class="lg-swatch" style="background:${seriesColor(o.slot)}"></span>${esc(o.label)}</button>`).join("") +
      `<button class="chip" id="mk-custom">${mk.custom ? "Change custom…" : "+ Custom…"}</button>` +
      `<span class="sep"></span>` +
      (mk.series[0]?.kind === "symbol" ? `<button class="chip toggle ${mk.volume ? "on" : ""}" data-flag="volume">Volume</button>` : "") +
      `<button class="chip toggle ${mk.rsi ? "on" : ""}" data-flag="rsi">RSI 14</button>`;
  $("#mk-chips").innerHTML = mk.series.length > 1 ? mk.series.map((s, i) => `<span class="series-chip"><span class="lg-swatch" style="background:${seriesColor(s.slot)}"></span>${esc(s.label)}<button aria-label="Remove ${esc(s.label)}" data-rm="${i}">✕</button></span>`).join("") : "";
  $("#mk-basket").innerHTML = `<option value="">Baskets…</option>` + mkBaskets.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("");
  const cur = mk.series.length === 1 && mk.series[0].kind === "basket" ? mk.series[0].basket_id : "";
  $("#mk-basket").value = cur || "";
  // Edit applies to the basket on screen; New always creates another one.
  $("#mk-edit-basket").classList.toggle("hidden", !cur);
  $("#mk-compare-basket").classList.toggle("hidden", !mkBaskets.length);
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
$("#mk-overlays").addEventListener("change", (e) => {
  if (e.target.id === "mk-corr-win") { mk.corrWindow = Number(e.target.value); saveMkState(); loadMarketsChart(); }
});
$("#mk-overlays").onclick = (e) => {
  const bm = e.target.closest("#mk-bmode button");
  if (bm) { mk.basketMode = bm.dataset.v; saveMkState(); loadMarketsChart(); return; }
  if (e.target.closest("#mk-matrix")) { showCorrMatrix(); return; }
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
  if (cur) basketForm(cur);
};
$("#mk-new-basket").onclick = () => basketForm(null);
/* Add a basket to whatever is on the chart (as its combined value). */
$("#mk-compare-basket").onclick = () => {
  if (!mkBaskets.length) return;
  const body = el(`<div><div class="field"><label>Basket</label><select>${mkBaskets.map((b) => `<option value="${b.id}">${esc(b.name)}</option>`).join("")}</select></div></div>`);
  modal.open({ title: "Compare a basket", body, small: true, foot: [button("Cancel", "", modal.close), button("Add to chart", "primary", () => {
    const b = mkBaskets.find((x) => x.id === Number($("select", body).value));
    modal.close();
    if (!b) return;
    if (mk.series.some((x) => x.kind === "basket" && x.basket_id === b.id)) { toast(`${b.name} is already on the chart`); return; }
    if (mk.series.length >= MAX_COMPARE) { toast(`Compare up to ${MAX_COMPARE} series at once`, true); return; }
    mk.series.push({ kind: "basket", basket_id: b.id, label: b.name, slot: freeSlot() });
    saveMkState();
    loadMarketsChart();
  })] });
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
  // Drop baskets that no longer exist (e.g. deleted), and keep names current.
  mk.series = mk.series.filter((x) => x.kind !== "basket" || mkBaskets.some((b) => b.id === x.basket_id));
  mk.series.forEach((x) => { if (x.kind === "basket") x.label = mkBaskets.find((b) => b.id === x.basket_id).name; });
  if (!mk.series.length) mk.series = [{ kind: "symbol", symbol: "SPY", label: "SPY", slot: 0 }];
  const items = viewItems();
  const compare = items.length > 1;
  const single = mk.series[0];
  const indicators = compare ? [] : [...mk.overlays, ...(mk.rsi ? ["rsi:14"] : [])];
  const req = ++mkReq;
  $("#mk-chart").classList.add("loading");
  let data;
  try {
    data = await api("POST", "/api/market/series", {
      account_id: state.accountId, range: mk.range, interval: mk.interval || null, indicators,
      items: items.map((s) => (s.kind === "symbol" ? { kind: "symbol", symbol: s.symbol, corr: s.corr !== false } : { kind: "basket", basket_id: s.basket_id, corr: s.corr !== false })),
      correlation: compare ? mk.corrWindow : null,
    });
  } catch (e) {
    if (req !== mkReq) return;
    $("#mk-chart").classList.remove("loading");
    destroyMkCharts();
    $("#mk-chart").innerHTML = `<div class="empty-inline error-text">${esc(e.message)}</div>`;
    $("#mk-head").innerHTML = ""; $("#mk-stats").innerHTML = ""; $("#mk-rsi").classList.add("hidden"); hideCorrPanel();
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
  mkLastCorr = data.correlation;
  if (compare) renderCompare(data, items, intraday);
  else { hideCorrPanel(); renderSingle(data, good[0], single, intraday); }
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

function itemColor(item) {
  if (item.role === "total") return cssVar("--text");
  if (item.slot == null || item.slot >= 6) return cssVar("--muted");
  return seriesColor(item.slot);
}

function renderCompare(data, items, intraday) {
  // data.series is in the same order as the requested items.
  const pairs = data.series.map((s, i) => ({ s, item: items[i] })).filter((x) => !x.s.error);
  const good = pairs.map((x) => x.s);
  const separate = items.some((x) => x.role === "total");
  $("#mk-head").innerHTML = `<div class="mk-title"><h2>${separate ? esc(items[0].label) : "Comparison"}</h2><span class="muted">${separate ? "each stock vs the combined basket, " : ""}% change over ${esc(data.range)}</span></div>`;
  const chart = makeChart($("#mk-chart"), { height: 420, intraday, percent: true });
  mkCharts.push(chart);
  const extra = pairs.filter((x) => x.item.slot >= 6).length;
  for (const { s, item } of pairs) {
    const base = s.line[0]?.v;
    const pts = s.line.map((p) => ({ t: p.t, v: (p.v / base - 1) * 100 }));
    const faded = item.slot >= 6;
    chart.addLine(item.role === "total" ? `${s.label} (combined)` : s.label, pts,
      { color: itemColor(item), width: faded ? 1 : 2, dashed: item.role === "total", legend: !faded });
  }
  if (extra) chart.legend.dataset.extra = `+${extra} more in grey`;
  chart.chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
  chart.fit();
  chart.renderLegend();
  if (extra) chart.legend.insertAdjacentHTML("beforeend", `<span class="lg-item muted">+${extra} more in grey (see table)</span>`);
  $("#mk-rsi").classList.add("hidden");
  renderCorrPanel(chart, intraday);
  const c = mkLastCorr;
  const corrTile = c && c.average != null
    ? `<div class="corr-summary">${c.labels.length === 2 ? `Correlation of ${esc(c.labels[0])} and ${esc(c.labels[1])}` : `Average correlation between the ${c.labels.length} ${separate ? "stocks" : "series"}`} over the range:
        <b class="num">${c.average.toFixed(2)}</b> <span class="muted">(${corrWord(c.average)}, ${c.bars} bars of returns)</span>
        <button class="btn small ghost" id="mk-matrix2">View matrix</button></div>` : "";
  $("#mk-stats").innerHTML = corrTile + `<div class="table-wrap" style="grid-column:1/-1"><table><thead><tr><th>Series</th><th class="r">Change</th><th class="r">Volatility (ann.)</th><th class="r">Max drawdown</th><th class="r">Last</th></tr></thead><tbody>${
    pairs.map(({ s, item }) => `<tr><td><span class="lg-swatch" style="background:${itemColor(item)}"></span> <b>${esc(s.label)}</b>${item.role === "total" ? ' <span class="muted">(combined)</span>' : ""}</td><td class="r num">${signed(s.stats.change_pct)}</td><td class="r num">${pctPlain(s.stats.volatility)}</td><td class="r num">${pctPlain(s.stats.max_drawdown)}</td><td class="r num">${money(s.stats.last)}</td></tr>`).join("")}</tbody></table></div>`;
  const m2 = $("#mk-matrix2");
  if (m2) m2.onclick = showCorrMatrix;
  const times = good[0].line.slice(-100).reverse();
  const maps = good.map((s) => new Map(s.line.map((p) => [p.t, p.v])));
  $("#mk-table").innerHTML = `<table><thead><tr><th>Time (NY)</th>${good.map((s) => `<th class="r">${esc(s.label)}</th>`).join("")}</tr></thead><tbody>${
    times.map((p) => `<tr><td>${fmtChartTime(toChartTime(p.t), intraday)}</td>${maps.map((m) => `<td class="r num">${num2(m.get(p.t))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

/* ---------------- view items, correlation ---------------- */
let mkLastCorr = null;

/* What actually gets drawn. A basket in "each stock" mode expands to its members
   (colour slots 0-5, the rest grey) plus its combined value (excluded from correlation). */
function viewItems() {
  if (mk.series.length === 1 && mk.series[0].kind === "basket" && mk.basketMode === "separate") {
    const b = mkBaskets.find((x) => x.id === mk.series[0].basket_id);
    if (b) {
      return [{ ...mk.series[0], role: "total", corr: false },
        ...b.items.map((it, i) => ({ kind: "symbol", symbol: it.symbol, label: it.symbol, slot: i }))];
    }
  }
  return mk.series;
}
const isMultiView = () => viewItems().length > 1;

function corrWord(v) {
  const a = Math.abs(v);
  const strength = a >= 0.8 ? "very strong" : a >= 0.6 ? "strong" : a >= 0.4 ? "moderate" : a >= 0.2 ? "weak" : "little to none";
  return v < 0 && a >= 0.2 ? `${strength}, opposite directions` : strength;
}

function hideCorrPanel() { const h = $("#mk-corr"); h.classList.add("hidden"); h.innerHTML = ""; }

/* Correlation lives in its own panel under the price chart (it's on a -1..1 scale,
   so it never shares an axis with % change), with the time axes kept in step. */
function renderCorrPanel(main, intraday) {
  const c = mkLastCorr;
  if (!mk.corr || !c || !c.rolling.length) { hideCorrPanel(); return; }
  const host = $("#mk-corr");
  host.classList.remove("hidden");
  const cc = makeChart(host, { height: 140, intraday, valueFormat: (v) => v.toFixed(2) });
  mkCharts.push(cc);
  const name = c.labels.length === 2 ? `Correlation ${c.labels[0]}–${c.labels[1]} (${c.window}-bar)` : `Average correlation (${c.window}-bar, ${c.labels.length} series)`;
  const line = cc.addLine(name, c.rolling, { color: cssVar("--text") });
  line.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: -1, maxValue: 1 } }) });
  for (const [v, t] of [[0, "0"], [0.7, "0.7"], [-0.7, "-0.7"]]) {
    line.createPriceLine({ price: v, color: cssVar("--muted"), lineWidth: 1, lineStyle: v === 0 ? 0 : 2, axisLabelVisible: false, title: t });
  }
  cc.fit();
  cc.renderLegend();
  syncTimeScales(main, cc);
}

/* Diverging scale: blue (negative) - grey (0) - red (positive). */
function corrColor(v) {
  const dark = isDark();
  const mid = dark ? [56, 56, 53] : [240, 239, 236];
  const pos = dark ? [230, 103, 103] : [227, 73, 72];
  const neg = dark ? [57, 135, 229] : [42, 120, 214];
  const t = Math.min(1, Math.abs(v));
  const end = v >= 0 ? pos : neg;
  const rgb = mid.map((m, i) => Math.round(m + (end[i] - m) * t));
  return { bg: `rgb(${rgb.join(",")})`, fg: t > 0.55 ? "#fff" : "var(--text)" };
}

function showCorrMatrix() {
  const c = mkLastCorr;
  if (!c) { toast("Add at least two series to see correlations", true); return; }
  const L = c.labels;
  const body = el(`<div>
    <p class="muted" style="margin-top:0">Correlation of ${c.bars} bar-to-bar returns over the selected range (${esc(mk.range)}).
      1 = always move together, 0 = unrelated, -1 = always move opposite.</p>
    <div class="table-wrap"><table class="corr-matrix"><thead><tr><th></th>${L.map((l) => `<th class="r">${esc(l)}</th>`).join("")}</tr></thead>
      <tbody>${L.map((row, i) => `<tr><th>${esc(row)}</th>${c.matrix[i].map((v, j) => {
        if (v == null) return `<td class="r">—</td>`;
        const col = corrColor(v);
        return `<td class="r num" style="background:${col.bg};color:${col.fg}" title="${esc(row)} vs ${esc(L[j])}: ${v.toFixed(2)} (${corrWord(v)})">${v.toFixed(2)}</td>`;
      }).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="corr-legend"><span>-1 opposite</span><span class="corr-ramp"></span><span>+1 together</span></div>
    ${L.length > 2 ? `<p class="hint">Average of the pairs: <b>${c.average?.toFixed(2) ?? "—"}</b> (${c.average != null ? corrWord(c.average) : ""}).</p>` : ""}
  </div>`);
  const ramp = $(".corr-ramp", body);
  ramp.style.background = `linear-gradient(90deg, ${corrColor(-1).bg}, ${corrColor(0).bg}, ${corrColor(1).bg})`;
  modal.open({ title: "Correlation matrix", body, foot: [button("Close", "", modal.close)] });
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
