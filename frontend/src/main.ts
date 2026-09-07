// TradeX v4 terminal shell — thin shell + command palette + overlay panels.
// Every computation stays in tradex_trading: bars from /api/charts/history via
// withBarCache, indicators via Tier-2 POST /api/charts/indicators/compute,
// orders via POST /orders + WS control queue, replay dual-mode.
import {
  createChart,
  ReplayController,
  TimeNavigator,
  type Bar,
  type IndicatorApi,
  type IPrimitive,
  type SeriesApi,
} from "openalgo-charts";
import { tradexTheme } from "./theme";
import { TRANSFORMS, computeTransform, transformById } from "./transforms";
import { PROFILES, barsToTrades, computeProfile } from "./profiles";
import { computeSeasonality, createSeasonalityRenderer } from "./seasonality";
import { createTradexFeed, registerTradexIntervals } from "./feed";
import { registerBackendIndicators, setIndicatorContext, setIndicatorInterval, type CatalogueEntry } from "./backend-indicators";
import { TradexTradeFeed, fetchBook } from "./trade-feed";
import { createTradingHost } from "./trade";
import { createStrategiesPanel, fetchStrategyCatalogue, type PanelHost } from "./panels/strategies";
import { createWatchlist } from "./shell/watchlist";
import { createDrawRail } from "./shell/draw-rail";
import { showIndicatorModal } from "./shell/indicator-modal";
import { createBottomDock } from "./shell/bottom-dock";
import { wireCompare, toggleCompareUi } from "./shell/comparison";
import { wireChartSettings, toggleChartSettingsUi } from "./shell/chart-settings";
import { linkChart } from "./linking";
import { createShellShortcuts, type ShortcutActions } from "./shortcuts";
import { Chrome, type ChromeContext } from "./primitives";
import { createCommandPalette, type PaletteAction } from "./palette";
import { createOverlay, type Overlay } from "./shell/overlay";

registerTradexIntervals();

const $ = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
};

// ---------- state + persistence ---------------------------------------------
interface SymbolState { exchange: string; symbol: string; interval: string; }
function loadState(): SymbolState {
  try {
    const url = new URL(location.href);
    const stored = JSON.parse(localStorage.getItem("tradex:state") || "null") as Partial<SymbolState> | null;
    return {
      exchange: (url.searchParams.get("exchange") || stored?.exchange || "NSE").toUpperCase(),
      symbol: (url.searchParams.get("symbol") || stored?.symbol || "RELIANCE").toUpperCase(),
      interval: url.searchParams.get("interval") || stored?.interval || "5m",
    };
  } catch { return { exchange: "NSE", symbol: "RELIANCE", interval: "5m" }; }
}
const state: SymbolState = loadState();
function persistState(): void {
  localStorage.setItem("tradex:state", JSON.stringify(state));
  const url = new URL(location.href);
  url.searchParams.set("symbol", state.symbol);
  url.searchParams.set("exchange", state.exchange);
  url.searchParams.set("interval", state.interval);
  history.replaceState(null, "", url.toString());
}

// ---------- feeds (backend-owned data plane) ---------------------------------
const chartFeed = createTradexFeed();
const tradeFeed = new TradexTradeFeed();

// ---------- chart -------------------------------------------------------------
const chartHost = $<HTMLDivElement>("chart");
let chart = createChart(chartHost, {
  theme: tradexTheme as unknown as Record<string, unknown>,
  timezone: "Asia/Kolkata",
  dataFeed: chartFeed,
  shortcuts: false,
} as unknown as Record<string, unknown>);

linkChart(chart as never);

let resizeRaf = 0;
new ResizeObserver(() => {
  if (resizeRaf) return;
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0;
    const rect = chartHost.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      chart.applyOptions({ width: rect.width, height: rect.height } as never);
    }
  });
}).observe(chartHost);

let priceSeries: SeriesApi | null = null;
let volumeSeries: SeriesApi | null = null;
let transformSeries: SeriesApi | null = null;
let lastRawBars: Bar[] = [];
let activeProfile: { primitive: IPrimitive; setData(result: unknown): void } | null = null;

function currentPrice(): number | undefined {
  if (lastRawBars.length === 0) return undefined;
  const last = lastRawBars[lastRawBars.length - 1] as unknown as { close?: number };
  return typeof last.close === "number" ? last.close : undefined;
}

const VISIBLE_BARS = 20;
const VOLUME_UP_COLOR = "#26A69A";
const VOLUME_DOWN_COLOR = "#EF5350";

function volumeBars(bars: Bar[]): Bar[] {
  return bars.map((b) => ({
    time: b.time, open: 0, high: b.volume ?? 0, low: 0, close: b.volume ?? 0, volume: b.volume,
    color: b.close >= b.open ? VOLUME_UP_COLOR : VOLUME_DOWN_COLOR,
  }));
}

function showLastBars(count: number): void {
  chart.setVisibleLogicalRange({ from: Math.max(0, count - VISIBLE_BARS), to: count + 1 });
}

function ensureSeries(): void {
  if (priceSeries && volumeSeries) return;
  priceSeries?.remove();
  volumeSeries?.remove();
  priceSeries = chart.addSeries("candlestick");
  volumeSeries = chart.addSeries("histogram", { paneIndex: 1, priceFormat: { type: "volume" } });
  chart.setPaneWeight(1, 0.25);
}

// ---------- trade tier host ---------------------------------------------------
let tradeHost!: ReturnType<typeof createTradingHost>;
let chrome!: Chrome;
let stopShortcuts: () => void = () => {};

// ---------- status -------------------------------------------------------------
const statusDot = $("status-dot");
const statusText = $("status-text");
function setStatus(on: boolean): void { statusDot.classList.toggle("on", on); }

let wsLive = false;
let tradeWsLive = false;
tradeFeed.setLiveCallback((live) => { tradeWsLive = live; });
setInterval(() => setStatus(wsLive && tradeWsLive), 1000);

// ---------- history load --------------------------------------------------------
let lastChromeCtx: ChromeContext = {
  symbol: state.symbol, exchange: state.exchange, lastPrice: 0, prevClose: 0, sessionHigh: 0, sessionLow: 0,
};
function chromeContext(): ChromeContext {
  const finite = (v: number | undefined): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);
  const last = lastRawBars[lastRawBars.length - 1];
  let hi = -Infinity; let lo = Infinity;
  for (const b of lastRawBars) {
    const h = finite(b.high); const l = finite(b.low);
    if (h > hi) hi = h; if (l < lo) lo = l;
  }
  return {
    symbol: state.symbol, exchange: state.exchange,
    lastPrice: last ? finite(last.close) : 0,
    prevClose: lastRawBars.length > 0 ? finite(lastRawBars[0].close) : 0,
    sessionHigh: hi === -Infinity ? 0 : hi, sessionLow: lo === Infinity ? 0 : lo,
  };
}

async function loadHistory(): Promise<void> {
  clearProfile();
  tradeHost.stop();
  void tradeHost.start();
  setIndicatorContext(state.exchange, state.symbol);
  setIndicatorInterval(state.interval);
  for (const inst of chart.indicators() as readonly IndicatorApi[]) {
    if (!inst.indicatorId.startsWith("backend:")) continue;
    const s = inst.settings() as Record<string, unknown>;
    if (s["symbol"] !== state.symbol || s["exchange"] !== state.exchange || s["interval"] !== state.interval) {
      inst.setSettings({ symbol: state.symbol, exchange: state.exchange, interval: state.interval } as never);
    }
  }
  statusText.textContent = "loading…";
  try {
    const bars = await chartFeed.getBars({ symbol: state.symbol, exchange: state.exchange, interval: state.interval });
    lastRawBars = bars;
    lastChromeCtx = chromeContext();
    chrome.setContext(lastChromeCtx);
    resetToRaw();
    statusText.textContent = `${bars.length} bars · ${state.exchange}:${state.symbol} ${state.interval}`;
    symBtn.textContent = `${state.exchange}:${state.symbol}`;
    persistState();
    renderChips();
  } catch (err) {
    statusText.textContent = err instanceof Error ? err.message : String(err);
  }
}

let liveUnsub: (() => void) | null = null;
function bindLiveBars(): void {
  liveUnsub?.();
  if (!priceSeries || !volumeSeries) return;
  liveUnsub = chartFeed.subscribeBars?.(
    { symbol: state.symbol, exchange: state.exchange, interval: state.interval },
    (bar: Bar) => { wsLive = true; priceSeries!.update(bar as never); volumeSeries!.update(volumeBars([bar])[0] as never); },
  ) ?? null;
}

// ---------- shellbar ------------------------------------------------------------
const symBtn = $<HTMLButtonElement>("sym-btn");
symBtn.addEventListener("click", () => watchlistOverlay.toggle());

const tfPills = $("tf-pills");
const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "D"] as const;
for (const iv of INTERVALS) {
  const b = document.createElement("button");
  b.textContent = iv.toUpperCase();
  b.dataset.iv = iv;
  if (iv === state.interval) b.classList.add("is-on");
  b.addEventListener("click", () => {
    state.interval = iv;
    for (const x of Array.from(tfPills.children)) x.classList.toggle("is-on", (x as HTMLElement).dataset.iv === iv);
    void loadHistory(); bindLiveBars();
  });
  tfPills.append(b);
}

const qtyInput = $<HTMLInputElement>("qty-input");
function placeOrder(side: "BUY" | "SELL"): void {
  const qty = Math.max(1, Math.round(Number(qtyInput.value) || 1));
  tradeHost.orderEngine.placeOrder({ symbol: state.symbol, exchange: state.exchange, side, type: "MARKET", qty })
    .then((res) => {
      if (res.ok) logLine(`order ${side} ${qty} ${state.symbol} sent (${res.clientId})`);
      else logLine(`order rejected: ${res.reason ?? "unknown"}`);
    })
    .catch((e) => logLine(`order rejected: ${e instanceof Error ? e.message : String(e)}`));
}
$("buy-btn").addEventListener("click", () => placeOrder("BUY"));
$("sell-btn").addEventListener("click", () => placeOrder("SELL"));

// ---------- transforms (backend-computed chart-types) -------------------------
function resetToRaw(): void {
  clearProfile(); ensureSeries(); transformSeries?.remove(); transformSeries = null;
  if (lastRawBars.length === 0) return;
  priceSeries!.applyOptions({ visible: true });
  volumeSeries!.applyOptions({ visible: true });
  priceSeries!.setData(lastRawBars as never[]);
  volumeSeries!.setData(volumeBars(lastRawBars) as never[]);
  showLastBars(lastRawBars.length);
}

async function applyTransform(id: string): Promise<void> {
  const t = transformById(id);
  if (!t) { resetToRaw(); return; }
  if (lastRawBars.length === 0) return;
  clearProfile();
  statusText.textContent = "transforming…";
  try {
    const params: Record<string, number> = {};
    for (const p of t.params) params[p.key] = p.default;
    const bars = await computeTransform(t.id, params, lastRawBars);
    ensureSeries(); transformSeries?.remove(); transformSeries = null;
    if (t.kind === "candlestick") {
      priceSeries!.applyOptions({ visible: true }); volumeSeries!.applyOptions({ visible: true });
      priceSeries!.setData(bars as never[]); volumeSeries!.setData(volumeBars(bars) as never[]);
    } else {
      priceSeries!.applyOptions({ visible: false }); volumeSeries!.applyOptions({ visible: false });
      transformSeries = chart.addSeries(t.kind); transformSeries.setData(bars as never[]);
    }
    if (bars.length > 0) showLastBars(bars.length);
    statusText.textContent = `${bars.length} bars · ${t.name}`;
  } catch (err) { resetToRaw(); statusText.textContent = err instanceof Error ? err.message : String(err); }
}

// ---------- profiles + seasonality (backend-computed overlays) -----------------
function clearProfile(): void {
  if (activeProfile !== null) { chart.removePrimitive(activeProfile.primitive); activeProfile = null; }
}

async function applyProfile(id: string): Promise<void> {
  const spec = PROFILES.find((p) => p.id === id);
  clearProfile();
  if (id === "seasonality") {
    if (lastRawBars.length === 0) return;
    statusText.textContent = "computing seasonality…";
    try {
      const result = await computeSeasonality(lastRawBars);
      const ren = createSeasonalityRenderer(); ren.setData(result); chart.addPrimitive(ren.primitive, 0);
      activeProfile = ren; statusText.textContent = `seasonality · ${lastRawBars.length} bars`;
    } catch (err) { clearProfile(); statusText.textContent = err instanceof Error ? err.message : String(err); }
    return;
  }
  if (!spec || lastRawBars.length === 0) return;
  statusText.textContent = `computing ${spec.name}…`;
  try {
    const bars = lastRawBars;
    const params: Record<string, unknown> = {};
    const payload: unknown = spec.id === "footprint" ? barsToTrades(bars) : bars;
    if (spec.id === "footprint") params.time = bars[0].time;
    const result = await computeProfile(spec.id, params, payload as never);
    const ren = spec.create(); ren.setData(result); chart.addPrimitive(ren.primitive, 0);
    activeProfile = ren; statusText.textContent = `${spec.name} · ${bars.length} bars`;
  } catch (err) { clearProfile(); statusText.textContent = err instanceof Error ? err.message : String(err); }
}

// ---------- indicators (backend-computed, no UI-side logic) -------------------
const catalogueById = new Map<string, CatalogueEntry>();
const indList = document.createElement("span");
indList.className = "indlist";

function addIndicator(descriptorId: string): void {
  const inst = chart.addIndicator(descriptorId, {
    symbol: state.symbol, exchange: state.exchange, interval: state.interval,
  } as unknown as Record<string, unknown>) as unknown as IndicatorApi;
  renderChips();
  void inst;
}

function renderChips(): void {
  indList.innerHTML = "";
  for (const inst of chart.indicators() as readonly IndicatorApi[]) {
    const chip = document.createElement("span"); chip.className = "chip";
    const name = document.createElement("b"); name.textContent = inst.name.replace(/ \(backend\)$/, "");
    const gear = document.createElement("button"); gear.textContent = "⚙"; gear.title = "Settings";
    gear.addEventListener("click", () => showIndicatorModal(inst, catalogueById.get(inst.indicatorId)));
    const eye = document.createElement("button"); eye.textContent = inst.visible() ? "●" : "○";
    eye.title = inst.visible() ? "Hide" : "Show";
    eye.addEventListener("click", () => { inst.setVisible(!inst.visible()); renderChips(); });
    const x = document.createElement("button"); x.textContent = "×"; x.title = "Remove";
    x.addEventListener("click", () => { inst.remove(); renderChips(); });
    chip.append(name, eye, gear, x); indList.append(chip);
  }
}

// ---------- command palette (Ctrl+K) ------------------------------------------
const palette = createCommandPalette([]);

function rebuildPalette(): void {
  const actions: PaletteAction[] = [];
  // Indicators
  for (const entry of catalogueById.values()) {
    actions.push({ id: `ind:${entry.id}`, label: `Add ${entry.name}`, category: "Indicators", execute: () => addIndicator(`backend:${entry.id}`) });
  }
  // Transforms
  for (const t of TRANSFORMS) {
    actions.push({ id: `tx:${t.id}`, label: t.name, category: "Transforms", execute: () => void applyTransform(t.id) });
  }
  // Profiles
  for (const p of PROFILES) {
    actions.push({ id: `pf:${p.id}`, label: p.name, category: "Profiles", execute: () => void applyProfile(p.id) });
  }
  actions.push({ id: `pf:seasonality`, label: "Seasonality", category: "Profiles", execute: () => void applyProfile("seasonality") });
  // View
  actions.push({ id: `view:watchlist`, label: "Toggle Watchlist", category: "View", execute: () => watchlistOverlay.toggle() });
  actions.push({ id: `view:orders`, label: "Toggle Orders Panel", category: "View", execute: () => ordersOverlay.toggle() });
  actions.push({ id: `view:strategies`, label: "Toggle Strategies Panel", category: "View", execute: () => strategiesOverlay.toggle() });
  actions.push({ id: `view:draw`, label: "Toggle Draw Rail", category: "View", execute: () => drawRailOverlay.toggle() });
  // Replay
  actions.push({ id: `rp:bar`, label: "Start Bar Replay", category: "Replay", execute: () => enterBarReplay() });
  actions.push({ id: `rp:tick`, label: "Start Tick Replay", category: "Replay", execute: () => startTickReplay() });
  actions.push({ id: `rp:exit`, label: "Exit Replay", category: "Replay", execute: () => { if (barReplay) exitBarReplay(); else if (tickReplayActive) stopTickReplay(); } });
  // Layout
  actions.push({ id: `layout:save`, label: "Save Layout", category: "Layout", execute: () => saveLayout() });
  actions.push({ id: `layout:restore`, label: "Restore Layout", category: "Layout", execute: () => restoreLayout() });
  actions.push({ id: `layout:fit`, label: "Fit Content", category: "Layout", execute: () => chart.fitContent() });
  // Compare + Settings
  actions.push({ id: `cmp:add`, label: "Add Comparison Symbol", category: "Compare", execute: () => toggleCompareUi() });
  actions.push({ id: `set:chart`, label: "Chart Settings", category: "Settings", execute: () => toggleChartSettingsUi() });
  // Trade
  actions.push({ id: `trade:buy`, label: "Buy Market", category: "Trade", execute: () => placeOrder("BUY") });
  actions.push({ id: `trade:sell`, label: "Sell Market", category: "Trade", execute: () => placeOrder("SELL") });
  actions.push({ id: `trade:toggle`, label: "Toggle On-Chart Orders", category: "Trade", execute: () => { tradeVisible = !tradeVisible; if (tradeVisible) void tradeHost.start(); else tradeHost.stop(); } });
  // Rebuild palette with new actions
  palette.close();
  Object.assign(palette, createCommandPalette(actions));
}

// ---------- layout save/restore ------------------------------------------------
function saveLayout(): void {
  try { localStorage.setItem("tradex:layout", JSON.stringify(chart.getState())); statusText.textContent = "layout saved"; }
  catch (e) { statusText.textContent = `save failed: ${String(e)}`; }
}
function restoreLayout(): void {
  try {
    const raw = localStorage.getItem("tradex:layout");
    if (!raw) { statusText.textContent = "no saved layout"; return; }
    const report = chart.restoreState(JSON.parse(raw));
    if (!report.applied) { statusText.textContent = "layout not applicable"; return; }
    ensureSeries();
    const st = JSON.parse(raw) as { indicators?: { indicatorId: string; settings: Record<string, unknown> }[] };
    for (const spec of st.indicators ?? []) chart.addIndicator(spec.indicatorId, spec.settings as never);
    void loadHistory(); renderChips(); statusText.textContent = "layout restored";
  } catch (e) { statusText.textContent = `restore failed: ${String(e)}`; }
}

// ---------- mount chart --------------------------------------------------------
setTimeout(() => mountChart(), 100);
const legendEl = $("legend");

function mountChart(): void {
  chart = createChart(chartHost, {
    theme: tradexTheme as unknown as Record<string, unknown>,
    timezone: "Asia/Kolkata", dataFeed: chartFeed, shortcuts: false,
  } as unknown as Record<string, unknown>);
  linkChart(chart as never);
  ensureSeries();
  tradeHost = createTradingHost(chart as never, tradeFeed, tradeFeed, (symbol: string): number | undefined => {
    if (symbol !== state.symbol) return undefined; return currentPrice();
  });
  wireCompare(chart, chartFeed as never, () => ({ ...state }));
  wireChartSettings(chart);
  chrome = new Chrome(chart as never);
  chrome.setOrderAction((side) => placeOrder(side));
  chrome.enable(lastChromeCtx, priceSeries);

  const timeScale = chart.timeScale;
  const panBars = (n: number): void => { const r = chart.getVisibleLogicalRange(); chart.setVisibleLogicalRange({ from: r.from + n, to: r.to + n }); };
  const zoomAtCenter = (factor: number): void => timeScale.zoomAtX(timeScale.width / 2, factor);
  stopShortcuts = createShellShortcuts({
    panLeft: () => panBars(-10), panRight: () => panBars(10),
    panLeftFast: () => panBars(-50), panRightFast: () => panBars(50),
    panUp: () => chart.panes()[0]?.priceScale.panByPixels(20),
    panDown: () => chart.panes()[0]?.priceScale.panByPixels(-20),
    zoomIn: () => zoomAtCenter(1.1), zoomOut: () => zoomAtCenter(1 / 1.1),
    resetScale: () => showLastBars(lastRawBars.length), fitContent: () => chart.fitContent(),
    screenshot: () => chart.downloadScreenshot(),
    toggleGridVert: () => chart.setGridOptions({ vertLines: !chart.gridOptions().vertLines }),
    toggleGridHorz: () => chart.setGridOptions({ horzLines: !chart.gridOptions().horzLines }),
    toggleCrosshairMagnet: () => chart.applyOptions({ crosshairMode: chart.crosshairMode() === "magnet" ? "normal" : "magnet" }),
    togglePalette: () => palette.isOpen() ? palette.close() : palette.open(),
    toggleWatchlist: () => watchlistOverlay.toggle(),
    toggleOrders: () => ordersOverlay.toggle(),
    toggleStrategies: () => strategiesOverlay.toggle(),
    toggleDrawRail: () => drawRailOverlay.toggle(),
  } satisfies ShortcutActions).start();
  window.addEventListener("beforeunload", stopShortcuts);

  const railEl = document.createElement("div");
  createDrawRail(railEl, chart, { magnetCheckbox: document.createElement("input") });
  drawRailOverlay = createOverlay({ position: 'left', content: railEl });

  const timeNav = new TimeNavigator({ id: "tradex-nav" } as never);
  chart.addPrimitive(timeNav as never);
  type CrossEvt = { bar?: { open: number; high: number; low: number; close: number; volume?: number; time: number } | null; point?: { x: number; y: number } | null };
  chart.subscribeCrosshairMove?.(((e: CrossEvt) => {
    (timeNav as unknown as { setPointer(p: { x: number; y: number } | null): void }).setPointer(e.point ?? null);
    const b = e.bar;
    if (!b) { legendEl.innerHTML = ""; return; }
    legendEl.innerHTML = "";
    const name = document.createElement("span"); name.className = "name"; name.textContent = `${state.symbol} · ${state.interval}`;
    const meta = document.createElement("span"); meta.className = "meta";
    meta.textContent = ` O ${b.open} H ${b.high} L ${b.low} C ${b.close}${b.volume !== undefined ? ` V ${Math.round(b.volume)}` : ""}`;
    legendEl.append(name, meta);
  }) as never);

  const syncChartSize = (): void => { const { width, height } = chartHost.getBoundingClientRect(); if (width > 0 && height > 0) chart.applySize(Math.round(width), Math.round(height)); };
  requestAnimationFrame(() => requestAnimationFrame(syncChartSize));
  window.addEventListener("resize", syncChartSize);

  void loadHistory().then(() => bindLiveBars());
}
mountChart();

// ---------- overlay panels -----------------------------------------------------
const watchlistEl = document.createElement("div");
const watchlistOverlay = createOverlay({ position: 'right', width: 280, content: watchlistEl });
const watchlist = createWatchlist(watchlistEl, (item) => {
  state.symbol = item.symbol.toUpperCase(); state.exchange = item.exchange.toUpperCase();
  watchlist.setActive({ symbol: state.symbol, exchange: state.exchange });
  void loadHistory(); bindLiveBars();
}, { symbol: state.symbol, exchange: state.exchange });

const ordersEl = document.createElement("div");
const ordersOverlay = createOverlay({ position: 'bottom', height: 140, content: ordersEl });
const dock = createBottomDock(ordersEl);
const logsEl = document.createElement("div");

tradeFeed.subscribeOrders(async () => {
  const book = await fetchBook();
  const contentEl = document.createElement("div");
  const tbl = document.createElement("table"); tbl.className = "scanner-table";
  tbl.innerHTML = "<tr><th>Side</th><th>Symbol</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th></tr>";
  for (const o of book.orders as Record<string, unknown>[]) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${o["side"] ?? ""}</td><td>${o["symbol"] ?? ""}</td><td>${o["type"] ?? ""}</td><td>${o["qty"] ?? ""}</td><td>${o["price"] ?? ""}</td><td>${o["status"] ?? ""}</td>`;
    tbl.append(tr);
  }
  contentEl.append(tbl);
  ordersEl.innerHTML = ""; ordersEl.append(contentEl);
});
tradeFeed.subscribePositions(async () => { const book = await fetchBook(); logLine(`positions: ${(book.positions as unknown[]).length}`); });
dock.setContent("orders", ordersEl);
dock.setContent("logs", logsEl);

const strategiesEl = document.createElement("div");
const strategiesOverlay = createOverlay({ position: 'right', width: 280, content: strategiesEl });
void fetchStrategyCatalogue().then((catalogue) => {
  const host: PanelHost = {
    showEquityCurve(points) { const series = chart.addSeries("line", { paneIndex: chart.panes().length }); series.setData(points.map((p) => ({ time: p.time, value: p.value })) as never[]); dock.select("backtest"); },
    showTradeMarkers(trades) { for (const t of trades) { if (t.rejected || !t.price) continue; const trading = (chart as unknown as { trading?: { addTrade(o: unknown): void } }).trading; trading?.addTrade({ id: `${t.time}-${t.side}-${t.price}`, side: t.side === "BUY" ? "buy" : "sell", price: t.price, size: t.qty ?? 1, timestamp: t.time * 1000, label: t.side === "BUY" ? "B" : "S" }); } },
  };
  createStrategiesPanel(strategiesEl, host, catalogue, state);
}).catch(() => { /* panel degrades; strategies still reachable via API */ });

// Draw rail overlay (created in mountChart)
let drawRailOverlay: Overlay;

// ---------- replay -----------------------------------------------------------------
const bar2: HTMLDivElement = $("replaybar");
let barReplay: InstanceType<typeof ReplayController> | null = null;
let tickReplayActive = false;
let tickSpeed = 10;
let tradeVisible = true;

class ControlSocket {
  private ws: WebSocket | null = null;
  private readonly handlers = new Set<(msg: Record<string, unknown>) => void>();
  connect(): void {
    if (this.ws && this.ws.readyState !== WebSocket.CLOSED && this.ws.readyState !== WebSocket.CLOSING) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    this.ws = ws;
    ws.onmessage = (ev) => { try { const m = JSON.parse(ev.data as string) as Record<string, unknown>; for (const h of this.handlers) h(m); } catch { /* ignore */ } };
  }
  send(msg: Record<string, unknown>): void {
    this.connect();
    if (this.ws?.readyState === WebSocket.OPEN) { this.ws.send(JSON.stringify(msg)); return; }
    window.setTimeout(() => { if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg)); }, 300);
  }
  on(h: (msg: Record<string, unknown>) => void): void { this.handlers.add(h); }
}
const controlSocket = new ControlSocket();
controlSocket.connect();

const rbPlay = document.createElement("button"); rbPlay.textContent = "▶";
const rbExit = document.createElement("button"); rbExit.textContent = "✕"; rbExit.title = "Exit replay";
const rbSpeed = document.createElement("select");
for (const s of [1, 2, 5, 10]) { const o = document.createElement("option"); o.value = String(s); o.textContent = `${s}x`; rbSpeed.append(o); }
const rbRange = document.createElement("input"); rbRange.type = "range"; rbRange.min = "0"; rbRange.max = "0"; rbRange.value = "0";
const rbCount = document.createElement("span"); rbCount.className = "rcount"; rbCount.textContent = "0/0";
const rbClock = document.createElement("span"); rbClock.className = "rclock"; rbClock.textContent = "--";
const rbTick = document.createElement("button"); rbTick.textContent = "Sim ticks"; rbTick.title = "Tick-replay: SyntheticTickGenerator → BarAggregator (fill parity)";
const rbTickSpeed = document.createElement("select");
for (const s of [5, 10, 20, 50]) { const o = document.createElement("option"); o.value = String(s); o.textContent = `${s}x`; o.selected = s === 10; rbTickSpeed.append(o); }
bar2.append(rbPlay, rbSpeed, rbRange, rbCount, rbClock, rbTick, rbTickSpeed, rbExit);

let rbPlaying = false;
rbPlay.addEventListener("click", () => { if (!barReplay) return; if (rbPlaying) barReplay.pause(); else barReplay.play(); });
rbExit.addEventListener("click", () => { if (barReplay) exitBarReplay(); else if (tickReplayActive) stopTickReplay(); });
rbSpeed.addEventListener("change", () => { if (barReplay) barReplay.play({ speed: Number(rbSpeed.value) }); });
rbRange.addEventListener("input", () => { if (barReplay) barReplay.seek(Number(rbRange.value)); });
rbTick.addEventListener("click", () => { tickReplayActive ? stopTickReplay() : startTickReplay(); });
rbTickSpeed.addEventListener("change", () => { tickSpeed = Number(rbTickSpeed.value); if (tickReplayActive) controlSocket.send({ type: "replay_speed", speed: tickSpeed }); });

function enterBarReplay(): void {
  const bars = (priceSeries as unknown as { getData?(): unknown[] }).getData?.() ?? [];
  if (bars.length === 0) { statusText.textContent = "no bars to replay"; return; }
  barReplay = new ReplayController(chart as never, {
    series: [priceSeries!, volumeSeries!].filter(Boolean) as never, bars: bars as never, barMs: 700, speed: 1,
    onFrame: (s: { index: number; total: number; playing: boolean; speed: number; bar: { time: number } | null }) => {
      rbPlaying = s.playing; rbRange.max = String(Math.max(0, s.total - 1)); rbRange.value = String(s.index);
      rbCount.textContent = `${s.index}/${s.total - 1}`; rbPlay.textContent = s.playing ? "❚❚" : "▶";
      rbClock.textContent = s.bar ? new Date(s.bar.time * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) : "--";
    },
  } as never);
}
function exitBarReplay(): void { barReplay?.stop(); barReplay = null; rbPlaying = false; rbPlay.textContent = "▶"; }
function startTickReplay(): void {
  tickReplayActive = true; rbTick.classList.add("is-on");
  controlSocket.send({ type: "replay_start", instrument: `${state.exchange}:${state.symbol}`, interval: "1m", speed: tickSpeed, seed: 42 });
}
function stopTickReplay(): void { controlSocket.send({ type: "replay_stop" }); tickReplayActive = false; rbTick.classList.remove("is-on"); }

controlSocket.on((msg) => {
  const t = msg["type"];
  if (t === "replay_done") { stopTickReplay(); void loadHistory(); }
  else if (t === "replay_stopped") { tickReplayActive = false; rbTick.classList.remove("is-on"); }
  else if (t === "bar") { wsLive = true; }
});

// ---------- logs ---------------------------------------------------------------------
function logLine(s: string): void {
  const line = document.createElement("div"); line.textContent = `${new Date().toLocaleTimeString()} ${s}`;
  logsEl.prepend(line); while (logsEl.childElementCount > 200) logsEl.removeChild(logsEl.lastChild!);
}
controlSocket.on((msg) => {
  const t = String(msg["type"] ?? "");
  if (["replay_start_ack", "replay_pause_ack", "replay_resume_ack", "replay_speed_ack", "replay_stop_ack"].includes(t)) logLine(`ws:${t}`);
});

// ---------- boot -----------------------------------------------------------------------
void (async () => {
  const entries = await registerBackendIndicators();
  catalogueById.clear();
  for (const e of entries) catalogueById.set(`backend:${e.id}`, e);
  rebuildPalette();
})();
