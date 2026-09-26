import {
  withBarCache,
  type Bar,
  type BarSubscriptionOptions,
  type BarsPage,
  type BarsPageRequest,
  type BarsRequest,
  type DataFeed,
  type LiveBarMeta,
  type MarketDepth,
  type UnsubscribeFn,
} from 'openalgo-charts';
import { getApiKey } from './apikey';

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
  /** UTC seconds of the last bar in the returned window (absent on error). */
  last_closed_time?: number | null;
  error?: { code?: string; message?: string };
}

const anchors = new Map<string, number>();
let activeAnchorKey: string | null = null;
let anchorRequestId = 0;
let activeAnchorRequest = 0;

const anchorKey = (exchange: string, symbol: string, interval: string): string =>
  `${exchange}:${symbol}:${interval}`;

function selectAnchorKey(key: string, requestId: number): void {
  if (requestId < activeAnchorRequest) return;
  activeAnchorRequest = requestId;
  activeAnchorKey = key;
}

export const datalakeClock = (exchange?: string, symbol?: string, interval?: string): number => {
  const key = exchange !== undefined && symbol !== undefined && interval !== undefined
    ? anchorKey(exchange, symbol, interval)
    : activeAnchorKey;
  return (key === null ? undefined : anchors.get(key)) ?? Math.floor(Date.now() / 1000);
};

function noteAnchor(exchange: string, symbol: string, interval: string, body: HistoryEnvelope, requestId: number): void {
  const key = anchorKey(exchange, symbol, interval);
  selectAnchorKey(key, requestId);
  const newest = body.last_closed_time;
  if (typeof newest !== 'number' || !Number.isFinite(newest) || newest <= 0) return;
  const current = anchors.get(key);
  if (current === undefined || newest > current) anchors.set(key, newest);
}

function requestError(name: string, message: string): Error {
  const error = new Error(message);
  error.name = name;
  return error;
}

function withHistorySignal<T>(
  callerSignal: AbortSignal | undefined,
  timeoutMs: number,
  run: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  if (callerSignal?.aborted) return Promise.reject(requestError('AbortError', 'history request cancelled'));
  return new Promise<T>((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    let timer: ReturnType<typeof setTimeout>;
    const finish = (ok: boolean, value: unknown): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', onAbort);
      if (ok) resolve(value as T);
      else reject(value);
    };
    const onAbort = (): void => {
      controller.abort();
      finish(false, requestError('AbortError', 'history request cancelled'));
    };
    timer = setTimeout(() => {
      controller.abort();
      finish(false, requestError('TimeoutError', 'history request timed out'));
    }, timeoutMs);
    callerSignal?.addEventListener('abort', onAbort, { once: true });
    try {
      void run(controller.signal).then(
        (value) => finish(true, value),
        (error) => finish(false, error),
      );
    } catch (error) {
      finish(false, error);
    }
  });
}

async function fetchHistory(
  exchange: string,
  symbol: string,
  interval: string,
  from?: number,
  to?: number,
  signal?: AbortSignal,
  timeoutMs = 45_000,
): Promise<HistoryEnvelope> {
  const requestId = ++anchorRequestId;
  selectAnchorKey(anchorKey(exchange, symbol, interval), requestId);
  const fromSec = from === undefined ? undefined : Math.floor(from);
  const toSec = to === undefined ? undefined : Math.floor(to);
  const window =
    `${fromSec === undefined ? '' : `&from=${fromSec}`}${toSec === undefined ? '' : `&to=${toSec}`}`;
  try {
    return await withHistorySignal(signal, Number.isFinite(timeoutMs) && timeoutMs > 0 ? timeoutMs : 45_000, async requestSignal => {
      const res = await fetch(
        `${API_BASE}/api/charts/history/${exchange}:${symbol}?interval=${mapInterval(interval)}${window}`,
        { signal: requestSignal },
      );
      const body = (await res.json().catch((error: unknown) => {
        if (error instanceof Error && error.name === 'AbortError') throw error;
        return undefined;
      })) as HistoryEnvelope | undefined;
      if (body?.error) {
        throw new FeedError(body.error.message ?? 'history failed', body.error.code ?? 'error');
      }
      if (!res.ok) throw new FeedError(`history failed (${res.status})`);
      if (requestSignal.aborted) throw requestError('AbortError', 'history request cancelled');
      const parsed = body ?? {};
      noteAnchor(exchange, symbol, interval, parsed, requestId);
      return parsed;
    });
  } catch (err) {
    if (err instanceof Error && err.name === 'TimeoutError') {
      throw new FeedError('history request timed out', 'timeout');
    }
    throw err;
  }
}

export async function probeDatalakeAnchor(
  exchange: string,
  symbol: string,
  interval: string,
  signal?: AbortSignal,
): Promise<number | null> {
  const key = anchorKey(exchange, symbol, interval);
  try {
    await fetchHistory(
      exchange,
      symbol,
      interval,
      undefined,
      Math.floor(Date.now() / 1000) + 86_400,
      signal,
    );
    return anchors.get(key) ?? null;
  } catch (err) {
    if (signal?.aborted) throw err;
    console.warn('datalake anchor probe failed', err);
  }
  return anchors.get(key) ?? null;
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
  provisional?: boolean;
  run_id?: string | null;
}

type BarSubscriptionSeed = {
  lastClosed?: Bar;
  currentBucket?: Bar;
};

type WireBarSubscriptionOptions = BarSubscriptionOptions & {
  currentBucket?: Bar;
  seed?: BarSubscriptionSeed;
};

type BarFrameHandler = (bar: Bar, meta?: LiveBarMeta) => void;

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
  private readonly subs = new Map<string, Set<BarFrameHandler>>();
  private readonly subOptions = new Map<string, Map<BarFrameHandler, WireBarSubscriptionOptions>>();
  private readonly depths = new Map<string, Set<(depth: MarketDepth) => void>>();
  /** Caller-requested depth level per instrument, replayed on (re)open. */
  private readonly depthLevels = new Map<string, string>();
  private readonly handlers = new Map<string, Set<(msg: unknown) => void>>();
  private readonly barHandlers = new Set<BarFrameHandler>();
  private readonly openCbs = new Set<() => void>();
  private readonly closeCbs = new Set<() => void>();
  private readonly giveUpCbs = new Set<() => void>();
  /** Consecutive failed connect attempts; reset on open and on fresh user demand. */
  private attempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  /** True while closing on purpose (last ref left) — no reconnect then. */
  private intentionalClose = false;
  /** Frames sent while the handshake was still CONNECTING. */
  private readonly pendingSends: object[] = [];
  private closeTimer: ReturnType<typeof setTimeout> | null = null;

  subscribe(
    instrument: string,
    interval: string,
    onBar: BarFrameHandler,
    opts?: WireBarSubscriptionOptions,
  ): UnsubscribeFn {
    const key = `${instrument}|${interval}`;
    let set = this.subs.get(key);
    const wasEmpty = set === undefined || set.size === 0;
    if (set === undefined) {
      set = new Set();
      this.subs.set(key, set);
    }
    set.add(onBar);
    if (opts !== undefined) {
      let options = this.subOptions.get(key);
      if (options === undefined) {
        options = new Map();
        this.subOptions.set(key, options);
      }
      options.set(onBar, opts);
    }
    this.acquire();
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN && (wasEmpty || opts !== undefined)) {
      this.send(this.barSubscribeMessage(key));
    }
    // else: CONNECTING — onopen subscribes every current key.
    return () => {
      const s = this.subs.get(key);
      if (s === undefined || !s.delete(onBar)) return;
      this.subOptions.get(key)?.delete(onBar);
      if (s.size > 0) return;
      this.subs.delete(key);
      this.subOptions.delete(key);
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'unsubscribe_bars', bars: [{ instrument, interval }] });
      }
      this.release();
    };
  }

  private barSubscribeMessage(key: string): object {
    const [instrument = '', interval = ''] = key.split('|');
    const item: { instrument: string; interval: string; seed?: BarSubscriptionSeed; provisional?: boolean } = { instrument, interval };
    let selected: WireBarSubscriptionOptions | undefined;
    for (const candidate of this.subOptions.get(key)?.values() ?? []) {
      if (selected === undefined || candidate.currentBucket !== undefined || candidate.seed?.currentBucket !== undefined) selected = candidate;
      if (selected.currentBucket !== undefined || selected.seed?.currentBucket !== undefined) break;
    }
    const lastClosed = selected?.seedFrom ?? selected?.seed?.lastClosed;
    const currentBucket = selected?.currentBucket ?? selected?.seed?.currentBucket;
    if (currentBucket !== undefined) {
      item.seed = { ...(lastClosed !== undefined ? { lastClosed } : {}), currentBucket };
      item.provisional = false;
    } else if (lastClosed !== undefined) {
      item.seed = { lastClosed };
      item.provisional = true;
    } else if (selected !== undefined) {
      item.provisional = true;
    }
    return { type: 'subscribe_bars', bars: [item] };
  }

  /** Refcounted depth subscription. Paper mode only delivers a snapshot frame (broker.depth on subscribe). */
  subscribeDepth(instrument: string, onDepth: (depth: MarketDepth) => void, depthLevel = 30): UnsubscribeFn {
    let set = this.depths.get(instrument);
    if (set === undefined) {
      set = new Set();
      this.depths.set(instrument, set);
    }
    this.depthLevels.set(instrument, String(depthLevel));
    set.add(onDepth);
    this.acquire();
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN) {
      this.send({ type: 'subscribe', instruments: [instrument], depth: String(depthLevel), snapshot: true });
    }
    return () => {
      const s = this.depths.get(instrument);
      if (s === undefined || !s.delete(onDepth) || s.size > 0) return;
      this.depths.delete(instrument);
      this.depthLevels.delete(instrument);
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.send({ type: 'unsubscribe', instruments: [instrument] });
      }
      this.release();
    };
  }

  /** Listen for any frame by its wire `type` (order, fill, replay_*). Holds a socket ref. */
  on(type: 'bar', handler: BarFrameHandler): UnsubscribeFn;
  on(type: string, handler: (msg: unknown) => void): UnsubscribeFn;
  on(type: string, handler: ((msg: unknown) => void) | BarFrameHandler): UnsubscribeFn {
    if (type === 'bar') {
      const barHandler = handler as BarFrameHandler;
      this.barHandlers.add(barHandler);
      this.acquire();
      return () => {
        if (!this.barHandlers.delete(barHandler)) return;
        this.release();
      };
    }
    const messageHandler = handler as (msg: unknown) => void;
    let set = this.handlers.get(type);
    if (set === undefined) {
      set = new Set();
      this.handlers.set(type, set);
    }
    set.add(messageHandler);
    this.acquire();
    return () => {
      const s = this.handlers.get(type);
      if (s === undefined || !s.delete(messageHandler) || s.size > 0) return;
      this.handlers.delete(type);
      this.release();
    };
  }

  /** Callback for every (re)open, so connection-scoped subscriptions can re-arm. */
  onOpen(cb: () => void): UnsubscribeFn {
    this.openCbs.add(cb);
    if (this.ws !== null && this.ws.readyState === WebSocket.OPEN) {
      cb();
    }
    return () => this.openCbs.delete(cb);
  }

  /** Callback for connection close / drop. */
  onClose(cb: () => void): UnsubscribeFn {
    this.closeCbs.add(cb);
    return () => this.closeCbs.delete(cb);
  }

  /** Fired once when 10 consecutive reconnect attempts have failed. */
  onGiveUp(cb: () => void): UnsubscribeFn {
    this.giveUpCbs.add(cb);
    return () => this.giveUpCbs.delete(cb);
  }

  // ponytail: frames sent while the socket is CONNECTING queue and flush on
  // open — a send-during-handshake is normal ordering, not an error.
  send(msg: object): void {
    if (this.ws === null || this.ws.readyState === WebSocket.CONNECTING) {
      this.pendingSends.push(msg);
      return;
    }
    try {
      this.ws.send(JSON.stringify(msg));
    } catch (err) {
      console.warn('bar socket send failed', err);
    }
  }

  private acquire(): void {
    this.refs += 1;
    if (this.closeTimer !== null) {
      clearTimeout(this.closeTimer);
      this.closeTimer = null;
    }
    // A new subscription after a give-up is a fresh chance to connect.
    this.attempts = 0;
    if (this.ws === null || this.ws.readyState === WebSocket.CLOSED) this.connect();
  }

  private release(): void {
    this.refs -= 1;
    if (this.refs <= 0) {
      this.refs = 0;
      this.cancelReconnect();
      if (this.closeTimer !== null) clearTimeout(this.closeTimer);
      this.closeTimer = setTimeout(() => {
        this.closeTimer = null;
        if (this.refs <= 0) {
          this.intentionalClose = true;
          this.ws?.close();
          this.ws = null;
        }
      }, 100);
    }
  }

  private notifyResync(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      for (const key of this.subs.keys()) this.send(this.barSubscribeMessage(key));
    }
    const callbacks = new Set<() => void>();
    for (const options of this.subOptions.values()) {
      for (const value of options.values()) {
        if (value.onResync !== undefined) callbacks.add(value.onResync);
      }
    }
    for (const callback of callbacks) {
      try {
        callback();
      } catch (err) {
        console.warn('bar socket resync callback failed', err);
      }
    }
  }

  private cancelReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  /** Jittered exponential backoff: 1s base, 30s cap, ±30% jitter, ~10 tries. */
  private scheduleReconnect(): void {
    this.attempts += 1;
    if (this.attempts > 10) {
      console.error('bar socket: giving up after 10 consecutive failed reconnects');
      for (const cb of this.giveUpCbs) cb();
      return;
    }
    const base = Math.min(30_000, 1_000 * 2 ** (this.attempts - 1));
    const delay = base * (1 + 0.3 * (Math.random() * 2 - 1));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.refs > 0) this.connect();
    }, delay);
  }

  /** Auth lives in the connect URL: the server checks ?api_key pre-accept (close 1008 on mismatch). */
  private url(): string {
    const key = getApiKey();
    const inputEl = typeof document !== 'undefined' ? (document.getElementById('wsurl') as HTMLInputElement | null) : null;
    const base = inputEl?.value.trim() || `${WS_BASE}/ws/stream`;
    if (key === '') return base;
    const delim = base.includes('?') ? '&' : '?';
    return `${base}${delim}api_key=${encodeURIComponent(key)}`;
  }

  private connect(): void {
    this.cancelReconnect();
    this.intentionalClose = false;
    this.ws = new WebSocket(this.url());
    this.ws.onopen = () => {
      this.attempts = 0;
      // Re-arm every live subscription server-side: bars, depth (at the
      // remembered per-instrument levels) and orders (via openCbs).
      for (const key of this.subs.keys()) this.send(this.barSubscribeMessage(key));
      for (const instrument of this.depths.keys()) {
        this.send({ type: 'subscribe', instruments: [instrument], depth: this.depthLevels.get(instrument) ?? '30', snapshot: true });
      }
      // Frames queued during the handshake, then anything the re-arm pushed.
      for (const msg of this.pendingSends.splice(0)) this.send(msg);
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
        const bar: Bar = {
          time: msg.time,
          open: msg.open,
          high: msg.high,
          low: msg.low,
          close: msg.close,
          volume: msg.volume,
        };
        const meta: LiveBarMeta = { source: msg.source, closed: msg.closed };
        if (msg.provisional !== undefined) meta.provisional = msg.provisional;
        if (typeof msg.run_id === 'string') meta.run_id = msg.run_id;
        for (const cb of this.barHandlers) cb(bar, meta);
        if (set === undefined) return;
        for (const cb of set) cb(bar, meta);
        return;
      }
      if (isDepthFrame(msg)) {
        // Depth frames are already engine MarketDepth shape (numeric, {price,qty} levels).
        const set = this.depths.get(msg.instrument);
        if (set === undefined) return;
        for (const cb of set) cb({ bids: msg.bids, asks: msg.asks, ltp: Number(msg.ltp) });
        return;
      }
      if (typeof frame.type === 'string') {
        for (const cb of this.handlers.get(frame.type) ?? []) cb(msg);
      }
    };
    this.ws.onclose = () => {
      this.ws = null;
      this.pendingSends.length = 0;
      for (const cb of this.closeCbs) cb();
      if (this.intentionalClose || this.refs <= 0) return;
      this.notifyResync();
      if (this.intentionalClose || this.refs <= 0 || this.ws !== null) return;
      this.scheduleReconnect();
    };
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
    (typeof m.ltp === 'number' || typeof m.ltp === 'string')
  );
}

/** One shared socket for the app: the widget re-subscribes on every reload. */
export const barSocket = new BarSocket();

function toBars(body: HistoryEnvelope, from?: number, to?: number, before?: number): Bar[] {
  const bars = (Array.isArray(body.bars) ? body.bars : [])
    .filter((bar) => from === undefined || bar.time >= from)
    .filter((bar) => to === undefined || bar.time <= to)
    .filter((bar) => before === undefined || bar.time < before)
    .map((bar) => ({
      time: bar.time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
      volume: bar.volume,
    }));
  return bars.sort((left, right) => left.time - right.time);
}

function fixedIntervalSeconds(interval: string): number {
  if (interval === 'D' || interval === '1d') return 86_400;
  const match = /^(\d+)([mhd])$/i.exec(interval);
  if (match === null) return 60;
  const amount = Number(match[1]);
  const unit = match[2]!.toLowerCase();
  return amount * (unit === 'd' ? 86_400 : unit === 'h' ? 3_600 : 60);
}

export class V4DataFeed implements DataFeed {
  async getBars(req: BarsRequest): Promise<Bar[]> {
    const body = await fetchHistory(
      req.exchange,
      req.symbol,
      req.interval,
      req.from,
      req.to,
      req.signal,
      req.timeoutMs,
    );
    return toBars(body, req.from, req.to);
  }

  async getBarsPage(req: BarsPageRequest): Promise<BarsPage> {
    const count = Number.isFinite(req.countBack) ? Math.max(0, Math.floor(req.countBack)) : 0;
    const before = Math.floor(req.before);
    const to = before - 1;
    const from = to - count * fixedIntervalSeconds(req.interval) + 1;
    const body = await fetchHistory(
      req.exchange,
      req.symbol,
      req.interval,
      from,
      to,
      req.signal,
      req.timeoutMs,
    );
    const bars = toBars(body, from, to, before);
    return { bars, hasMore: count > 0 && bars.length >= count };
  }

  subscribeBars(
    req: BarsRequest,
    onBar: BarFrameHandler,
    opts?: WireBarSubscriptionOptions,
  ): UnsubscribeFn {
    return barSocket.subscribe(`${req.exchange}:${req.symbol}`, mapWsInterval(req.interval), onBar, opts);
  }

  subscribeDepth(req: BarsRequest, onDepth: (depth: MarketDepth) => void, opts?: { depthLevel?: number }): UnsubscribeFn {
    return barSocket.subscribeDepth(`${req.exchange}:${req.symbol}`, onDepth, opts?.depthLevel ?? 30);
  }
}

export const feed: DataFeed = withBarCache(new V4DataFeed(), { timezone: 'Asia/Kolkata' });
