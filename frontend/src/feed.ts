// TradexDataFeed — maps openalgo-charts' DataFeed contract onto the v4
// backend's /api/charts/history + /ws/stream live bars. Standardized against
// openalgo-charts-master/src/feed/cache.ts + openalgo-live.ts: history is
// cached via withBarCache (forming bar never cached), live bars delivered
// through DataFeed.subscribeBars so ReplayController and withBarCache freshness
// both work without a second code path.
import {
  registerInterval,
  withBarCache,
  type Bar,
  type BarsRequest,
  type DataFeed,
  type UnsubscribeFn,
} from "openalgo-charts";
import { expectJson } from "./http";

// Interval codes the backend serves (see chart_api.INTERVAL_TIMEFRAME).
// 'M' (monthly) is deliberately NOT registered: neither side has calendar
// month bucketing yet, and the library refuses to guess that M means 30 days.
const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "D"] as const;
export type IntervalCode = (typeof INTERVALS)[number];

export function registerTradexIntervals(): void {
  for (const code of INTERVALS) {
    const seconds = intervalSeconds(code);
    registerInterval({ code, bucketing: { mode: "interval", seconds } });
  }
}

function intervalSeconds(code: IntervalCode): number {
  if (code === "D") return 86400;
  return Number(code.slice(0, -1)) * 60;
}

export interface HistoryResponse {
  symbol: string;
  exchange: string;
  interval: string;
  source: "datalake" | "broker" | "none";
  last_closed_time: number | null;
  bars: Bar[];
}

// --- WebSocket bar multiplex (one socket, many subscribeBars callers) --------
type BarCb = (bar: Bar) => void;
interface Sub { key: string; req: BarsRequest; cbs: Set<BarCb> }

function subKey(req: BarsRequest): string {
  return `${req.exchange}:${req.symbol}:${req.interval}`;
}

class WsBarHub {
  private ws: WebSocket | null = null;
  private readonly subs = new Map<string, Sub>();
  private retryMs = 1000;
  private connectTimer: number | null = null;

  ensureConnected(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    this.open();
  }

  private open(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    // The server injects the configured key into <meta name="x-api-key">;
    // dev/paper serves it empty and the WS connects unauthenticated.
    const key =
      document.querySelector('meta[name="x-api-key"]')?.getAttribute("content") ?? "";
    const qs = key ? `?api_key=${encodeURIComponent(key)}` : "";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream${qs}`);
    this.ws = ws;
    ws.onopen = () => {
      this.retryMs = 1000;
      this.pushSubscriptions();
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as Record<string, unknown>;
        if (msg["type"] !== "bar") return;
        const key = `${msg["instrument"]}:${msg["interval"]}`;
        const sub = this.subs.get(key);
        if (!sub) return;
        const bar: Bar = {
          time: Number(msg["time"]),
          open: Number(msg["open"]),
          high: Number(msg["high"]),
          low: Number(msg["low"]),
          close: Number(msg["close"]),
          volume: Number((msg["volume"] as number) ?? 0),
        };
        for (const cb of sub.cbs) cb(bar);
      } catch { /* malformed frame: ignore */ }
    };
    ws.onclose = () => {
      this.ws = null;
      if (this.subs.size === 0) return;
      this.connectTimer = window.setTimeout(() => this.open(), this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 15000);
    };
  }

  private pushSubscriptions(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    if (this.subs.size === 0) return;
    this.ws.send(JSON.stringify({
      type: "subscribe_bars",
      bars: Array.from(this.subs.values()).map((s) => ({
        instrument: `${s.req.exchange}:${s.req.symbol}`,
        interval: s.req.interval,
      })),
    }));
  }

  subscribe(req: BarsRequest, cb: BarCb): UnsubscribeFn {
    const key = subKey(req);
    let entry = this.subs.get(key);
    if (!entry) {
      entry = { key, req, cbs: new Set() };
      this.subs.set(key, entry);
    }
    entry.cbs.add(cb);
    this.ensureConnected();
    // Defer push until next tick so coalesced multi-pane subscriptions batch.
    queueMicrotask(() => this.pushSubscriptions());

    return () => {
      const e = this.subs.get(key);
      if (!e) return;
      e.cbs.delete(cb);
      if (e.cbs.size === 0) {
        this.subs.delete(key);
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: "unsubscribe_bars", bars: [{ instrument: `${req.exchange}:${req.symbol}`, interval: req.interval }] }));
        }
      }
      if (this.subs.size === 0 && this.connectTimer !== null) {
        clearTimeout(this.connectTimer);
        this.connectTimer = null;
      }
    };
  }
}

const hub = new WsBarHub();

export class TradexDataFeed implements DataFeed {
  async getBars(req: BarsRequest): Promise<Bar[]> {
    const params = new URLSearchParams({
      interval: req.interval,
    });
    if (req.from !== undefined) params.set("from", String(req.from));
    if (req.to !== undefined) params.set("to", String(req.to));
    const resp = await fetch(
      `/api/charts/history/${encodeURIComponent(req.exchange)}:${encodeURIComponent(req.symbol)}?${params}`,
    );
    const body = await expectJson<HistoryResponse>(resp);
    return body.bars.filter((b) => Number.isFinite(b.time));
  }

  subscribeBars(req: BarsRequest, onBar: (bar: Bar) => void): UnsubscribeFn {
    return hub.subscribe(req, onBar);
  }
}

/** Feed wrapped in the library's warm-load cache (forming bar never cached).
 *  Returns the BarCache instance so hosts can reach `.source` (the raw feed)
 *  and stats/clear for the shellbar cache badge, mirroring master examples. */
export function createTradexFeed(): import("openalgo-charts").DataFeed & {
  source: TradexDataFeed;
  stats?: () => { entries: number; bars: number; hits: number; misses: number; evictions: number };
  clear?: () => void;
} {
  registerTradexIntervals();
  const cached = withBarCache(new TradexDataFeed(), { ttlMs: 60_000 }) as import("openalgo-charts").DataFeed & {
    source?: TradexDataFeed;
    stats?: () => { entries: number; bars: number; hits: number; misses: number; evictions: number };
    clear?: () => void;
  };
  if (!cached.source) (cached as { source?: TradexDataFeed }).source = undefined as unknown as TradexDataFeed;
  return cached as import("openalgo-charts").DataFeed & { source: TradexDataFeed };
}
