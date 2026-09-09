import { withBarCache, type Bar, type BarsRequest, type DataFeed } from 'openalgo-charts';

// # ponytail: same-origin when FastAPI serves us, dev-server proxy target when vite runs on :5173
export const API_BASE = typeof location === 'undefined' || location.port !== '5173' ? '' : 'http://127.0.0.1:8000';

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
}

export const feed: DataFeed = withBarCache(new V4DataFeed(), { timezone: 'Asia/Kolkata' });
