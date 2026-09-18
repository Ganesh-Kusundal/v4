import {
  withBarCache,
  type Bar,
  type BarsRequest,
  type DataFeed,
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

/** Newest bar time the backend has actually served, in UTC seconds.
 *
 * The widget derives its load window from a clock: `lookbackBars` back from
 * "now". Wall-clock "now" is the wrong anchor whenever the datalake lags —
 * overnight, over a weekend, or on any day not yet synced — because the
 * window then lands entirely *after* the newest bar and every intraday
 * interval renders empty ("no bars for this range") while daily still works
 * by spanning years. The backend already reports the newest bar it served, so
 * the host hands the widget that instant instead and the window always ends
 * on data that exists.
 */
let anchorSec: number | null = null;

/** Clock the chart's load window is derived from (falls back to wall clock). */
export const datalakeClock = (): number => anchorSec ?? Math.floor(Date.now() / 1000);

/** Remember the newest served bar from any history response. */
function noteAnchor(body: HistoryEnvelope): void {
  if (typeof body.last_closed_time === 'number' && body.last_closed_time > 0) {
    anchorSec = body.last_closed_time;
  }
}

/** One history request, unwrapped. Shared by `getBars` and the anchor probe. */
async function fetchHistory(
  exchange: string,
  symbol: string,
  interval: string,
  from?: number,
  to?: number,
): Promise<HistoryEnvelope> {
  // The engine's paging math produces fractional epochs (`to` like
  // 1788839099.999999). Bars are whole seconds and the endpoint types these as
  // `int`, so a float 422s — which the UI surfaces as "Could not load older
  // history" for a page that actually has bars. Normalize at the wire edge.
  const fromSec = from === undefined ? undefined : Math.floor(from);
  const toSec = to === undefined ? undefined : Math.floor(to);
  const window =
    `${fromSec === undefined ? '' : `&from=${fromSec}`}${toSec === undefined ? '' : `&to=${toSec}`}`;
  let res: Response;
  try {
    res = await fetch(
      `${API_BASE}/api/charts/history/${exchange}:${symbol}?interval=${mapInterval(interval)}${window}`,
      { signal: AbortSignal.timeout(45_000) },
    );
  } catch (err) {
    if (err instanceof Error && (err.name === 'TimeoutError' || err.name === 'AbortError')) {
      throw new FeedError('history request timed out', 'timeout');
    }
    throw err;
  }
  const body = (await res.json().catch(() => undefined)) as HistoryEnvelope | undefined;
  if (body?.error) {
    throw new FeedError(body.error.message ?? 'history failed', body.error.code ?? 'error');
  }
  if (!res.ok) throw new FeedError(`history failed (${res.status})`);
  const parsed = body ?? {};
  noteAnchor(parsed);
  return parsed;
}

/** Learn the datalake's newest bar before the widget computes its first window.
 *
 * `last_closed_time` is the last bar *in the returned window*, so one
 * unbounded request makes it the newest bar that exists. Best-effort: with no
 * anchor the clock falls back to wall clock and the chart behaves exactly as
 * it did before this probe existed.
 *
 * Returns **this probe's own** answer, not the shared `anchorSec`: the global is
 * written by every response, and a paged-in older window reports an older
 * `last_closed_time`, so the global can sit below the newest bar. Callers that
 * need the series *identity* — the workspace fingerprint check — need the exact
 * value for the instrument they asked about. (The mutable global is a known
 * wart: it should be keyed per instrument so a paged response cannot drag the
 * load-window clock backwards. Out of scope here; the probe is exact.)
 */
export async function probeDatalakeAnchor(
  exchange: string,
  symbol: string,
  interval: string,
): Promise<number | null> {
  try {
    const body = await fetchHistory(
      exchange,
      symbol,
      interval,
      undefined,
      Math.floor(Date.now() / 1000) + 86_400,
    );
    const newest = body.last_closed_time;
    if (typeof newest === 'number' && newest > 0) return newest;
  } catch (err) {
    console.warn('datalake anchor probe failed', err);
  }
  return anchorSec;
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
  /** Caller-requested depth level per instrument, replayed on (re)open. */
  private readonly depthLevels = new Map<string, string>();
  private readonly handlers = new Map<string, Set<(msg: unknown) => void>>();
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
      for (const key of this.subs.keys()) {
        const [instrument, interval] = key.split('|');
        this.send({ type: 'subscribe_bars', bars: [{ instrument, interval }] });
      }
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

export class V4DataFeed implements DataFeed {
  async getBars(req: BarsRequest): Promise<Bar[]> {
    const body = await fetchHistory(req.exchange, req.symbol, req.interval, req.from, req.to);
    const raw = Array.isArray(body.bars) ? body.bars : [];
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
