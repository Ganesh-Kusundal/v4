import {
  ChartTable,
  registerIndicator,
  type ChartDataContext,
  type ChartTableOptions,
  type IndicatorDescriptor,
  type IndicatorSettings,
  type TableCell,
} from 'openalgo-charts';
import { createTier2Indicator, type Tier2Point } from 'openalgo-charts/indicators';
import {
  BACKTEST_INDICATOR_ID,
  markRunLoaded,
  markRunLoading,
  publishBacktestResult,
  runBacktest,
  runFor,
  runState,
  type BacktestView,
} from './backtest';
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
// fetch learns the instrument from an input it can see. The chart's published
// data context is that source of truth: the widget calls `setDataContext()` on
// every symbol/interval/exchange change, the engine folds it into the fetch
// cache key and hands it to `fetch` as `ctx.dataContext`, so a study follows
// the chart with no host-side bookkeeping. The inputs stay as the fallback for
// when no context is published (bare `createChart` hosts) and as the visible
// record of what the study is pointed at.
const instrumentInputs = (defaultInterval: string) => [
  { key: 'exchange', type: 'text', label: 'Exchange', default: 'NSE' } as const,
  { key: 'symbol', type: 'text', label: 'Symbol', default: 'RELIANCE' } as const,
  { key: 'interval', type: 'select', label: 'Interval', default: defaultInterval, options: INTERVAL_OPTIONS } as const,
];

/** The chart's instrument: published context wins over the descriptor inputs. */
const marketOf = (ctx: {
  dataContext?: Readonly<ChartDataContext>;
  settings: Readonly<IndicatorSettings>;
}): { exchange: string; symbol: string; interval: string } => ({
  exchange: String(ctx.dataContext?.exchange ?? ctx.settings.exchange ?? 'NSE'),
  symbol: String(ctx.dataContext?.symbol ?? ctx.settings.symbol ?? 'RELIANCE'),
  interval: String(ctx.dataContext?.interval ?? ctx.settings.interval ?? '1m'),
});

/**
 * One strategy the `/backtest` endpoint can build, with its editable params.
 *
 * `params` is the backend's full param schema — name, type ("int" or "float")
 * and default — so the UI can render editable inputs without hardcoding each
 * strategy's shape.
 */
export interface StrategyParam {
  name: string;
  type: 'int' | 'float';
  default: number;
}

export interface StrategyInfo {
  id: string;
  params: StrategyParam[];
}

/**
 * The strategies `/backtest` can actually build, from `GET /strategies`, with
 * their full param schemas.
 *
 * The backend discovers eight strategy classes but only exposes its
 * `_STRATEGY_FACTORIES` set as backtestable, so this is deliberately the server's
 * answer rather than a hardcoded list: offering a strategy the endpoint cannot
 * build would render an empty pane. The fallback keeps the descriptor
 * registrable when the API is unreachable (no session, backend down).
 */
export async function loadBacktestStrategies(): Promise<StrategyInfo[]> {
  try {
    const res = await fetch(`${API_BASE}/api/charts/strategies`);
    if (!res.ok) return FALLBACK_STRATEGIES_INFO;
    const body = (await res.json()) as { backtestable?: Array<unknown> };
    const infos: StrategyInfo[] = [];
    for (const raw of Array.isArray(body.backtestable) ? body.backtestable : []) {
      if (typeof raw !== 'object' || raw === null) continue;
      const id = (raw as Record<string, unknown>).id;
      const rawParams = (raw as Record<string, unknown>).params;
      if (typeof id !== 'string' || !Array.isArray(rawParams)) continue;
      const params: StrategyParam[] = [];
      for (const p of rawParams) {
        if (typeof p !== 'object' || p === null) continue;
        const name = (p as Record<string, unknown>).name;
        const type = (p as Record<string, unknown>).type;
        const defaultVal = (p as Record<string, unknown>).default;
        if (typeof name === 'string' && (type === 'int' || type === 'float') && typeof defaultVal === 'number') {
          params.push({ name, type, default: defaultVal });
        }
      }
      infos.push({ id, params });
    }
    return infos.length > 0 ? infos : FALLBACK_STRATEGIES_INFO;
  } catch (err) {
    console.warn('strategy list failed', err);
    return FALLBACK_STRATEGIES_INFO;
  }
}

/** What `/backtest` defaults to when the catalogue cannot be read. */
const FALLBACK_STRATEGIES_INFO: StrategyInfo[] = [
  { id: 'sma_cross', params: [{ name: 'fast', type: 'int', default: 5 }, { name: 'slow', type: 'int', default: 20 }] },
];

/** Cell fill for the statistics table's heading row (matches the seasonality table). */
const HEADER_FILL = '#787b8633';

/**
 * Grid geometry for the statistics block.
 *
 * `margin` clears the pane's legend row rather than hugging the corner: the
 * legend is drawn at `top: 6` with an 18px row, so a corner-anchored grid at the
 * usual 8px margin lands straight through "Backtest Equity mean_reversion …".
 * 28 is that row's bottom edge plus a gap.
 */
const STATS_TABLE_OPTIONS: Partial<ChartTableOptions> = {
  position: 'top-left',
  cellWidth: [104, 78],
  cellHeight: 18,
  fontSize: 11,
  margin: 28,
};

const pct = (v: number): string => `${(v * 100).toFixed(2)}%`;
const fixed = (v: number): string => v.toFixed(2);

/**
 * The strategy a pane is pointed at: its own setting, or the offered default.
 *
 * One helper for both the fetch and the statistics grid, because the two must
 * agree on the run's identity: the grid looks its run up by this key, and a
 * disagreement here is a pane whose grid draws another pane's numbers.
 *
 * The run key includes a param fingerprint so two panes on the same strategy
 * with different params are two different runs — same as two different
 * strategies would be.
 */
const paramsOf = (
  settings: Readonly<IndicatorSettings>,
  strategyId?: string,
  strategies?: readonly StrategyInfo[],
): Record<string, number> => {
  let allowed: Set<string> | undefined;
  if (strategyId !== undefined && strategies !== undefined) {
    const match = strategies.find((s) => s.id === strategyId);
    if (match !== undefined) {
      allowed = new Set(match.params.map((p) => p.name));
    }
  }
  const out: Record<string, number> = {};
  for (const key of Object.keys(settings)) {
    if (!key.startsWith('param:')) continue;
    const name = key.slice(6);
    if (allowed !== undefined && !allowed.has(name)) continue;
    const value = Number(settings[key]);
    if (Number.isFinite(value)) out[name] = value;
  }
  return out;
};

const paramsKey = (params: Record<string, number>): string =>
  Object.keys(params)
    .sort()
    .map((k) => `${k}=${params[k]}`)
    .join(',');

function strategyOf(
  settings: Readonly<IndicatorSettings>,
  strategies: readonly StrategyInfo[],
): string {
  let id = 'sma_cross';
  const rawId = settings.strategy as string | undefined;
  if (typeof rawId === 'string' && rawId !== '') {
    id = rawId;
  } else {
    const first: StrategyInfo | undefined = strategies[0];
    const firstId: string | undefined = first?.id;
    if (firstId !== undefined) {
      id = firstId;
    }
  }
  const params = paramsOf(settings, id, strategies);
  return Object.keys(params).length > 0 ? `${id}@${paramsKey(params)}` : id;
}

/** The strategy id from a run key (strips the param fingerprint). */
const strategyIdOf = (key: string): string => key.split('@')[0] ?? key;

/**
 * The result statistics as table rows. Every value comes from the backend's
 * `metrics` — nothing is recomputed here, so the chart cannot disagree with the
 * engine about what a run did.
 *
 * The badge is part of the strategy cell when more than one run is live: with
 * two panes up, a grid that says only "sma cross" leaves the reader to work out
 * which of the two strategies the chart's zones belong to, and the badge is the
 * same letter the chip and the runs strip use.
 */
const formatParams = (params: Record<string, number>): string => {
  const parts = Object.entries(params)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => `${k}=${v}`);
  return parts.length > 0 ? ` (${parts.join(', ')})` : '';
};

const metricsRows = (view: BacktestView, badge: string | null): TableCell[][] => {
  const m = view.metrics;
  const header = (text: string): TableCell => ({ text, bold: true, bgColor: HEADER_FILL });
  const cell = (text: string): TableCell => ({ text, align: 'right' });
  const name = view.strategy.replace(/_/g, ' ') + formatParams(view.params);
  return [
    [header('Strategy'), header(badge === null ? name : `${badge} · ${name}`)],
    [{ text: 'Net Profit' }, cell(pct(m.total_return))],
    [{ text: 'Sharpe' }, cell(fixed(m.sharpe))],
    [{ text: 'Max Drawdown' }, cell(pct(m.max_drawdown))],
    [{ text: 'Trades' }, cell(String(m.num_trades))],
    ...(m.num_rejected > 0
      ? [[{ text: 'Rejected' }, cell(String(m.num_rejected))] as TableCell[]]
      : []),
    [{ text: 'Fees' }, cell(fixed(m.total_fees))],
  ];
};

/**
 * Build the descriptor's strategy-param inputs from a strategy's schema.
 *
 * Each param becomes a number input keyed `param:<name>` so it does not collide
 * with the descriptor's own `strategy`/`exchange`/`symbol`/`interval` keys.
 * The label carries the type so a user knows whether to expect integers or
 * decimals; the step matches it (1 for int, 0.1 for float).
 */
const paramInputs = (strategy: StrategyInfo): IndicatorDescriptor['inputs'] =>
  strategy.params.map((p) => ({
    key: `param:${p.name}`,
    type: 'number',
    label: `${p.name.replace(/_/g, ' ')} (${p.type})`,
    default: p.default,
    step: p.type === 'int' ? 1 : 0.1,
  }));

/** The param keys for a strategy, used in `refetchOn` so changing any param re-runs. */
const paramRefetchKeys = (strategies: readonly StrategyInfo[]): string[] => [
  ...new Set(strategies.flatMap((s) => s.params.map((p) => `param:${p.name}`))),
];

/**
 * Build the backtest pane's descriptor, offering the server's strategies.
 *
 * The statistics block rides the descriptor's own `table` hook, NOT `attach`.
 * `createTier2Indicator` returns an `attach` that *is* the fetch/align lifecycle,
 * so spreading an `attach` over it silently deletes the fetch and the pane renders
 * an empty grid forever; `table` composes instead of colliding.
 */
const makeBacktestEquity = (strategies: readonly StrategyInfo[]): IndicatorDescriptor => ({
  ...createTier2Indicator({
  id: BACKTEST_INDICATOR_ID,
  name: 'Backtest Equity',
  category: 'Backend',
  placement: 'pane',
  inputs: [
    // The picker is the engine's own settings dialog: a Tier-2 descriptor's
    // inputs are a form, so selecting a strategy (and re-running on change)
    // needs no host chrome at all.
    {
      key: 'strategy',
      type: 'select',
      label: 'Strategy',
      default: strategies[0]?.id ?? 'sma_cross',
      options: strategies.map((s) => ({ label: s.id.replace(/_/g, ' '), value: s.id })),
    },
    // Strategy params are rendered as number inputs. The selected strategy
    // decides which params exist; the descriptor offers every strategy's params
    // so switching strategies does not require re-adding the pane. Values for
    // params the selected strategy does not use are ignored at fetch time.
    ...strategies.flatMap(paramInputs),
    ...instrumentInputs('1h'),
  ],
  // Equity on the right scale, drawdown on the left: same pane, same bar
  // timeline, two different units. One scale would flatten a 0.5% dip into the
  // 100k equity line, which is the whole reason a drawdown column exists.
  plots: [
    { key: 'value', type: 'line', title: 'Equity' },
    {
      key: 'drawdownPct',
      type: 'line',
      title: 'Drawdown %',
      priceScaleId: 'left',
      style: { color: '#ef5350', lineWidth: 1 },
    },
  ],
  refetchOn: ['exchange', 'symbol', 'interval', 'strategy', ...paramRefetchKeys(strategies)],
  // One run, three consumers: the pane plots the equity, and `backtest.ts`
  // publishes the same result so the statistics table below and the chart
  // markers read it instead of each firing their own backtest.
  fetch: async (ctx) => {
    // The window is the bars the pane was handed — the chart's visible slice —
    // and NOT `ctx.from`/`ctx.to`.
    //
    // Tier-2 passes those along as the *request's* window, and the engine pages an
    // extension to `[state.to, visible.to]` whenever the visible slice grows past
    // what it already holds. That window is the newly revealed sliver, so a live
    // bar append would re-run the strategy over the last few bars and replace a
    // real equity curve with a handful of them (observed: a 55-fill run becoming
    // "Trades 0" after the tape appended seven bars). Reading the bars keeps the
    // published run over exactly what is on screen, whichever kind of load it was.
    const first = ctx.bars[0];
    const last = ctx.bars[ctx.bars.length - 1];
    const market = marketOf(ctx);
    const key = strategyOf(ctx.settings, strategies);
    const strategyId = strategyIdOf(key);
    const params = paramsOf(ctx.settings, strategyId, strategies);
    // No bars on the chart (a replay window that hides them, or a chart before
    // its first load) is no window to backtest; one bar is not a window either.
    if (first === undefined || last === undefined || last.time <= first.time) {
      publishBacktestResult(key, null, market);
      return [];
    }
    markRunLoading(key);
    try {
      const view = await runBacktest(
        { ...market, from: first.time, to: last.time, strategy: strategyId, params },
        ctx.signal,
      );
      // null means the backend had no datalake data for the window; clearing is
      // right, so a stale table or marker set does not outlive the data. It
      // clears *this* run: another pane's run is not this pane's business.
      publishBacktestResult(key, view, market);
      return view?.equity ?? [];
    } catch (err) {
      // A superseded run (aborted by a settings change) must not clear what a
      // newer run is about to publish; a real failure must, so the statistics
      // cannot outlive the data they were computed on.
      if (!(err instanceof DOMException && err.name === 'AbortError')) {
        publishBacktestResult(key, null, market);
      }
      throw err;
    } finally {
      markRunLoaded(key);
    }
  },
  }),
  // The statistics block, via the runtime's own summary-grid hook: it creates the
  // grid lazily in this indicator's pane and re-reads it after every recompute,
  // so it moves, clips and dies with the pane and owns no lifecycle here.
  //
  // The numbers come from `backtest.ts` rather than from `values`: one run feeds
  // the equity plot, this block, and the price-chart overlay, so the three
  // surfaces cannot disagree about what was run. The *run* is resolved from this
  // pane's own strategy setting — `table` sees its instance's settings and is
  // called per instance, so a chart with two backtest panes draws two grids of
  // two different runs. Reading a single "latest" here is what made every pane
  // show whichever run finished last.
  table: (ctx) => {
    const key = strategyOf(ctx.settings, strategies);
    const run = runFor(key);
    if (run === null) return null;
    const many = runState().runs.length > 1;
    return {
      rows: metricsRows(run.view, many ? run.letter : null),
      options: STATS_TABLE_OPTIONS,
    };
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
      const body = (await post('/api/charts/seasonality', {
        ...marketOf({ dataContext: ctx.dataContext?.(), settings: ctx.settings() }),
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

/**
 * Register the three backend descriptors once, before the widget is created.
 *
 * `strategies` comes from `loadBacktestStrategies()` — the backtest pane's
 * picker is built from it, so the catalogue must be read before this runs.
 */
export function registerTier2(strategies: readonly StrategyInfo[] = FALLBACK_STRATEGIES_INFO): void {
  if (registered) return;
  registered = true;
  registerIndicator(makeBacktestEquity(strategies));
  registerIndicator(marketProfile);
  registerIndicator(seasonalityHeatmap);
}

/** The ids this module registers (a restored chart carries them in its state). */
export const TIER2_IDS: ReadonlySet<string> = new Set(['backtest-equity', 'market-profile', 'seasonality-heatmap']);
