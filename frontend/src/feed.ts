import { withBarCache, type Bar, type BarsRequest, type DataFeed, type UnsubscribeFn } from 'openalgo-charts';

// # ponytail: same-origin when FastAPI serves us, dev-server proxy target when vite runs on :5173
export const API_BASE = typeof location === 'undefined' || location.port !== '5173' ? '' : 'http://127.0.0.1:8000';
// # ponytail: ws twin of API_BASE (same port logic; no TLS termination locally)
export const WS_BASE =
  typeof location === 'undefined' || location.port !== '5173'
    ? `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`
    : 'ws://127.0.0.1:8000';

/** The request did not complete or the backend refused it. `code` is the backend's. */
export class FeedError extends Error {
  code: string;
  constructor(message: string, code = 'error') {
    super(message);
    this.name = 'FeedError';
    this.code = code;
  }
}

/** No bars for this symbol and range. Retrying will not help. */
export class NotFoundError extends FeedError {
  constructor(message: string) {
    super(message, 'not_found');
    this.name = 'NotFoundError';
  }
}

/** Engine token -> backend interval param (`1m|5m|15m|30m|1h|D`). */
const mapInterval = (interval: string): string => (interval === 'D' || interval === '1d' ? 'D' : interval);

interface RawBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

interface HistoryEnvelope {
  bars?: RawBar[];
  error?: { code?: string; message?: string };
}

/** Engine token -> WS interval param. Backend Timeframe values are 1m|5m|15m|30m|1h|1d. */
const mapWsInterval = (interval: string): string => (interval === 'D' ? '1d' : interval);

interface WsBarFrame {
  type: 'bar';
  instrument: string;
  interval: string;
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  closed: boolean;
  source: string;
}

/**
 * One WebSocket for every bar subscription (openalgo-charts src/widget/widget.ts
 * calls subscribeBars on every reload). Lazy-connect on first subscriber,
 * refcount, close when the last one leaves. Frames ride /ws/stream's
 * server-side bar aggregators; we only fan them out per (instrument, interval).
 */
class BarSocket {
  private ws: WebSocket | null = null;
  private readonly subs = new Map<string, Set<(bar: Bar) => void>>();

  subscribe(instrument: string, interval: string, onBar: (bar: Bar) => void): UnsubscribeFn {
    const key = `${instrument}|${interval}`;
    let set = this.subs.get(key);
    if (set === undefined) {
      set = new Set();
      this.subs.set(key, set);
    }
    set.add(onBar);
    if (this.ws === null || this.ws.readyState === WebSocket.CLOSED) this.connect();
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN) {
      this.send({ type: 'subscribe_bars', bars: [{ instrument, interval }] });
    }
    // else: CONNECTING — onopen subscribes every current key.
    return () => {
      const s = this.subs.get(key);
      if (s === undefined || !s.delete(onBar) || s.size > 0) return;
      this.subs.delete(key);
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'unsubscribe_bars', bars: [{ instrument, interval }] });
      }
      if (this.subs.size === 0) {
        this.ws?.close();
        this.ws = null;
      }
    };
  }

  private send(msg: object): void {
    try {
      this.ws?.send(JSON.stringify(msg));
    } catch (err) {
      console.warn('bar socket send failed', err);
    }
  }

  private connect(): void {
    this.ws = new WebSocket(`${WS_BASE}/ws/stream`);
    this.ws.onopen = () => {
      for (const key of this.subs.keys()) {
        const [instrument, interval] = key.split('|');
        this.send({ type: 'subscribe_bars', bars: [{ instrument, interval }] });
      }
    };
    this.ws.onmessage = (ev) => {
      let msg: unknown;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      if (typeof msg === 'object' && msg !== null && (msg as { type?: unknown }).type === 'error') {
        console.warn('bar socket error frame', msg);
        return;
      }
      if (!isBarFrame(msg)) return;
      const set = this.subs.get(`${msg.instrument}|${msg.interval}`);
      if (set === undefined) return;
      const bar: Bar = {
        time: msg.time,
        open: msg.open,
        high: msg.high,
        low: msg.low,
        close: msg.close,
        volume: msg.volume,
      };
      for (const cb of set) cb(bar);
    };
    // # ponytail: add jittered reconnect when live reliability demands it
  }
}

function isBarFrame(msg: unknown): msg is WsBarFrame {
  return (
    typeof msg === 'object' &&
    msg !== null &&
    (msg as { type?: unknown }).type === 'bar' &&
    typeof (msg as { time?: unknown }).time === 'number'
  );
}

/** One shared socket for the app: the widget re-subscribes on every reload. */
const barSocket = new BarSocket();

export class V4DataFeed implements DataFeed {
  async getBars(req: BarsRequest): Promise<Bar[]> {
    const from = req.from === undefined ? '' : `&from=${req.from}`;
    const to = req.to === undefined ? '' : `&to=${req.to}`;
    const res = await fetch(
      `${API_BASE}/api/charts/history/${req.exchange}:${req.symbol}?interval=${mapInterval(req.interval)}${from}${to}`,
    );
    const body = (await res.json().catch(() => undefined)) as HistoryEnvelope | undefined;
    if (body?.error) {
      throw new FeedError(body.error.message ?? 'history failed', body.error.code ?? 'error');
    }
    if (!res.ok) throw new FeedError(`history failed (${res.status})`);
    const raw = Array.isArray(body?.bars) ? body.bars : [];
    if (raw.length === 0) throw new NotFoundError(`${req.exchange}:${req.symbol}: no bars for this range`);
    return raw.map((b) => ({
      time: b.time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
    }));
  }

  subscribeBars(req: BarsRequest, onBar: (bar: Bar) => void): UnsubscribeFn {
    return barSocket.subscribe(`${req.exchange}:${req.symbol}`, mapWsInterval(req.interval), onBar);
  }
}

export const feed: DataFeed = withBarCache(new V4DataFeed(), { timezone: 'Asia/Kolkata' });
