import {
  ChartTable,
  registerIndicator,
  type ChartTableOptions,
  type IndicatorDescriptor,
  type TableCell,
} from 'openalgo-charts';
import { createTier2Indicator, type Tier2Point } from 'openalgo-charts/indicators';
import { API_BASE, FeedError } from './feed';

// Backend-computed studies: their data never comes from the chart's bars, so
// they ride the engine's Tier-2 lifecycle (fetch once per data-affecting
// settings change, last-known-value alignment onto the bar timeline).

interface ErrorEnvelope {
  error?: { code?: string; message?: string };
}

const INTERVAL_OPTIONS = ['1m', '5m', '15m', '30m', '1h', 'D'].map((v) => ({ label: v, value: v }));

/** POST and unwrap the backend's `{"error":{code,message}}` envelope as FeedError. */
async function post(path: string, body: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = (await res.json().catch(() => undefined)) as (ErrorEnvelope & Record<string, unknown>) | undefined;
  if (data !== undefined && data.error !== undefined) {
    const err = data.error as { code?: unknown; message?: unknown };
    throw new FeedError(String(err.message ?? 'request failed'), String(err.code ?? 'error'));
  }
  if (!res.ok) throw new FeedError(`request failed (${res.status})`);
  return data ?? {};
}

// The engine core is deliberately handed bars, never an instrument, so a Tier-2
// fetch learns the instrument from its own inputs. Defaults match the chart's
// initial context; refetchOn reloads when the user retunes them.
const instrumentInputs = (defaultInterval: string) => [
  { key: 'exchange', type: 'text', label: 'Exchange', default: 'NSE' } as const,
  { key: 'symbol', type: 'text', label: 'Symbol', default: 'RELIANCE' } as const,
  { key: 'interval', type: 'select', label: 'Interval', default: defaultInterval, options: INTERVAL_OPTIONS } as const,
];

const backtestEquity = createTier2Indicator({
  id: 'backtest-equity',
  name: 'Backtest Equity',
  category: 'Backend',
  placement: 'pane',
  inputs: instrumentInputs('1h'),
  plots: [{ key: 'value', type: 'line', title: 'Equity' }],
  refetchOn: ['exchange', 'symbol', 'interval'],
  fetch: async (ctx) => {
    if (ctx.to === 0) return [];
    const s = ctx.settings;
    const body = await post('/api/charts/backtest', {
      exchange: String(s.exchange ?? 'NSE'),
      symbol: String(s.symbol ?? ''),
      interval: String(s.interval ?? '1h'),
      from: ctx.from,
      to: ctx.to,
      strategy: 'sma_cross',
    });
    // metrics null means the backend had no datalake data for the window.
    if (body.metrics === null || body.metrics === undefined) return [];
    const curve = (Array.isArray(body.equity_curve) ? body.equity_curve : []) as Array<{ time?: unknown; value?: unknown }>;
    const out: Tier2Point[] = [];
    for (const p of curve) {
      if (typeof p.time === 'number' && typeof p.value === 'number') {
        out.push({ time: p.time, values: { value: p.value } });
      }
    }
    return out;
  },
});

const marketProfile = createTier2Indicator({
  id: 'market-profile',
  name: 'Market Profile',
  category: 'Backend',
  placement: 'onchart',
  // The fetch posts the visible bars, so no input changes what is computed;
  // the inputs exist for the cache key only: the host syncs them on a symbol
  // or interval change, and refetchOn then invalidates the stale profile.
  inputs: instrumentInputs('1m'),
  refetchOn: ['exchange', 'symbol', 'interval'],
  plots: [
    { key: 'poc', type: 'line', title: 'POC' },
    { key: 'vah', type: 'line', title: 'VAH' },
    { key: 'val', type: 'line', title: 'VAL' },
  ],
  fetch: async (ctx) => {
    if (ctx.bars.length === 0) return [];
    const body = await post('/api/charts/profiles/market-profile', {
      bars: ctx.bars.map((b) => ({
        time: b.time, open: b.open, high: b.high, low: b.low, close: b.close, volume: b.volume ?? 0,
      })),
    });
    // The result is row-per-session; `developing` is its per-time half: the
    // POC/VAH/VAL as of each 30-minute block, keyed by block end time. Points
    // carry their own timestamps, so the engine's last-known-value alignment
    // lands them on the bar timeline without any per-bar derivation here.
    const result = body.result as
      | { sessions?: Array<{ developing?: Array<{ time?: unknown; poc?: unknown; vah?: unknown; val?: unknown }> }> }
      | undefined;
    const out: Tier2Point[] = [];
    for (const session of result?.sessions ?? []) {
      for (const d of session.developing ?? []) {
        if (typeof d.time === 'number' && typeof d.poc === 'number' && typeof d.vah === 'number' && typeof d.val === 'number') {
          out.push({ time: d.time, values: { poc: d.poc, vah: d.vah, val: d.val } });
        }
      }
    }
    return out;
  },
});

// Seasonality is a text table, not a line: the fetch returns the backend's
// `{table: {rows, options}}` verbatim into a ChartTable primitive pinned to the
// price pane. points stay empty by design (there is no value column to align).
// 'onchart' rather than 'pane' is deliberate: the engine's restore drops a
// saved pane that holds no series (chart.ts empty-pane sweep), which would
// silently delete this indicator on every reload; the price pane is never
// swept, and the reference seasonality overlay is a chart-mounted table anyway.
const seasonalityHeatmap: IndicatorDescriptor = {
  id: 'seasonality-heatmap',
  name: 'Seasonality',
  category: 'Backend',
  placement: 'onchart',
  inputs: instrumentInputs('D'),
  plots: [],
  calc: () => ({}),
  attach: (ctx) => {
    const table = new ChartTable({ position: 'bottom-center' });
    ctx.addPrimitive?.(table);
    let dead = false;
    const load = async (): Promise<void> => {
      const bars = ctx.bars();
      if (bars.length === 0) return;
      const s = ctx.settings();
      const body = (await post('/api/charts/seasonality', {
        exchange: String(s.exchange ?? 'NSE'),
        symbol: String(s.symbol ?? ''),
        interval: String(s.interval ?? 'D'),
        // Monthly % changes need whole months of history; the chart window is
        // usually days, so ask from the epoch and let the datalake bound it.
        from: 0,
        to: bars[bars.length - 1]?.time ?? 0,
      })) as { table?: { rows?: unknown; options?: Partial<ChartTableOptions> } };
      if (dead) return;
      const rows = (Array.isArray(body.table?.rows) ? body.table?.rows : []) as TableCell[][];
      // An empty cell serializes bgColor as JSON null; the engine's cell type
      // reads undefined as "no fill".
      table.setRows(rows.map((row) => row.map((cell) => ({ ...cell, bgColor: cell.bgColor ?? undefined }))));
      if (body.table?.options !== undefined) table.setOptions(body.table.options);
    };
    // A failed fetch leaves the (possibly empty) table as-is and warns; the
    // engine's keep-previous-points rule has no points to keep here.
    void load().catch((err: unknown) => {
      if (!dead) console.warn('seasonality fetch failed', err);
    });
    return () => {
      dead = true;
      ctx.removePrimitive?.(table);
    };
  },
};

let registered = false;

/** Register the three backend descriptors once, before the widget is created. */
export function registerTier2(): void {
  if (registered) return;
  registered = true;
  registerIndicator(backtestEquity);
  registerIndicator(marketProfile);
  registerIndicator(seasonalityHeatmap);
}

/** The ids this module registers (a restored chart carries them in its state). */
export const TIER2_IDS: ReadonlySet<string> = new Set(['backtest-equity', 'market-profile', 'seasonality-heatmap']);
