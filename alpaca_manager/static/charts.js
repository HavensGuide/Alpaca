/* Chart helpers on top of TradingView Lightweight Charts (vendored, Apache-2.0).
   - Theme-aware (reads CSS tokens, re-themes when the OS switches light/dark)
   - Times shown in New York market time
   - An HTML legend per chart that shows each series' value under the crosshair */
"use strict";

const LWC = window.LightweightCharts;
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
const isDark = () => document.documentElement.dataset.theme === "dark" ||
  (document.documentElement.dataset.theme !== "light" && darkQuery.matches);

/* Categorical series colours, fixed order (validated light + dark; see README). */
const SERIES = {
  light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#6250d6"],
  dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"],
};
const seriesColor = (slot) => SERIES[isDark() ? "dark" : "light"][slot % 6];
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ---- time: show New York wall-clock on the axis ---------------------- */
const _offsetCache = new Map();
function nyOffset(t) {
  const key = Math.floor(t / 3600);
  if (_offsetCache.has(key)) return _offsetCache.get(key);
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", hourCycle: "h23", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(t * 1000)).map((x) => [x.type, x.value]));
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000;
  const off = asUtc - t;
  _offsetCache.set(key, off);
  return off;
}
const toChartTime = (t) => t + nyOffset(t);
const fromChartTime = (ct) => ct - nyOffset(ct); // close enough for labels

function fmtChartTime(ct, intraday) {
  const d = new Date(ct * 1000); // already shifted to NY wall-clock, read as UTC
  const opts = intraday
    ? { timeZone: "UTC", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }
    : { timeZone: "UTC", year: "numeric", month: "short", day: "numeric" };
  return d.toLocaleString(undefined, opts);
}

/* Chart points must have strictly increasing times; keep the last value per time. */
function toSeries(points) {
  const out = [];
  for (const p of points) {
    const time = toChartTime(p.t);
    if (out.length && out[out.length - 1].time >= time) {
      if (out[out.length - 1].time === time) out[out.length - 1].value = p.v;
      continue;
    }
    out.push({ time, value: p.v });
  }
  return out;
}

/* ---- chart factory --------------------------------------------------- */
const _charts = new Set();

function chartTheme() {
  return {
    layout: { background: { type: "solid", color: "transparent" }, textColor: cssVar("--muted"),
      fontFamily: getComputedStyle(document.body).fontFamily, fontSize: 12 },
    grid: { vertLines: { color: cssVar("--grid"), style: 0 }, horzLines: { color: cssVar("--grid"), style: 0 } },
    rightPriceScale: { borderColor: cssVar("--border") },
    leftPriceScale: { borderColor: cssVar("--border") },
    timeScale: { borderColor: cssVar("--border") },
    crosshair: {
      vertLine: { color: cssVar("--muted"), labelBackgroundColor: cssVar("--text"), width: 1, style: 3 },
      horzLine: { color: cssVar("--muted"), labelBackgroundColor: cssVar("--text"), width: 1, style: 3 },
    },
  };
}

/**
 * Create a chart in `host`. Returns a wrapper with:
 *   addLine(name, points, {slot|color, width, priceFormat, dashed})
 *   addCandles(name, candles), addVolume(candles), addArea(name, points, {...})
 *   setLegendExtra(html), fit(), destroy()
 * `points` are [{t: unixSeconds, v}]; candles [{t,o,h,l,c,v}].
 */
function makeChart(host, { height = 360, intraday = false, valueFormat = (v) => money(v), percent = false } = {}) {
  host.innerHTML = "";
  host.classList.add("chart-host");
  const legend = el(`<div class="chart-legend" aria-live="off"></div>`);
  const box = el(`<div class="chart-canvas" style="height:${height}px"></div>`);
  host.append(legend, box);
  const chart = LWC.createChart(box, {
    autoSize: true,
    ...chartTheme(),
    localization: { priceFormatter: (v) => (percent ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : valueFormat(v)), timeFormatter: (t) => fmtChartTime(t, intraday) },
    timeScale: { ...chartTheme().timeScale, timeVisible: intraday, secondsVisible: false, rightOffset: 2, fixLeftEdge: true, fixRightEdge: true },
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
  });
  // Same axis width on every chart so stacked panels line up vertically.
  chart.priceScale("right").applyOptions({ minimumWidth: 84 });
  const entries = []; // {name, series, color, kind, data(map)}

  const w = {
    chart, host, legend,
    addLine(name, points, { slot = 0, color, width = 2, dashed = false, legend: showLegend = true, scale } = {}) {
      const c = color || seriesColor(slot);
      const s = chart.addLineSeries({ color: c, lineWidth: width, lineStyle: dashed ? 2 : 0, priceLineVisible: false,
        lastValueVisible: showLegend, crosshairMarkerRadius: 4, priceScaleId: scale || "right" });
      s.setData(toSeries(points));
      entries.push({ name, series: s, color: c, slot, color0: color, kind: "line", points, showLegend });
      return s;
    },
    addArea(name, points, { color, topAlpha = 0.28 } = {}) {
      const c = color || seriesColor(0);
      const s = chart.addAreaSeries({ lineColor: c, topColor: hexA(c, topAlpha), bottomColor: hexA(c, 0.02), lineWidth: 2, priceLineVisible: false });
      s.setData(toSeries(points));
      entries.push({ name, series: s, color: c, color0: color, kind: "line", points, showLegend: true });
      return s;
    },
    addCandles(name, candles) {
      const up = cssVar("--up"), down = cssVar("--down");
      const s = chart.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down, priceLineVisible: false });
      s.setData(candles.map((b) => ({ time: toChartTime(b.t), open: b.o, high: b.h, low: b.l, close: b.c })));
      entries.push({ name, series: s, kind: "candle", candles, showLegend: true });
      return s;
    },
    addVolume(candles) {
      const s = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
      chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
      const up = hexA(cssVar("--up"), 0.35), down = hexA(cssVar("--down"), 0.35);
      s.setData(candles.map((b) => ({ time: toChartTime(b.t), value: b.v || 0, color: b.c >= b.o ? up : down })));
      entries.push({ name: "Volume", series: s, kind: "volume", candles, showLegend: true });
      chart.priceScale("right").applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });
      return s;
    },
    fit() { chart.timeScale().fitContent(); },
    destroy() { _charts.delete(w); chart.remove(); },
    retheme() {
      chart.applyOptions(chartTheme());
      for (const e of entries) {
        if (e.kind === "line" && !e.color0) { e.color = seriesColor(e.slot); e.series.applyOptions({ color: e.color }); }
        if (e.kind === "candle") {
          const up = cssVar("--up"), down = cssVar("--down");
          e.series.applyOptions({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
        }
      }
      renderLegend(null);
    },
  };

  const fmt = (v) => (v == null ? "—" : percent ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : valueFormat(v));
  function lastOf(e) {
    if (e.kind === "candle") return e.candles[e.candles.length - 1];
    if (e.kind === "volume") return e.candles[e.candles.length - 1]?.v;
    return e.points[e.points.length - 1]?.v;
  }
  function renderLegend(param) {
    let when = "";
    const parts = entries.filter((e) => e.showLegend).map((e) => {
      let val;
      if (param && param.time !== undefined) {
        const d = param.seriesData.get(e.series);
        if (e.kind === "candle") val = d ? { o: d.open, h: d.high, l: d.low, c: d.close } : null;
        else val = d ? d.value : null;
      } else {
        const l = lastOf(e);
        val = e.kind === "candle" ? l : l;
      }
      if (e.kind === "candle") {
        if (!val) return "";
        const chg = val.c - val.o;
        return `<span class="lg-item"><b>${esc(e.name)}</b> <span class="lg-k">O</span>${fmt(val.o)} <span class="lg-k">H</span>${fmt(val.h)} <span class="lg-k">L</span>${fmt(val.l)} <span class="lg-k">C</span>${fmt(val.c)} <span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "▲" : "▼"}</span></span>`;
      }
      if (e.kind === "volume") return `<span class="lg-item"><span class="lg-k">Vol</span> ${val == null ? "—" : Number(val).toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 })}</span>`;
      return `<span class="lg-item"><span class="lg-swatch" style="background:${e.color}"></span>${esc(e.name)} <b class="num">${fmt(val)}</b></span>`;
    });
    if (param && param.time !== undefined) when = `<span class="lg-time">${fmtChartTime(param.time, intraday)}</span>`;
    legend.innerHTML = when + parts.join("");
  }
  chart.subscribeCrosshairMove((param) => renderLegend(param && param.time !== undefined ? param : null));
  w.renderLegend = () => renderLegend(null);
  _charts.add(w);
  return w;
}

function hexA(color, alpha) {
  const c = color.trim();
  if (c.startsWith("#") && (c.length === 7 || c.length === 4)) {
    const h = c.length === 4 ? c.slice(1).split("").map((x) => x + x).join("") : c.slice(1);
    const n = parseInt(h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
  }
  return c;
}

/* Keep a secondary chart's time axis in step with a main chart (e.g. RSI under price).
   Synced by time, not bar position, so panels whose series start later still line up. */
function syncTimeScales(a, b) {
  let lock = false;
  const link = (src, dst) => src.chart.timeScale().subscribeVisibleTimeRangeChange((r) => {
    if (lock || !r) return;
    lock = true;
    try { dst.chart.timeScale().setVisibleRange(r); } catch { /* range outside dst's data */ }
    lock = false;
  });
  link(a, b);
  link(b, a);
}

const onThemeChange = () => { _charts.forEach((c) => c.retheme()); };
darkQuery.addEventListener("change", onThemeChange);

/* Compact number formats for stat tiles */
const compact = (v) => (v == null ? "—" : Number(v).toLocaleString(undefined, { notation: "compact", maximumFractionDigits: 1 }));
const pctPlain = (v, d = 1) => (v == null || isNaN(v) ? "—" : `${(v * 100).toFixed(d)}%`);
const num2 = (v) => (v == null || isNaN(v) ? "—" : Number(v).toFixed(2));

/* A stat tile; `tone` = "up" | "down" | "" colours only the value's arrow, not the text. */
function tile(label, value, sub = "", hint = "") {
  return `<div class="tile"${hint ? ` title="${esc(hint)}"` : ""}><div class="tile-label">${label}</div><div class="tile-value num">${value}</div>${sub ? `<div class="tile-sub">${sub}</div>` : ""}</div>`;
}
const signed = (v, f = pct) => (v == null ? "—" : `<span class="${signClass(v)}">${f(v)}</span>`);
