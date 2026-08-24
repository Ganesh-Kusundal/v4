// TradexDataFeed — maps openalgo-charts' DataFeed contract onto the v4
// backend's /api/charts/history. The chart depends only on DataFeed; this is
// the one adapter it needs to speak to our engine.
import {
  registerInterval,
  withBarCache,
  type Bar,
  type BarsRequest,
  type DataFeed,
} from "openalgo-charts";

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
    if (!resp.ok) {
      throw new Error(`history failed (${resp.status}): ${await resp.text()}`);
    }
    const body = (await resp.json()) as HistoryResponse;
    // The backend only ever serves closed bars; assert the shape so a schema
    // drift fails loudly here instead of drawing garbage.
    return body.bars.filter((b) => Number.isFinite(b.time));
  }

  // Live forming-bar updates arrive over /ws/stream (Task 4); subscribeBars
  // stays unimplemented until then and the chart treats history-only feeds
  // as valid.
}

/** Feed wrapped in the library's warm-load cache. */
export function createTradexFeed(): DataFeed {
  registerTradexIntervals();
  return withBarCache(new TradexDataFeed(), { ttlMs: 60_000 });
}
