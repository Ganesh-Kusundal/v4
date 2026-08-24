// TradeX v4 terminal shell. The backend owns every computation (bars,
// indicators, strategies, backtests, scanners, replay ticks); this file is
// host chrome: it fetches, feeds the chart, and renders panels. The chart
// never consumes a DataFeed itself — this host does, then pushes bars into
// series and updates into indicators.
import {
  createChart,
  darkTheme,
  type SeriesApi,
} from "openalgo-charts";
import { createTradexFeed } from "./feed";
import { registerBackendIndicators, setIndicatorContext } from "./backend-indicators";
import { createStrategiesPanel, fetchStrategyCatalogue, type PanelHost } from "./panels/strategies";

const el = document.getElementById("app");
if (!el) throw new Error("missing #app mount point");

// Layout: toolbar / chart / right panel.
el.innerHTML = "";
const toolbar = document.createElement("div");
toolbar.id = "toolbar";
const chartHost = document.createElement("div");
chartHost.id = "chart-host";
const panelHostEl = document.createElement("div");
panelHostEl.id = "panel-host";
panelHostEl.className = "panel";
el.append(toolbar, chartHost, panelHostEl);

const chart = createChart(chartHost, {
  theme: darkTheme,
  timezone: "Asia/Kolkata",
});
let priceSeries: SeriesApi | null = null;
let volumeSeries: SeriesApi | null = null;

// ------------------------------------------------------------------ state
interface SymbolState {
  exchange: string;
  symbol: string;
  interval: string;
}
const state: SymbolState = { exchange: "NSE", symbol: "RELIANCE", interval: "5m" };

const feed = createTradexFeed();

// ------------------------------------------------------------------ toolbar
const symbolInput = document.createElement("input");
symbolInput.type = "text";
symbolInput.value = state.symbol;
symbolInput.id = "symbol-input";
const intervalSelect = document.createElement("select");
for (const iv of ["1m", "5m", "15m", "30m", "1h", "D"]) {
  const opt = document.createElement("option");
  opt.value = iv;
  opt.textContent = iv;
  if (iv === state.interval) opt.selected = true;
  intervalSelect.append(opt);
}
const statusSpan = document.createElement("span");
statusSpan.className = "status";
const replayBtn = document.createElement("button");
replayBtn.textContent = "Replay";
const indicatorSelect = document.createElement("select");
indicatorSelect.id = "indicator-select";
toolbar.append(symbolInput, intervalSelect, indicatorSelect, replayBtn, statusSpan);

async function loadHistory(): Promise<void> {
  setIndicatorContext(state.exchange, state.symbol);
  statusSpan.textContent = "loading...";
  try {
    const bars = await feed.getBars({
      symbol: state.symbol,
      exchange: state.exchange,
      interval: state.interval,
    });
    priceSeries?.remove();
    volumeSeries?.remove();
    priceSeries = chart.addSeries("candlestick");
    // Volume as a hidden-scale histogram under the price series.
    volumeSeries = chart.addSeries("histogram", { priceScaleId: "" });
    priceSeries.setData(bars);
    volumeSeries.setData(
      bars.map((b) => ({ time: b.time, value: b.volume ?? 0 })),
    );
    statusSpan.textContent = `${bars.length} bars`;
    if (bars.length > 0) chart.setVisibleLogicalRange({ from: bars.length - 150, to: bars.length + 5 });
  } catch (err) {
    statusSpan.textContent = err instanceof Error ? err.message : String(err);
  }
}

symbolInput.addEventListener("change", () => {
  state.symbol = symbolInput.value.trim().toUpperCase();
  if (state.symbol) void loadHistory();
});
intervalSelect.addEventListener("change", () => {
  state.interval = intervalSelect.value;
  void loadHistory();
});

// ---------------------------------------------------------- backend indicators
/** Catalogue entries keyed by registry id (`backend:<id>`). */
const catalogueEntries: { id: string; name: string }[] = [];

async function initIndicators(): Promise<void> {
  const entries = await registerBackendIndicators();
  catalogueEntries.push(...entries.map((e) => ({ id: e.id, name: e.name })));
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "+ Indicator";
  indicatorSelect.append(placeholder);
  for (const e of catalogueEntries) {
    const opt = document.createElement("option");
    opt.value = e.id;
    opt.textContent = e.name;
    indicatorSelect.append(opt);
  }
  indicatorSelect.addEventListener("change", () => {
    const id = indicatorSelect.value;
    if (!id) return;
    // Tier-2 descriptors fetch on attach; window comes from the chart's bars.
    chart.addIndicator(id);
    indicatorSelect.value = "";
  });
}

// ---------------------------------------------------------------- strategies
const strategyCataloguePromise = fetchStrategyCatalogue().catch(() => null);
void strategyCataloguePromise.then((catalogue) => {
  if (!catalogue) return;
  const host: PanelHost = {
    showEquityCurve(points) {
      // Equity curve in a NEW pane below the price action, not overlaid on it.
      const series = chart.addSeries("line", { paneIndex: chart.panes().length });
      series.setData(points.map((p) => ({ time: p.time, value: p.value })));
    },
    showTradeMarkers(trades) {
      for (const t of trades) {
        if (t.rejected || !t.price) continue;
        chart.trading.addTrade({
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
  createStrategiesPanel(panelHostEl, host, catalogue, state);
});

// ---------------------------------------------------------------- websocket
type WsSend = Record<string, unknown>;

class TradexSocket {
  private ws: WebSocket | null = null;
  private retryMs = 1000;
  private readonly handlers = new Set<(msg: WsSend) => void>();
  private readonly openHandlers = new Set<() => void>();

  connect(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    ws.onopen = () => {
      this.retryMs = 1000;
      // (Re)subscription hooks fire here — once per connection, never per
      // ack, or an ack-triggered subscribe would feed back into itself.
      for (const h of this.openHandlers) h();
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as WsSend;
        for (const h of this.handlers) h(msg);
      } catch {
        // malformed frame: ignore, next frame re-syncs
      }
    };
    ws.onclose = () => {
      setTimeout(() => this.connect(), this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 15000);
    };
    this.ws = ws;
  }

  send(msg: WsSend): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }

  on(h: (msg: WsSend) => void): () => void {
    this.handlers.add(h);
    return () => this.handlers.delete(h);
  }

  /** Runs on every (re)connect — the right place for subscription setup. */
  onOpen(h: () => void): () => void {
    this.openHandlers.add(h);
    return () => this.openHandlers.delete(h);
  }
}

const socket = new TradexSocket();
socket.connect();

function onBarMsg(handler: (msg: WsSend) => void): () => void {
  return socket.on((msg) => {
    if (msg["type"] === "bar") handler(msg);
  });
}

// Live forming-bar subscription follows the loaded symbol/interval. Sent on
// every socket (re)open; loadHistory re-sends when the user changes symbol.
function subscribeBars(): void {
  socket.send({
    type: "subscribe_bars",
    bars: [{ instrument: `${state.exchange}:${state.symbol}`, interval: state.interval }],
  });
}
socket.onOpen(subscribeBars);

onBarMsg((bar) => {
  if (
    bar["instrument"] !== `${state.exchange}:${state.symbol}` ||
    bar["interval"] !== state.interval ||
    !priceSeries
  ) {
    return;
  }
  const update = {
    time: Number(bar["time"]),
    open: Number(bar["open"]),
    high: Number(bar["high"]),
    low: Number(bar["low"]),
    close: Number(bar["close"]),
    v: Number(bar["volume"]),
  };
  if (bar["closed"] === true && volumeSeries) {
    priceSeries.update(update);
    volumeSeries.update({ time: update.time, value: update.v });
  } else if (priceSeries) {
    // Forming bar: update without committing volume.
    priceSeries.update(update);
  }
});

// ------------------------------------------------------------------- replay
let replayActive = false;
replayBtn.addEventListener("click", () => {
  if (replayActive) {
    socket.send({ type: "replay_stop" });
    replayBtn.textContent = "Replay";
    replayActive = false;
    return;
  }
  replayBtn.textContent = "Stop";
  replayActive = true;
  socket.send({
    type: "replay_start",
    instrument: `${state.exchange}:${state.symbol}`,
    interval: "1m",
    speed: 10,
    seed: 42,
  });
});
socket.on((msg) => {
  const t = msg["type"];
  if (t === "replay_done" || t === "replay_stopped") {
    replayBtn.textContent = "Replay";
    replayActive = false;
    if (t === "replay_done") void loadHistory(); // refresh closed history after sim
  }
});

// -------------------------------------------------------------------- boot
void loadHistory();
void initIndicators();
