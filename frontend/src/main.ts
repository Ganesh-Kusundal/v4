// TradeX v4 terminal shell — drives the openalgo-charts-master-style chrome in
// index.html (#shellbar, .rail, #chart, #replaybar, #setmodal) while every
// computation stays in tradex_trading: bars from /api/charts/history via
// withBarCache, indicators via Tier-2 POST /api/charts/indicators/compute,
// orders via POST /orders + WS control queue, replay dual-mode.
import {
  createChart,
  darkTheme,
  ReplayController,
  TimeNavigator,
  type Bar,
  type IndicatorApi,
  type IPrimitive,
  type SeriesApi,
} from "openalgo-charts";
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
const chartFeed = createTradexFeed(); // withBarCache-wrapped DataFeed
const rawFeed = chartFeed.source; // TradexDataFeed underneath the cache
const tradeFeed = new TradexTradeFeed();

// ---------- chart -------------------------------------------------------------
const chartHost = $<HTMLDivElement>("chart");
const chart = createChart(chartHost, {
  theme: darkTheme,
  timezone: "Asia/Kolkata",
  dataFeed: chartFeed,
} as unknown as Record<string, unknown>);

let priceSeries: SeriesApi | null = null;
let volumeSeries: SeriesApi | null = null;
let transformSeries: SeriesApi | null = null;
let lastRawBars: Bar[] = [];
let transformSel: HTMLSelectElement;
let profileSel: HTMLSelectElement;
let activeProfile: { primitive: IPrimitive; setData(result: unknown): void } | null = null;

function ensureSeries(): void {
  if (priceSeries && volumeSeries) return;
  priceSeries?.remove();
  volumeSeries?.remove();
  priceSeries = chart.addSeries("candlestick");
  volumeSeries = chart.addSeries("histogram", { priceScaleId: "" });
}
ensureSeries();

// ---------- trade tier host (on-chart orders + positions from /book) ---------
const tradeHost = createTradingHost(
  chart as never,
  tradeFeed,
  (symbol: string): number | undefined => {
    // Synchronous LTP: last close of the loaded bars for the chart's symbol.
    // Other symbols degrade to entry price (flat PnL) until a live LTP source
    // lands — the trade-tier PositionMarker band / distance labels don't apply
    // to the base TradingController's pnlText anyway.
    if (symbol !== state.symbol || lastRawBars.length === 0) return undefined;
    const last = lastRawBars[lastRawBars.length - 1] as unknown as { close?: number };
    return typeof last.close === "number" ? last.close : undefined;
  },
);

// ---------- status -------------------------------------------------------------
const statusDot = document.createElement("span");
statusDot.className = "status-dot";
const statusText = document.createElement("span");
statusText.id = "status";
statusText.textContent = "ready";
const sourcePill = document.createElement("span");
sourcePill.className = "pill";
sourcePill.textContent = "datalake";
const modePill = document.createElement("span");
modePill.className = "pill live";
modePill.textContent = "LIVE";
function setStatus(on: boolean): void { statusDot.classList.toggle("on", on); }

// feed.ts WsBarHub reconnects silently; reflect liveness via first bar ack.
let wsLive = false;
setInterval(() => setStatus(wsLive), 1000);

// ---------- history load --------------------------------------------------------
async function loadHistory(): Promise<void> {
  clearProfile();
  // Drop stale order/position markers before re-syncing the current book on
  // symbol/interval change (clear() removes prior price-lines, start refetches).
  tradeHost.stop();
  void tradeHost.start();
  setIndicatorContext(state.exchange, state.symbol);
  setIndicatorInterval(state.interval);
  statusText.textContent = "loading…";
  try {
    const bars = await chartFeed.getBars({ symbol: state.symbol, exchange: state.exchange, interval: state.interval });
    lastRawBars = bars;
    resetToRaw();
    const source = (rawFeed as unknown as { lastSource?: string }).lastSource;
    sourcePill.textContent = source ?? "datalake";
    statusText.textContent = `${bars.length} bars · ${state.exchange}:${state.symbol} ${state.interval}`;
    symBtn.textContent = "";
    const b = document.createElement("b");
    b.textContent = `${state.exchange}:${state.symbol}`;
    symBtn.append(b);
    persistState();
    renderChips();
  } catch (err) {
    statusText.textContent = err instanceof Error ? err.message : String(err);
  }
}

let liveUnsub: (() => void) | null = null;
function bindLiveBars(): void {
  liveUnsub?.();
  liveUnsub =
    chartFeed.subscribeBars?.(
      { symbol: state.symbol, exchange: state.exchange, interval: state.interval },
      () => { wsLive = true; },
    ) ?? null;
}

// ---------- shellbar ------------------------------------------------------------
const shellbar = $("shellbar");
const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "D"] as const;
const catalogueById = new Map<string, CatalogueEntry>();

const symBtn = document.createElement("button");
symBtn.className = "tbtn";
symBtn.title = "Change symbol (searches GET /api/charts/symbols)";
{
  const b = document.createElement("b");
  b.textContent = `${state.exchange}:${state.symbol}`;
  symBtn.append(b);
}
symBtn.addEventListener("click", () => {
  const side = $("sidepanel");
  side.scrollIntoView({ behavior: "smooth" });
  ($("watchlist").querySelector('input[type="search"]') as HTMLInputElement | null)?.focus();
});

const brand = document.createElement("div");
brand.className = "brand";
brand.innerHTML = '<span class="brand-dot"></span> TradeX <em>v4</em>';

const divider = (): HTMLSpanElement => Object.assign(document.createElement("span"), { className: "divider" });

const pills = document.createElement("div");
pills.className = "pills";
for (const iv of INTERVALS) {
  const b = document.createElement("button");
  b.textContent = iv.toUpperCase();
  b.dataset.iv = iv;
  if (iv === state.interval) b.classList.add("is-on");
  b.addEventListener("click", () => {
    state.interval = iv;
    for (const x of Array.from(pills.children)) x.classList.toggle("is-on", (x as HTMLElement).dataset.iv === iv);
    void loadHistory();
    bindLiveBars();
  });
  pills.append(b);
}

// ---------- transforms (backend-computed chart-types) -------------------------
function resetToRaw(): void {
  clearProfile();
  ensureSeries();
  transformSeries?.remove();
  transformSeries = null;
  transformSel.value = "none";
  if (lastRawBars.length === 0) return;
  priceSeries!.applyOptions({ visible: true });
  volumeSeries!.applyOptions({ visible: true });
  priceSeries!.setData(lastRawBars as never[]);
  volumeSeries!.setData(
    (lastRawBars as unknown as { time: number; volume?: number }[]).map((b) => ({ time: b.time, value: b.volume ?? 0 })) as never[],
  );
  chart.setVisibleLogicalRange({ from: Math.max(0, lastRawBars.length - 150), to: lastRawBars.length + 5 });
}

async function applyTransform(id: string): Promise<void> {
  const t = transformById(id);
  if (!t) { resetToRaw(); return; }
  if (lastRawBars.length === 0) { transformSel.value = "none"; return; }
  clearProfile();
  statusText.textContent = "transforming…";
  try {
    const params: Record<string, number> = {};
    for (const p of t.params) params[p.key] = p.default;
    const bars = await computeTransform(t.id, params, lastRawBars);
    ensureSeries();
    transformSeries?.remove();
    transformSeries = null;
    if (t.kind === "candlestick") {
      priceSeries!.applyOptions({ visible: true });
      volumeSeries!.applyOptions({ visible: true });
      priceSeries!.setData(bars as never[]);
      volumeSeries!.setData(
        (bars as unknown as { time: number; volume?: number }[]).map((b) => ({ time: b.time, value: b.volume ?? 0 })) as never[],
      );
    } else {
      priceSeries!.applyOptions({ visible: false });
      volumeSeries!.applyOptions({ visible: false });
      transformSeries = chart.addSeries(t.kind);
      transformSeries.setData(bars as never[]);
    }
    if (bars.length > 0) {
      chart.setVisibleLogicalRange({ from: Math.max(0, bars.length - 150), to: bars.length + 5 });
    }
    statusText.textContent = `${bars.length} bars · ${t.name}`;
  } catch (err) {
    resetToRaw();
    statusText.textContent = err instanceof Error ? err.message : String(err);
  }
}

transformSel = document.createElement("select");
transformSel.className = "field field--tf";
transformSel.title = "Price series transform — computed by tradex_trading, rendered here";
{
  const none = document.createElement("option");
  none.value = "none";
  none.textContent = "Candles";
  transformSel.append(none);
  for (const t of TRANSFORMS) {
    const o = document.createElement("option");
    o.value = t.id;
    o.textContent = t.name;
    transformSel.append(o);
  }
}
transformSel.addEventListener("change", () => void applyTransform(transformSel.value));

// ---------- profiles + seasonality (backend-computed overlays) -----------------
function clearProfile(): void {
  if (activeProfile !== null) {
    chart.removePrimitive(activeProfile.primitive);
    activeProfile = null;
  }
  if (profileSel) profileSel.value = "none";
}

async function applyProfile(id: string): Promise<void> {
  const spec = PROFILES.find((p) => p.id === id);
  clearProfile();
  if (id === "seasonality") {
    if (lastRawBars.length === 0) return;
    statusText.textContent = "computing seasonality…";
    try {
      const result = await computeSeasonality(lastRawBars);
      const ren = createSeasonalityRenderer();
      ren.setData(result);
      chart.addPrimitive(ren.primitive, 0);
      activeProfile = ren;
      profileSel.value = id;
      statusText.textContent = `seasonality · ${lastRawBars.length} bars`;
    } catch (err) {
      clearProfile();
      statusText.textContent = err instanceof Error ? err.message : String(err);
    }
    return;
  }
  if (!spec || lastRawBars.length === 0) return;
  statusText.textContent = `computing ${spec.name}…`;
  try {
    const bars = lastRawBars;
    const params: Record<string, unknown> = {};
    // The backend's compute_footprint consumes classified trades, not OHLCV
    // bars (route contract: body.bars IS the trades list for footprint).
    const payload: unknown = spec.id === "footprint" ? barsToTrades(bars) : bars;
    if (spec.id === "footprint") params.time = bars[0].time;
    const result = await computeProfile(spec.id, params, payload as never);
    const ren = spec.create();
    ren.setData(result);
    chart.addPrimitive(ren.primitive, 0);
    activeProfile = ren;
    profileSel.value = spec.id;
    statusText.textContent = `${spec.name} · ${bars.length} bars`;
  } catch (err) {
    clearProfile();
    statusText.textContent = err instanceof Error ? err.message : String(err);
  }
}

profileSel = document.createElement("select");
profileSel.className = "field field--tf field--profile";
profileSel.title = "Market profile / seasonality overlay — computed by tradex_trading, rendered here";
{
  const none = document.createElement("option");
  none.value = "none";
  none.textContent = "No Profile";
  profileSel.append(none);
  for (const p of PROFILES) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = p.name;
    profileSel.append(o);
  }
  const s = document.createElement("option");
  s.value = "seasonality";
  s.textContent = "Seasonality";
  profileSel.append(s);
}
profileSel.addEventListener("change", () => void applyProfile(profileSel.value));

// Indicators menu + chips
const indWrap = document.createElement("span");
indWrap.style.display = "inline-flex";
indWrap.style.gap = "6px";
indWrap.style.alignItems = "center";
const indBtn = document.createElement("button");
indBtn.className = "tbtn";
indBtn.textContent = "Indicators";
indBtn.title = "Add indicator — catalogue served by tradex_trading analytics registry";
const indList = document.createElement("span");
indList.className = "indlist";
indWrap.append(indBtn, indList);

indBtn.addEventListener("click", () => {
  const existing = document.querySelector(".menu[data-role='ind']");
  if (existing) { existing.remove(); return; }
  const menu = document.createElement("div");
  menu.className = "menu";
  menu.dataset.role = "ind";
  const rect = indBtn.getBoundingClientRect();
  menu.style.left = `${rect.left}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  const find = document.createElement("div");
  find.className = "menu-find";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = "Filter…";
  find.append(input);
  menu.append(find);
  const listBody = document.createElement("div");
  for (const entry of catalogueById.values()) {
    const row = document.createElement("button");
    row.textContent = entry.name;
    row.dataset.name = entry.name.toLowerCase();
    row.addEventListener("click", () => {
      addIndicator(`backend:${entry.id}`);
      menu.remove();
    });
    listBody.append(row);
  }
  menu.append(listBody);
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    for (const r of Array.from(listBody.querySelectorAll("button"))) {
      (r as HTMLElement).hidden = q !== "" && !(r.dataset.name ?? "").includes(q);
    }
  });
  document.body.append(menu);
  setTimeout(() => document.addEventListener("click", function close(e) {
    if (!menu.contains(e.target as Node)) { menu.remove(); document.removeEventListener("click", close); }
  }), 0);
});

function addIndicator(descriptorId: string): void {
  const inst = chart.addIndicator(descriptorId, {
    symbol: state.symbol,
    exchange: state.exchange,
    interval: state.interval,
  } as unknown as Record<string, unknown>) as unknown as IndicatorApi;
  renderChips();
  void inst;
}

function renderChips(): void {
  indList.innerHTML = "";
  for (const inst of chart.indicators() as readonly IndicatorApi[]) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const name = document.createElement("b");
    name.textContent = inst.name.replace(/ \(backend\)$/, "");
    const gear = document.createElement("button");
    gear.textContent = "⚙";
    gear.title = "Settings";
    gear.addEventListener("click", () =>
      showIndicatorModal(inst, catalogueById.get(inst.indicatorId)));
    const eye = document.createElement("button");
    eye.textContent = inst.visible() ? "●" : "○";
    eye.title = inst.visible() ? "Hide" : "Show";
    eye.addEventListener("click", () => { inst.setVisible(!inst.visible()); renderChips(); });
    const x = document.createElement("button");
    x.textContent = "×";
    x.title = "Remove";
    x.addEventListener("click", () => { inst.remove(); renderChips(); });
    chip.append(name, eye, gear, x);
    indList.append(chip);
  }
}

// Order ticket: qty field + Buy/Sell market orders through the execution spine.
const qtyInput = document.createElement("input");
qtyInput.type = "number"; qtyInput.className = "field field--qty";
qtyInput.value = "10"; qtyInput.min = "1"; qtyInput.title = "Quantity";
function placeOrder(side: "BUY" | "SELL"): void {
  const qty = Math.max(1, Math.round(Number(qtyInput.value) || 1));
  tradeFeed.placeOrder({ symbol: state.symbol, exchange: state.exchange, side, type: "MARKET", qty })
    .then(() => logLine(`order ${side} ${qty} ${state.symbol} sent`))
    .catch((e) => logLine(`order rejected: ${e instanceof Error ? e.message : String(e)}`));
}
const buyBtn = document.createElement("button");
buyBtn.className = "tbtn tbtn--buy";
buyBtn.innerHTML = "<b>Buy</b>";
buyBtn.addEventListener("click", () => placeOrder("BUY"));
const sellBtn = document.createElement("button");
sellBtn.className = "tbtn tbtn--sell";
sellBtn.innerHTML = "<b>Sell</b>";
sellBtn.addEventListener("click", () => placeOrder("SELL"));

// Trade pill: shows/hides the on-chart order lines + position markers. The
// static button lives in index.html#shellbar; we unhide + wire it here and let
// shellbar.append() move it into place next to Buy/Sell (mirrors compare/settings).
const tradeBtn = $<HTMLButtonElement>("tradetoggle");
tradeBtn.hidden = false;
tradeBtn.classList.add("is-on");
let tradeVisible = true;
tradeBtn.addEventListener("click", () => {
  tradeVisible = !tradeVisible;
  tradeBtn.classList.toggle("is-on", tradeVisible);
  if (tradeVisible) void tradeHost.start(); else tradeHost.stop();
});

// Fit + layout save/restore (chart.getState/restoreState)
const fitBtn = document.createElement("button");
fitBtn.className = "tbtn";
fitBtn.textContent = "Fit";
fitBtn.title = "Fit all bars";
fitBtn.addEventListener("click", () => {
  (chart.timeScale as unknown as { fitContent?: () => void } | undefined)?.fitContent?.();
});
const lsave = document.createElement("button");
lsave.className = "tbtn";
lsave.textContent = "Layout ⤓";
lsave.title = "Save chart state (viewport, panes, indicators) to localStorage";
lsave.addEventListener("click", () => {
  try {
    localStorage.setItem("tradex:layout", JSON.stringify(chart.getState()));
    statusText.textContent = "layout saved";
  } catch (e) { statusText.textContent = `save failed: ${String(e)}`; }
});
const lload = document.createElement("button");
lload.className = "tbtn";
lload.textContent = "⤒ Restore";
lload.title = "Restore chart state, then rebuild series data + indicator instances";
lload.addEventListener("click", () => {
  try {
    const raw = localStorage.getItem("tradex:layout");
    if (!raw) { statusText.textContent = "no saved layout"; return; }
    const report = chart.restoreState(JSON.parse(raw));
    if (!report.applied) { statusText.textContent = "layout not applicable"; return; }
    ensureSeries();
    // Re-add indicators the state carried (restore does not recreate instances).
    const st = JSON.parse(raw) as { indicators?: { indicatorId: string; settings: Record<string, unknown> }[] };
    for (const spec of st.indicators ?? []) {
      chart.addIndicator(spec.indicatorId, spec.settings as never);
    }
    void loadHistory();
    renderChips();
    statusText.textContent = "layout restored";
  } catch (e) { statusText.textContent = `restore failed: ${String(e)}`; }
});

// Replay toggle lives in shellbar too (transport detail in #replaybar).
const rpBtn = document.createElement("button");
rpBtn.className = "tbtn";
rpBtn.innerHTML = "<span>Replay</span>";
rpBtn.title = "Bar replay (prefix-slice) / tick replay (SyntheticTickGenerator)";
rpBtn.addEventListener("click", () => {
  if (barReplay) { exitBarReplay(); return; }
  if (tickReplayActive) { stopTickReplay(); return; }
  enterBarReplay();
});

// Compare + chart settings (library controllers, Tradex bars)
const cmpBtn = document.createElement("button");
cmpBtn.className = "tbtn";
cmpBtn.textContent = "Compare";
cmpBtn.title = "Overlay a second instrument (percentage scale)";
cmpBtn.addEventListener("click", toggleCompareUi);
const setBtn = document.createElement("button");
setBtn.className = "tbtn";
setBtn.textContent = "Settings";
setBtn.title = "Chart settings (schema-driven, engine-applied)";
setBtn.addEventListener("click", toggleChartSettingsUi);
wireCompare(chart, chartFeed as never, () => ({ ...state }));
wireChartSettings(chart);

shellbar.append(
  brand, divider(),
  symBtn, divider(),
  pills, divider(),
  transformSel, divider(),
  profileSel, divider(),
  indWrap, divider(),
  rpBtn, divider(),
  fitBtn, divider(),
  lsave, lload, divider(),
  qtyInput, buyBtn, sellBtn, tradeBtn,
  divider(),
  cmpBtn, setBtn,
);
const statusWrap = document.createElement("div");
statusWrap.className = "status";
statusWrap.append(statusDot, statusText, sourcePill, modePill);
shellbar.append(statusWrap);

// ---------- draw rail -----------------------------------------------------------
const railEl = $("rail");
const magnetBox = document.createElement("input");
magnetBox.type = "checkbox";
magnetBox.checked = true;
magnetBox.id = "magnet";
createDrawRail(railEl, chart, { magnetCheckbox: magnetBox });

// ---------- legend (OHLC readout via subscribeCrosshairMove) ----------------------
const legendEl = $("legend");
// Zoom/pan navigator: library primitive, fades in near the plot bottom.
const timeNav = new TimeNavigator({ id: "tradex-nav" } as never);
chart.addPrimitive(timeNav as never);
type CrossEvt = { bar?: { open: number; high: number; low: number; close: number; volume?: number; time: number } | null; point?: { x: number; y: number } | null };
chart.subscribeCrosshairMove?.(((e: CrossEvt) => {
  (timeNav as unknown as { setPointer(p: { x: number; y: number } | null): void }).setPointer(e.point ?? null);
  const b = e.bar;
  if (!b) { legendEl.innerHTML = ""; return; }
  legendEl.innerHTML = "";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = `${state.symbol} · ${state.interval}`;
  const meta = document.createElement("span");
  meta.className = "meta";
  meta.textContent = ` O ${b.open} H ${b.high} L ${b.low} C ${b.close}${b.volume !== undefined ? ` V ${Math.round(b.volume)}` : ""}`;
  legendEl.append(name, meta);
}) as never);

// ---------- watchlist / panel / dock ----------------------------------------------
const watchlist = createWatchlist($("watchlist"), (item) => {
  state.symbol = item.symbol.toUpperCase();
  state.exchange = item.exchange.toUpperCase();
  watchlist.setActive({ symbol: state.symbol, exchange: state.exchange });
  void loadHistory();
  bindLiveBars();
}, { symbol: state.symbol, exchange: state.exchange });

const dock = createBottomDock($("bottom-dock"));
const panelHost = $("panel-host");
const ordersEl = document.createElement("div");
ordersEl.textContent = "Loading book…";
const logsEl = document.createElement("div");

tradeFeed.subscribeOrders(async () => {
  const book = await fetchBook();
  ordersEl.innerHTML = "";
  const tbl = document.createElement("table");
  tbl.className = "scanner-table";
  tbl.innerHTML = "<tr><th>Side</th><th>Symbol</th><th>Type</th><th>Qty</th><th>Price</th><th>Status</th></tr>";
  for (const o of book.orders as Record<string, unknown>[]) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${o["side"] ?? ""}</td><td>${o["symbol"] ?? ""}</td><td>${o["type"] ?? ""}</td><td>${o["qty"] ?? ""}</td><td>${o["price"] ?? ""}</td><td>${o["status"] ?? ""}</td>`;
    tbl.append(tr);
  }
  ordersEl.append(tbl);
});
tradeFeed.subscribePositions(async () => {
  const book = await fetchBook();
  logLine(`positions: ${(book.positions as unknown[]).length}`);
});
dock.setContent("orders", ordersEl);
dock.setContent("logs", logsEl);

void fetchStrategyCatalogue().then((catalogue) => {
  const host: PanelHost = {
    showEquityCurve(points) {
      const series = chart.addSeries("line", { paneIndex: chart.panes().length });
      series.setData(points.map((p) => ({ time: p.time, value: p.value })) as never[]);
      dock.select("backtest");
    },
    showTradeMarkers(trades) {
      for (const t of trades) {
        if (t.rejected || !t.price) continue;
        const trading = (chart as unknown as { trading?: { addTrade(o: unknown): void } }).trading;
        trading?.addTrade({
          id: `${t.time}-${t.side}-${t.price}`,
          side: t.side === "BUY" ? "buy" : "sell",
          price: t.price,
          size: t.qty ?? 1,
          timestamp: t.time * 1000,
          label: t.side === "BUY" ? "B" : "S",
        });
      }
    },
  };
  createStrategiesPanel(panelHost, host, catalogue, state);
  // Bottom SCANNER tab shows live results placeholder; Backtest shows equity note
  const scannerNote = document.createElement("div");
  scannerNote.style.cssText = "color:var(--muted);font:12px \"Sora\",sans-serif;padding:8px";
  scannerNote.textContent = "Run a scanner from the right panel — results appear here.";
  dock.setContent("scanner", scannerNote);
}).catch(() => { /* panel degrades; scanner/backtest still reachable via API */ });
const btNote = document.createElement("div");
btNote.style.cssText = "color:var(--muted);font:12px \"Sora\",sans-serif;padding:8px";
btNote.textContent = "Backtest results render as an equity pane plus trade markers on the chart.";
dock.setContent("backtest", btNote);

// ---------- replay -----------------------------------------------------------------
const bar2: HTMLDivElement = $("replaybar");
let barReplay: InstanceType<typeof ReplayController> | null = null;
let tickReplayActive = false;
let tickSpeed = 10;

class ControlSocket {
  private ws: WebSocket | null = null;
  private readonly handlers = new Set<(msg: Record<string, unknown>) => void>();
  connect(): void {
    if (this.ws && this.ws.readyState !== WebSocket.CLOSED && this.ws.readyState !== WebSocket.CLOSING) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    this.ws = ws;
    ws.onmessage = (ev) => {
      try { const m = JSON.parse(ev.data as string) as Record<string, unknown>; for (const h of this.handlers) h(m); }
      catch { /* ignore */ }
    };
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

// Transport built into master-styled #replaybar.
const rbPlay = document.createElement("button"); rbPlay.textContent = "▶";
const rbExit = document.createElement("button"); rbExit.textContent = "✕"; rbExit.title = "Exit replay";
const rbSpeed = document.createElement("select"); rbSpeed.className = "field";
for (const s of [1, 2, 5, 10]) { const o = document.createElement("option"); o.value = String(s); o.textContent = `${s}x`; rbSpeed.append(o); }
const rbRange = document.createElement("input"); rbRange.type = "range"; rbRange.min = "0"; rbRange.max = "0"; rbRange.value = "0";
const rbCount = document.createElement("span"); rbCount.className = "rcount"; rbCount.textContent = "0/0";
const rbClock = document.createElement("span"); rbClass(rbClock, "rclock"); rbClock.textContent = "--";
const rbSep = document.createElement("span"); rbClass(rbSep, "vsep");
const rbTick = document.createElement("button"); rbTick.textContent = "Sim ticks"; rbTick.title = "Tick-replay: SyntheticTickGenerator → BarAggregator (fill parity)";
const rbTickSpeed = document.createElement("select"); rbTickSpeed.className = "field";
for (const s of [5, 10, 20, 50]) { const o = document.createElement("option"); o.value = String(s); o.textContent = `${s}x`; o.selected = s === 10; rbTickSpeed.append(o); }
function rbClass(el: HTMLElement, cls: string): void { el.classList.add(cls); }
bar2.append(rbPlay, rbSpeed, rbRange, rbCount, rbClock, rbSep, rbTick, rbTickSpeed, rbExit);

let rbPlaying = false;
rbPlay.addEventListener("click", () => {
  if (!barReplay) return;
  if (rbPlaying) barReplay.pause(); else barReplay.play();
});
rbExit.addEventListener("click", () => { if (barReplay) exitBarReplay(); else if (tickReplayActive) stopTickReplay(); });
rbSpeed.addEventListener("change", () => { if (barReplay) barReplay.play({ speed: Number(rbSpeed.value) }); });
rbRange.addEventListener("input", () => { if (barReplay) barReplay.seek(Number(rbRange.value)); });
rbTick.addEventListener("click", () => { tickReplayActive ? stopTickReplay() : startTickReplay(); });
rbTickSpeed.addEventListener("change", () => {
  tickSpeed = Number(rbTickSpeed.value);
  if (tickReplayActive) controlSocket.send({ type: "replay_speed", speed: tickSpeed });
});

function setMode(pill: "live" | "replay" | "tick"): void {
  modePill.className = pill === "live" ? "pill live" : "pill replay";
  modePill.textContent = pill === "live" ? "LIVE" : pill === "replay" ? "REPLAY" : "TICK-REPLAY";
  rpBtn.classList.toggle("is-on", pill !== "live");
  bar2.hidden = pill === "live";
}

function enterBarReplay(): void {
  const bars = (priceSeries as unknown as { getData?(): unknown[] }).getData?.() ?? [];
  if (bars.length === 0) { statusText.textContent = "no bars to replay"; return; }
  barReplay = new ReplayController(chart as never, {
    series: [priceSeries!, volumeSeries!].filter(Boolean) as never,
    bars: bars as never,
    barMs: 700,
    speed: 1,
    onFrame: (s: { index: number; total: number; playing: boolean; speed: number; bar: { time: number } | null }) => {
      rbPlaying = s.playing;
      rbRange.max = String(Math.max(0, s.total - 1));
      rbRange.value = String(s.index);
      rbCount.textContent = `${s.index}/${s.total - 1}`;
      rbPlay.textContent = s.playing ? "❚❚" : "▶";
      rbClock.textContent = s.bar
        ? new Date(s.bar.time * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false })
        : "--";
    },
  } as never);
  setMode("replay");
}

function exitBarReplay(): void {
  barReplay?.stop();
  barReplay = null;
  rbPlaying = false;
  rbPlay.textContent = "▶";
  setMode("live");
}

function startTickReplay(): void {
  tickReplayActive = true;
  rbTick.classList.add("is-on");
  setMode("tick");
  controlSocket.send({
    type: "replay_start",
    instrument: `${state.exchange}:${state.symbol}`,
    interval: "1m",
    speed: tickSpeed,
    seed: 42,
  });
}
function stopTickReplay(): void {
  controlSocket.send({ type: "replay_stop" });
  tickReplayActive = false;
  rbTick.classList.remove("is-on");
  setMode("live");
}

controlSocket.on((msg) => {
  const t = msg["type"];
  if (t === "replay_done") {
    stopTickReplay();
    void loadHistory();
  } else if (t === "replay_stopped") {
    tickReplayActive = false;
    rbTick.classList.remove("is-on");
    setMode("live");
  } else if (t === "bar") {
    wsLive = true;
  }
});

// ---------- logs ---------------------------------------------------------------------
function logLine(s: string): void {
  const line = document.createElement("div");
  line.textContent = `${new Date().toLocaleTimeString()} ${s}`;
  logsEl.prepend(line);
  while (logsEl.childElementCount > 200) logsEl.removeChild(logsEl.lastChild!);
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
})();
void loadHistory().then(() => bindLiveBars());
