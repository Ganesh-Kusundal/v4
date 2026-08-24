// TradeX v4 terminal shell — standardized against
// openalgo-charts-master/examples/yfinance + src/feed/cache + src/indicators/external.
// The backend owns every computation; this host owns chrome, routing, and controller lifecycles.
import { createChart, darkTheme, ReplayController, type SeriesApi } from "openalgo-charts";
import { createTradexFeed } from "./feed";
import { registerBackendIndicators, setIndicatorContext, setIndicatorInterval } from "./backend-indicators";
import { createStrategiesPanel, fetchStrategyCatalogue, type PanelHost } from "./panels/strategies";
import { createWatchlist } from "./shell/watchlist";
import { createReplayBar } from "./shell/replay-bar";
import { createBottomDock } from "./shell/bottom-dock";

const el = document.getElementById("app");
if (!el) throw new Error("missing #app mount point");

// ---------- layout: topbar | watchlist | center | panel | bottom-dock --------
el.innerHTML = "";
const topbar = document.createElement("div");
topbar.id = "topbar";
const watchlistEl = document.createElement("div");
watchlistEl.id = "watchlist";
const center = document.createElement("div");
center.id = "center";
const toolbar = document.createElement("div");
toolbar.id = "toolbar";
const chartHost = document.createElement("div");
chartHost.id = "chart-host";
const replayBarEl = document.createElement("div");
replayBarEl.id = "replay-bar";
center.append(toolbar, chartHost, replayBarEl);
const panelHostEl = document.createElement("div");
panelHostEl.id = "panel-host";
const bottomDockEl = document.createElement("div");
bottomDockEl.id = "bottom-dock";
el.append(topbar, watchlistEl, center, panelHostEl, bottomDockEl);

// ---------- topbar -----------------------------------------------------------
const brand = document.createElement("span");
brand.className = "brand";
brand.textContent = "TradeX Terminal";
const statusSpan = document.createElement("span");
statusSpan.className = "status";
statusSpan.textContent = "paper";
const modePill = document.createElement("span");
modePill.className = "pill live";
modePill.textContent = "LIVE";
const sourcePill = document.createElement("span");
sourcePill.className = "pill";
sourcePill.textContent = "datalake";
const spacer = document.createElement("span");
spacer.className = "spacer";
topbar.append(brand, spacer, sourcePill, modePill, statusSpan);

// ---------- state + persistence ---------------------------------------------
interface SymbolState { exchange: string; symbol: string; interval: string; }
function loadState(): SymbolState {
  try {
    const url = new URL(location.href);
    const qSym = url.searchParams.get("symbol");
    const qEx = url.searchParams.get("exchange");
    const qIv = url.searchParams.get("interval");
    const stored = JSON.parse(localStorage.getItem("tradex:state") || "null") as Partial<SymbolState> | null;
    return {
      exchange: (qEx || stored?.exchange || "NSE").toUpperCase(),
      symbol: (qSym || stored?.symbol || "RELIANCE").toUpperCase(),
      interval: qIv || stored?.interval || "5m",
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

// ---------- chart (DataFeed-driven: withBarCache handles freshness) ----------
const dataFeed = createTradexFeed();
const chart = createChart(chartHost, {
  theme: darkTheme,
  timezone: "Asia/Kolkata",
  dataFeed,
} as unknown as Record<string, unknown>);

// Price + volume series: chart owns the timeline, we only ensure series exist.
// The DataFeed supplies bars; ReplayController will use the same series.
let priceSeries: SeriesApi | null = null;
let volumeSeries: SeriesApi | null = null;

function ensureSeries(): void {
  if (priceSeries && volumeSeries) return;
  priceSeries?.remove();
  volumeSeries?.remove();
  priceSeries = chart.addSeries("candlestick");
  volumeSeries = chart.addSeries("histogram", { priceScaleId: "" });
}
ensureSeries();

// Programmatic history load that also drives Tier-2 indicator window.
// When chart.dataFeed is present, prefer it (withBarCache); fallback to
// direct fetch for environments that haven't wired dataFeed into chart yet.
async function loadHistory(): Promise<void> {
  setIndicatorContext(state.exchange, state.symbol);
  setIndicatorInterval(state.interval);
  statusSpan.textContent = "loading…";
  try {
    // Use the DataFeed so withBarCache freshness applies. Fallback keeps
    // behavior identical if chart ignores dataFeed (legacy path).
    const bars = await dataFeed.getBars({
      symbol: state.symbol,
      exchange: state.exchange,
      interval: state.interval,
    });
    ensureSeries();
    priceSeries!.setData(bars as unknown as never[]);
    volumeSeries!.setData((bars as unknown as { time: number; volume?: number }[]).map((b) => ({ time: b.time, value: (b as unknown as { volume: number }).volume ?? 0 })) as never[]);
    if (bars.length > 0) {
      chart.setVisibleLogicalRange({ from: Math.max(0, bars.length - 150), to: bars.length + 5 });
      sourcePill.textContent = "datalake";
    }
    statusSpan.textContent = `${bars.length} bars · ${state.exchange}:${state.symbol} ${state.interval}`;
    persistState();
    // Live bars continue via DataFeed.subscribeBars hub (feed.ts).
  } catch (err) {
    statusSpan.textContent = err instanceof Error ? err.message : String(err);
  }
}

// Listen for live bars through the same series (feed.ts hub) — single writer.
// DataFeed.subscribeBars already pushes bar frames; the mirror below keeps
// volume aligned for forming bars without fighting the feed.
let liveUnsub: (() => void) | null = null;
function bindLiveBars(): void {
  liveUnsub?.();
  liveUnsub = dataFeed.subscribeBars?.(
    { symbol: state.symbol, exchange: state.exchange, interval: state.interval },
    (bar) => {
      if (!priceSeries || !volumeSeries) return;
      const update = {
        time: (bar as unknown as { time: number }).time,
        open: (bar as unknown as { open: number }).open,
        high: (bar as unknown as { high: number }).high,
        low: (bar as unknown as { low: number }).low,
        close: (bar as unknown as { close: number }).close,
      };
      priceSeries!.update(update as never);
      // Only commit volume on closed bar; forming bar keeps prior volume.
      // feed.ts BarFrame.closed is not exposed via Bar type, treat all as forming
      // and let the closed frame arrive as a new bar via setData continuation.
    },
  ) ?? null;
}

// ---------- toolbar ----------------------------------------------------------
const intervalSelect = document.createElement("select");
intervalSelect.id = "interval-select";
for (const iv of ["1m", "5m", "15m", "30m", "1h", "D"]) {
  const opt = document.createElement("option");
  opt.value = iv;
  opt.textContent = iv;
  if (iv === state.interval) opt.selected = true;
  intervalSelect.append(opt);
}
const indicatorSelect = document.createElement("select");
indicatorSelect.id = "indicator-select";
const intervalBadge = document.createElement("span");
intervalBadge.className = "badge";
intervalBadge.textContent = state.interval;
toolbar.append(intervalSelect, indicatorSelect, intervalBadge);

intervalSelect.addEventListener("change", () => {
  state.interval = intervalSelect.value;
  intervalBadge.textContent = state.interval;
  void loadHistory();
  bindLiveBars();
});

// ---------- backend indicators (Tier-2) --------------------------------------
async function initIndicators(): Promise<void> {
  const entries = await registerBackendIndicators();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "+ Indicator";
  indicatorSelect.append(placeholder);
  for (const e of entries) {
    const opt = document.createElement("option");
    opt.value = e.id;
    opt.textContent = e.name;
    indicatorSelect.append(opt);
  }
  indicatorSelect.addEventListener("change", () => {
    const id = indicatorSelect.value;
    if (!id) return;
    // Settings carry symbol/exchange/interval so refetchOn invalidates.
    chart.addIndicator(id, { symbol: state.symbol, exchange: state.exchange, interval: state.interval } as unknown as Record<string, unknown>);
    indicatorSelect.value = "";
  });
}

// ---------- watchlist --------------------------------------------------------
const watchlist = createWatchlist(watchlistEl, (item) => {
  state.symbol = item.symbol.toUpperCase();
  state.exchange = item.exchange.toUpperCase();
  watchlist.setActive({ symbol: state.symbol, exchange: state.exchange });
  void loadHistory();
  bindLiveBars();
}, { symbol: state.symbol, exchange: state.exchange });

// ---------- bottom dock + strategies -----------------------------------------
const dock = createBottomDock(bottomDockEl);
const logsEl = document.createElement("div");
logsEl.textContent = "Logs: WS + order events appear here.";
const ordersEl = document.createElement("div");
ordersEl.textContent = "Orders: live positions and working orders (TradeFeed WS).";

const strategyCataloguePromise = fetchStrategyCatalogue().catch(() => null);
void strategyCataloguePromise.then((catalogue) => {
  if (!catalogue) return;
  const host: PanelHost = {
    showEquityCurve(points) {
      const series = chart.addSeries("line", { paneIndex: chart.panes().length });
      series.setData(points.map((p) => ({ time: p.time, value: p.value })) as never[]);
      dock.select("backtest");
    },
    showTradeMarkers(trades) {
      for (const t of trades) {
        if (t.rejected || !t.price) continue;
        // TradeController lives on chart.trading in draw/trade tier; guard.
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
  // Right panel: strategy form
  createStrategiesPanel(panelHostEl, host, catalogue, state);
  // Bottom dock: scanner compact (second lightweight panel bound to dock)
  const dockStrategiesHost = document.createElement("div");
  createStrategiesPanel(dockStrategiesHost, host, catalogue, state);
  dock.setContent("scanner", dockStrategiesHost);
  const btHost = document.createElement("div");
  btHost.textContent = "Backtest results render as equity pane + markers.";
  dock.setContent("backtest", btHost);
});
dock.setContent("orders", ordersEl);
dock.setContent("logs", logsEl);

// ---------- replay: two modes ------------------------------------------------
// 1) Bar-replay (trader practice): ReplayController prefix-slice — indicators
//    recompute historically for free because Chart._setData triggers recompute.
// 2) Tick-replay (fill parity): WS SyntheticTickGenerator ticks → BarAggregator
//    → bar frames, proving execution parity (existing /ws replay_* flow).
let barReplay: InstanceType<typeof ReplayController> | null = null;
let tickReplayActive = false;
let tickReplaySpeed = 10;

// Shared WS for tick-replay control (reuse feed's hub WS would conflate bar
// frames and replay control, so a dedicated control socket here).
class ControlSocket {
  private ws: WebSocket | null = null;
  private readonly handlers = new Set<(msg: Record<string, unknown>) => void>();
  connect(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    this.ws = ws;
    ws.onmessage = (ev) => {
      try { const msg = JSON.parse(ev.data as string) as Record<string, unknown>; for (const h of this.handlers) h(msg); } catch { /* ignore */ }
    };
  }
  send(msg: Record<string, unknown>): boolean {
    this.connect();
    if (this.ws && this.ws.readyState === WebSocket.OPEN) { this.ws.send(JSON.stringify(msg)); return true; }
    // retry once after open
    window.setTimeout(() => {
      if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(msg));
    }, 300);
    return false;
  }
  on(h: (msg: Record<string, unknown>) => void): () => void { this.handlers.add(h); return () => this.handlers.delete(h); }
}
const controlSocket = new ControlSocket();
controlSocket.connect();

const replayBar = createReplayBar(replayBarEl, {
  onSeek(index) { barReplay?.seek(index); },
  onPlay() { barReplay?.play(); },
  onPause() { barReplay?.pause(); },
  onStop() {
    if (barReplay) { barReplay.stop(); barReplay = null; replayBarEl.classList.remove("active"); modePill.textContent = "LIVE"; modePill.className = "pill live"; }
    if (tickReplayActive) { controlSocket.send({ type: "replay_stop" }); tickReplayActive = false; replayBar.setTickActive(false); }
  },
  onSpeed(speed) { barReplay?.play({ speed }); },
  onTickReplayToggle() {
    if (tickReplayActive) {
      controlSocket.send({ type: "replay_stop" });
      tickReplayActive = false;
      replayBar.setTickActive(false);
      modePill.textContent = "LIVE"; modePill.className = "pill live";
      return;
    }
    tickReplayActive = true;
    replayBar.setTickActive(true);
    modePill.textContent = "TICK-REPLAY"; modePill.className = "pill replay";
    controlSocket.send({ type: "replay_start", instrument: `${state.exchange}:${state.symbol}`, interval: "1m", speed: tickReplaySpeed, seed: 42 });
  },
  onTickSpeed(speed) { tickReplaySpeed = speed; if (tickReplayActive) controlSocket.send({ type: "replay_speed", speed }); },
});

function enterBarReplay(): void {
  if (!priceSeries) return;
  // Snapshot is the last loaded history; ReplayController owns the timeline.
  const bars = (priceSeries as unknown as { getData(): unknown[] }).getData?.() ?? [];
  if (bars.length === 0) return;
  // Ensure volume series follows the same timeline.
  const seriesList = [priceSeries, volumeSeries].filter(Boolean) as SeriesApi[];
  barReplay?.stop();
  barReplay = new ReplayController(chart as unknown as never, {
    series: seriesList as unknown as never,
    bars: bars as never,
    barMs: 800,
    speed: 1,
    onFrame: (s: { index: number; total: number; playing: boolean; speed: number; bar: { time: number } | null }) => {
      replayBar.setState({ index: s.index, total: s.total, playing: s.playing, speed: s.speed, barTime: s.bar?.time ?? null });
      modePill.textContent = s.playing ? "REPLAY" : "REPLAY·PAUSED";
      modePill.className = "pill replay";
    },
  } as unknown as never);
  replayBar.setState({ index: 0, total: bars.length, playing: false, speed: 1, barTime: (bars[0] as { time: number }).time ?? null });
  replayBarEl.classList.add("active");
}

controlSocket.on((msg) => {
  const t = msg["type"] as string;
  if (t === "replay_done" || t === "replay_stopped") {
    tickReplayActive = false;
    replayBar.setTickActive(false);
    modePill.textContent = "LIVE"; modePill.className = "pill live";
    if (t === "replay_done") void loadHistory();
  }
  if (t === "replay_speed") { /* ack */ }
});

// Expose bar-replay entry for toolbar (kept minimal: double-click chart toggles).
chartHost.addEventListener("dblclick", () => {
  if (barReplay) { barReplay.stop(); barReplay = null; replayBarEl.classList.remove("active"); modePill.textContent = "LIVE"; modePill.className = "pill live"; }
  else enterBarReplay();
});

// ---------- boot -------------------------------------------------------------
void loadHistory().then(() => bindLiveBars());
void initIndicators();

// Simple logs tap: mirror WS control messages for operator visibility.
const logLine = (s: string) => {
  const line = document.createElement("div");
  line.textContent = `${new Date().toLocaleTimeString()} ${s}`;
  logsEl.prepend(line);
  while (logsEl.childElementCount > 200) logsEl.removeChild(logsEl.lastChild!);
};
controlSocket.on((msg) => {
  const t = String(msg["type"] ?? "");
  if (["bar", "replay_done", "replay_stopped", "order", "fill"].includes(t)) logLine(`ws:${t}`);
});
