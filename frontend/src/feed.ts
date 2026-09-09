import {
  withBarCache,
  type Bar,
  type BarsRequest,
  type DataFeed,
  type MarketDepth,
  type UnsubscribeFn,
} from 'openalgo-charts';

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
export const mapWsInterval = (interval: string): string => (interval === 'D' ? '1d' : interval);

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

interface WsDepthFrame {
  type: 'depth';
  instrument: string;
  bids: { price: number; qty: number }[];
  asks: { price: number; qty: number }[];
  ltp: number;
}

/**
 * One WebSocket for every subscription (openalgo-charts src/widget/widget.ts
 * calls subscribeBars on every reload; ladder/replay/orders share this socket).
 * Lazy-connect on first subscriber, refcount across ALL subscription kinds,
 * close when the last one leaves. Frames ride /ws/stream's server-side
 * aggregators; we fan them out per key (bars, depth) or per message type.
 */
class BarSocket {
  private ws: WebSocket | null = null;
  private refs = 0;
  private readonly subs = new Map<string, Set<(bar: Bar) => void>>();
  private readonly depths = new Map<string, Set<(depth: MarketDepth) => void>>();
  private readonly handlers = new Map<string, Set<(msg: unknown) => void>>();
  private readonly openCbs = new Set<() => void>();

  subscribe(instrument: string, interval: string, onBar: (bar: Bar) => void): UnsubscribeFn {
    const key = `${instrument}|${interval}`;
    let set = this.subs.get(key);
    if (set === undefined) {
      set = new Set();
      this.subs.set(key, set);
    }
    set.add(onBar);
    this.acquire();
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
      this.release();
    };
  }

  /** Refcounted depth subscription. Paper mode only delivers a snapshot frame (broker.depth on subscribe). */
  subscribeDepth(instrument: string, onDepth: (depth: MarketDepth) => void, depthLevel = 30): UnsubscribeFn {
    let set = this.depths.get(instrument);
    if (set === undefined) {
      set = new Set();
      this.depths.set(instrument, set);
    }
    set.add(onDepth);
    this.acquire();
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN) {
      this.send({ type: 'subscribe', instruments: [instrument], depth: String(depthLevel), snapshot: true });
    }
    return () => {
      const s = this.depths.get(instrument);
      if (s === undefined || !s.delete(onDepth) || s.size > 0) return;
      this.depths.delete(instrument);
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'unsubscribe', instruments: [instrument] });
      }
      this.release();
    };
  }

  /** Listen for any frame by its wire `type` (order, fill, replay_*). Holds a socket ref. */
  on(type: string, handler: (msg: unknown) => void): UnsubscribeFn {
    let set = this.handlers.get(type);
    if (set === undefined) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(handler);
    this.acquire();
    return () => {
      const s = this.handlers.get(type);
      if (s === undefined || !s.delete(handler) || s.size > 0) return;
      this.handlers.delete(type);
      this.release();
    };
  }

  /** Callback for every (re)open, so connection-scoped subscriptions can re-arm. */
  onOpen(cb: () => void): UnsubscribeFn {
    this.openCbs.add(cb);
    return () => this.openCbs.delete(cb);
  }

  send(msg: object): void {
    try {
      this.ws?.send(JSON.stringify(msg));
    } catch (err) {
      console.warn('bar socket send failed', err);
    }
  }

  private acquire(): void {
    this.refs += 1;
    if (this.ws === null || this.ws.readyState === WebSocket.CLOSED) this.connect();
  }

  private release(): void {
    this.refs -= 1;
    if (this.refs <= 0) {
      this.ws?.close();
      this.ws = null;
    }
  }

  private connect(): void {
    this.ws = new WebSocket(`${WS_BASE}/ws/stream`);
    this.ws.onopen = () => {
      for (const key of this.subs.keys()) {
        const [instrument, interval] = key.split('|');
        this.send({ type: 'subscribe_bars', bars: [{ instrument, interval }] });
      }
      for (const instrument of this.depths.keys()) {
        this.send({ type: 'subscribe', instruments: [instrument], depth: '30', snapshot: true });
      }
      for (const cb of this.openCbs) cb();
    };
    this.ws.onmessage = (ev) => {
      let msg: unknown;
      try {
        msg = JSON.parse(String(ev.data));
      } catch {
        return;
      }
      const frame = msg as { type?: unknown };
      if (typeof msg === 'object' && msg !== null && frame.type === 'error') {
        console.warn('bar socket error frame', msg);
        for (const cb of this.handlers.get('error') ?? []) cb(msg);
        return;
      }
      if (isBarFrame(msg)) {
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
        return;
      }
      if (isDepthFrame(msg)) {
        // Depth frames are already engine MarketDepth shape (numeric, {price,qty} levels).
        const set = this.depths.get(msg.instrument);
        if (set === undefined) return;
        for (const cb of set) cb({ bids: msg.bids, asks: msg.asks, ltp: msg.ltp });
        return;
      }
      if (typeof frame.type === 'string') {
        for (const cb of this.handlers.get(frame.type) ?? []) cb(msg);
      }
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

function isDepthFrame(msg: unknown): msg is WsDepthFrame {
  const m = msg as Partial<WsDepthFrame> | null;
  return (
    typeof m === 'object' &&
    m !== null &&
    m.type === 'depth' &&
    typeof m.instrument === 'string' &&
    Array.isArray(m.bids) &&
    Array.isArray(m.asks) &&
    typeof m.ltp === 'number'
  );
}

/** One shared socket for the app: the widget re-subscribes on every reload. */
export const barSocket = new BarSocket();

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

  subscribeDepth(req: BarsRequest, onDepth: (depth: MarketDepth) => void, opts?: { depthLevel?: number }): UnsubscribeFn {
    return barSocket.subscribeDepth(`${req.exchange}:${req.symbol}`, onDepth, opts?.depthLevel ?? 30);
  }
}

export const feed: DataFeed = withBarCache(new V4DataFeed(), { timezone: 'Asia/Kolkata' });
