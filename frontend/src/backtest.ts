import { IndicatorDrawings, type IndicatorDrawing, type TradingTrade } from 'openalgo-charts';
import type { Tier2Point } from 'openalgo-charts/indicators';
import type { Widget } from 'openalgo-charts/widget';
import { API_BASE, FeedError } from './feed';

/**
 * Backtest results as a first-class chart object.
 *
 * `POST /api/charts/backtest` returns `{metrics, equity_curve, trades}` and the
 * chart used to keep only the equity curve — the statistics and the fill list
 * were computed, shipped, and dropped on the floor. This module owns the call and
 * fans each result out:
 *
 *   equity_curve → the Backtest Equity pane (a Tier-2 plot)
 *   metrics      → a statistics table in that pane
 *   trades       → entry/exit markers on the price chart
 *   fills        → position zones on the price chart, via a filled round trip
 *
 * A chart can hold several panes, so it holds several **runs**, addressed by
 * strategy and published into a registry (below). Exactly one of them owns the
 * price pane; the user can add comparisons beside it. The ownership rules are
 * stated where the registry is, because "which run is on the chart" is the part
 * that has to be readable — the previous single-slot version answered it with
 * "whichever fetch finished last".
 *
 * The fills are rebuilt into positions **once**, at publish time, and the same
 * list is rendered twice more: as zones on the price pane, and as one row per
 * round trip in the trades table (`trades-panel.ts`). A row click selects the
 * zone that row was built from rather than one that merely resembles it.
 *
 * The fills are the right source for markers, not the signals: a signal the risk
 * manager rejected, or one with no bar to fill at, has no price to draw. The
 * backend says so itself (`BacktestResult.fills`), and flags rejections so a UI
 * can render them as errors.
 *
 * Nothing here invents a level. Position zones are anchored on two real fill
 * prices, and a bracket is drawn only where the fill carries the levels its
 * order declared — so a filled order with no legs shows no bracket rather than a
 * plausible-looking default.
 *
 * What the drawn bracket does NOT mean: the backtest does not simulate a
 * protective exit. The lines describe the levels the order carried, and they are
 * truthful for a strategy that also exits at them (see
 * `docs/design/2026-09-13-backtest-results-surface.md`).
 */

export interface BacktestMetrics {
  total_return: number;
  sharpe: number;
  max_drawdown: number;
  num_trades: number;
  num_rejected: number;
  total_fees: number;
}

/** One executed fill. `time` is UTC seconds; rejections carry price 0. */
export interface BacktestFill {
  time: number;
  side: string;
  price: number;
  qty: number | null;
  reason: string;
  rejected: boolean;
  /**
   * The protective levels the order behind this fill carried, or `null`.
   *
   * `null` is not "unknown" — it is "this order declared none", which is every
   * order from a strategy that trades signals rather than levels. The chart
   * draws a bracket only where these are present, so it can never show a stop
   * the run did not have.
   */
  stop: number | null;
  target: number | null;
}

export interface BacktestView {
  /** Echoed back so the statistics table can name what it ran. */
  strategy: string;
  /** The params the run was executed with, keyed by param name. */
  params: Record<string, number>;
  equity: Tier2Point[];
  metrics: BacktestMetrics;
  fills: BacktestFill[];
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string };
}

export interface BacktestRequest {
  exchange: string;
  symbol: string;
  interval: string;
  strategy: string;
  from: number;
  to: number;
  /** Strategy parameters keyed by param name. */
  params?: Record<string, number>;
}

const num = (v: unknown, fallback = 0): number => (typeof v === 'number' && Number.isFinite(v) ? v : fallback);

/** A price that must be usable to be drawn, or `null`. Rejections carry 0. */
const positive = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null;

/**
 * Run one backtest. Returns null when the backend had no datalake data for the
 * window — it answers `{metrics: null, message}` rather than an error, which is
 * a legitimate "nothing to show", not a failure to report.
 */
export async function runBacktest(
  req: BacktestRequest,
  signal?: AbortSignal,
): Promise<BacktestView | null> {
  const requestBody: Record<string, unknown> = { ...req };
  const reqParams = requestBody.params as Record<string, number> | undefined;
  if (reqParams !== undefined && Object.keys(reqParams).length === 0) {
    delete requestBody.params;
  }
  const res = await fetch(`${API_BASE}/api/charts/backtest`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(requestBody),
    signal,
  });
  const body = (await res.json().catch(() => undefined)) as
    | (ErrorEnvelope & Record<string, unknown>)
    | undefined;
  if (body !== undefined && body.error !== undefined) {
    const err = body.error as { code?: unknown; message?: unknown };
    throw new FeedError(String(err.message ?? 'backtest failed'), String(err.code ?? 'error'));
  }
  if (!res.ok) throw new FeedError(`backtest failed (${res.status})`);
  if (body === undefined || body.metrics === null || body.metrics === undefined) return null;

  const m = body.metrics as Record<string, unknown>;
  const raw: Array<{ time: number; value: number }> = [];
  for (const p of (Array.isArray(body.equity_curve) ? body.equity_curve : []) as Array<{
    time?: unknown;
    value?: unknown;
  }>) {
    if (typeof p.time === 'number' && typeof p.value === 'number') {
      raw.push({ time: p.time, value: p.value });
    }
  }

  // The drawdown column rides the same points as the equity column, so the pane
  // draws the curve the engine measured its `max_drawdown` on. Derived here from
  // the shipped series against its own running peak rather than recomputed from
  // bars, which is why the two cannot disagree: `max_drawdown` IS the minimum of
  // this column. Points are built once, fully populated — a `Tier2Point`'s values
  // are read-only by contract.
  let peak = Number.NEGATIVE_INFINITY;
  const equity: Tier2Point[] = raw.map((p) => {
    peak = Math.max(peak, p.value);
    const drawdownPct = peak === 0 ? 0 : (p.value / peak - 1) * 100;
    return { time: p.time, values: { value: p.value, drawdownPct } };
  });

  const fills: BacktestFill[] = [];
  for (const f of (Array.isArray(body.trades) ? body.trades : []) as Array<Record<string, unknown>>) {
    if (typeof f.time !== 'number' || typeof f.price !== 'number') continue;
    fills.push({
      time: f.time,
      side: String(f.side ?? ''),
      price: f.price,
      qty: typeof f.qty === 'number' ? f.qty : null,
      reason: typeof f.reason === 'string' ? f.reason : '',
      rejected: f.rejected === true,
      stop: positive(f.stop),
      target: positive(f.target),
    });
  }

  return {
    strategy: req.strategy,
    params: req.params ?? {},
    equity,
    fills,
    metrics: {
      total_return: num(m.total_return),
      sharpe: num(m.sharpe),
      max_drawdown: num(m.max_drawdown),
      num_trades: num(m.num_trades),
      num_rejected: num(m.num_rejected),
      total_fees: num(m.total_fees),
    },
  };
}

// ── runs ──────────────────────────────────────────────────────────────────
//
// A chart holds a *set* of runs, not one. Two Backtest Equity panes are the
// normal way to compare two strategies on one instrument, and the previous
// version — a single `latest` slot that every pane overwrote — could not say
// which run the price pane was drawing: whichever fetch happened to land last
// owned it, so the overlay was decided by request timing.
//
// A run is addressed by **its strategy**, which is the whole of what a pane can
// differ in: the market and the window are the chart's, and the parameters are
// not exposed as inputs yet (when they are, they belong in this key, because two
// parameterisations of one strategy are two different runs). Keying by strategy
// is also what lets a pane find *its own* run — its settings are the only thing
// an instance knows about itself — instead of reading the newest one.
//
// Exactly one run owns the price pane (markers, zones, brackets). The owner is
// explicit state, never a publish order:
//
//   * the first run to register owns a chart that has no owner;
//   * publishing another run never steals ownership;
//   * the owner leaving (its pane closed, or the run cleared) hands the chart to
//     the **earliest remaining** run — a stable order — and to nothing when none
//     remain, which clears the overlay rather than stranding it on a dead run;
//   * the user can move it at any time, and that choice survives new publishes.

/** The chart-level instrument a run was over. One chart, one of these. */
export interface RunMarket {
  exchange: string;
  symbol: string;
  interval: string;
}

export interface RunRecord {
  /** Identity: the strategy id. */
  key: string;
  /** Badge (`A`, `B`, …), assigned on registration and stable while the run lives. */
  letter: string;
  /** Registration order. Ownership falls back to the smallest, not the newest. */
  seq: number;
  view: BacktestView;
  /**
   * The run's positions, paired once at publish time.
   *
   * Built here rather than per paint, or per consumer: the trades table's row
   * indices and the zones drawn on the chart must be the same list, and a pairing
   * recomputed on a different paint than the one the row was read from is exactly
   * how a row would come to name a different trade.
   */
  ledger: PositionLedger;
  market: RunMarket;
}

export interface RunState {
  /** Every live run, in registration order. */
  runs: readonly RunRecord[];
  owner: RunRecord | null;
  comparing: readonly RunRecord[];
}

/**
 * Identity colours for the comparison runs, indexed by badge letter.
 *
 * The only fixed colours in this module, and the reason is structural: a canvas
 * needs literal colour strings, and `ChartTheme` publishes exactly three hues —
 * the candle pair, which already means *up* and *down*, and the line colour,
 * which is the accent a picked zone is outlined in. So a run that is neither the
 * owner nor a direction needs a hue of its own, mid-luminance enough to read on
 * both the dark and the light theme. Indexed by letter so a run's colour never
 * moves to another run when the comparison set changes.
 */
const RUN_COLORS = [
  '#e6b53c', // amber — the colour the widget chrome already uses as its third
  '#9b7bf0',
  '#2bb3c0',
  '#d4805f',
  '#7cb342',
  '#c2185b',
  '#5c8bd6',
  '#b09a4f',
] as const;

/** How many runs can be drawn over the price pane besides the owner. */
export const MAX_COMPARISONS = 2;

let runs: RunRecord[] = [];
let ownerKey: string | null = null;
let comparing: string[] = [];
let seq = 0;
const listeners = new Set<(state: RunState) => void>();

/** Letters in use, so a new run takes a free one and a closed run frees it. */
const usedLetters = (): Set<string> => new Set(runs.map((r) => r.letter));

function nextLetter(): string {
  const used = usedLetters();
  for (let i = 0; i < 26; i += 1) {
    const letter = String.fromCharCode(65 + i);
    if (!used.has(letter)) return letter;
  }
  return '?';
}

/** The identity colour of a run: its badge letter indexes the palette. */
export const runColor = (run: RunRecord): string =>
  RUN_COLORS[run.letter.charCodeAt(0) - 65] ?? RUN_COLORS[0];

const byKey = (key: string): RunRecord | undefined => runs.find((r) => r.key === key);

function state(): RunState {
  const owner = ownerKey === null ? null : byKey(ownerKey) ?? null;
  return {
    runs,
    owner,
    comparing: comparing.map((k) => byKey(k)).filter((r): r is RunRecord => r !== undefined),
  };
}

/**
 * Push the current state to every consumer.
 *
 * Notified in one place, and only when something changed: the overlay paints the
 * owner *and* the comparison set, so a consumer that reacted to one of them
 * separately would draw the owner's change against a stale comparison.
 */
function notify(): void {
  const current = state();
  for (const cb of listeners) cb(current);
}

/**
 * Subscribe to the run set. Fires immediately, so a consumer attached after the
 * fetch is not left blank. Returns an unsubscribe.
 */
export function onRuns(cb: (state: RunState) => void): () => void {
  listeners.add(cb);
  cb(state());
  return () => {
    listeners.delete(cb);
  };
}

// ── loading runs ──────────────────────────────────────────────────────────
//
// Runs that are currently being computed but have not yet published. The runs
// bar shows these as a loading row so the user sees that something is happening
// instead of a blank strip. The trades panel shows a loading message.
//
// tier2.ts is the producer — it calls `markRunLoading` before the fetch and
// `markRunLoaded` after, regardless of success or failure. Panels subscribe via
// `onRunLoading`.

const loadingRuns = new Set<string>();
const loadingListeners = new Set<(keys: ReadonlySet<string>) => void>();

function notifyLoading(): void {
  for (const cb of loadingListeners) cb(loadingRuns);
}

/** Mark a run as in-flight. */
export function markRunLoading(key: string): void {
  if (key === '' || loadingRuns.has(key)) return;
  loadingRuns.add(key);
  notifyLoading();
}

/** Mark a run as no longer in-flight. */
export function markRunLoaded(key: string): void {
  if (!loadingRuns.has(key)) return;
  loadingRuns.delete(key);
  notifyLoading();
}

/** Subscribe to the set of in-flight runs. Fires immediately. */
export function onRunLoading(cb: (keys: ReadonlySet<string>) => void): () => void {
  loadingListeners.add(cb);
  cb(loadingRuns);
  return () => {
    loadingListeners.delete(cb);
  };
}

/** The strategies currently being computed. */
export const loadingRunKeys = (): ReadonlySet<string> => loadingRuns;

/** The live runs and which of them the price pane is drawing. */
export const runState = (): RunState => state();

/** One run by its strategy, or null. */
export const runFor = (key: string): RunRecord | null => byKey(key) ?? null;

/** `A · sma cross` — how a run is named wherever there is room for the letter. */
export const runLabel = (run: RunRecord, withLetter = true): string =>
  `${withLetter ? `${run.letter} · ` : ''}${run.view.strategy.replace(/_/g, ' ')}`;

/**
 * Register or refresh a run. `null` means *this* run produced nothing (no data
 * for the window): it is dropped, and the other runs are left alone.
 *
 * Ownership is decided here and nowhere else, and only for a chart that has
 * none: a run that arrives later never steals the overlay.
 */
export function publishBacktestResult(
  key: string,
  view: BacktestView | null,
  market: RunMarket,
): void {
  if (key === '') return;
  if (view === null) {
    removeRun(key);
    return;
  }

  const existing = byKey(key);
  const record: RunRecord = {
    key,
    letter: existing?.letter ?? nextLetter(),
    seq: existing?.seq ?? (seq += 1),
    view,
    ledger: pairRoundTrips(view.fills),
    market,
  };

  // A republished run has a new fill list, so a pick made in the old one names a
  // trade that may no longer exist. Dropped before the state is announced, so
  // the table and the painter rebuild from the same empty pick.
  if (existing !== undefined) clearPickFor(key);
  runs = existing === undefined
    ? [...runs, record].sort((a, b) => a.seq - b.seq)
    : runs.map((r) => (r.key === key ? record : r));

  if (ownerKey === null || byKey(ownerKey) === undefined) ownerKey = key;
  notify();
}

function removeRun(key: string): void {
  if (byKey(key) === undefined) return;
  runs = runs.filter((r) => r.key !== key);
  comparing = comparing.filter((k) => k !== key);
  clearPickFor(key);
  if (ownerKey === key) {
    // The earliest remaining run, not the newest and not "none while others
    // live": the chart keeps showing *something* real, and which something does
    // not depend on the order the runs happened to publish in.
    ownerKey = runs[0]?.key ?? null;
  }
  notify();
}

/**
 * Drop every run. The instrument or interval changed, so nothing on screen
 * describes these runs any more — an overlay on another symbol's bars is a claim
 * about bars the run never saw.
 */
export function clearBacktestRuns(): void {
  if (runs.length === 0 && ownerKey === null) return;
  runs = [];
  comparing = [];
  ownerKey = null;
  pickTrip(null);
  notify();
}

/** Make one run the owner of the price pane. */
export function setRunOwner(key: string): void {
  if (byKey(key) === undefined || ownerKey === key) return;
  ownerKey = key;
  // The owner is drawn by definition, so it is not also a comparison.
  comparing = comparing.filter((k) => k !== key);
  notify();
}

/**
 * Add or remove a comparison run. The owner is not comparable with itself, and
 * the set is capped: past it the outlines occlude the candles they are drawn
 * over, which is the thing the comparison is supposed to make readable.
 */
export function toggleRunComparison(key: string): void {
  if (byKey(key) === undefined || key === ownerKey) return;
  if (comparing.includes(key)) {
    comparing = comparing.filter((k) => k !== key);
  } else if (comparing.length < MAX_COMPARISONS) {
    comparing = [...comparing, key];
  } else {
    return;
  }
  notify();
}



// ── the picked trade ──────────────────────────────────────────────────────
//
// Which entry of the trades table is marked, and therefore which zone is
// emphasised. `{key, kind: 'trip', index}` indexes that run's `ledger.trips` —
// the list the table renders and the zone painter draws — so a row and its zone
// cannot drift apart. `{kind: 'open'}` is the lot the run ended holding: not a
// round trip, so it is not an index into `trips`.
//
// The run is part of the pick because a trip index is only meaningful against
// the fills it was counted from: with two runs on the chart, `3` names a trade in
// each of them.

export type TripPick = { kind: 'trip'; index: number } | { kind: 'open' };
export type TripSelection = (TripPick & { key: string }) | null;

let selection: TripSelection = null;
const selectionListeners = new Set<(sel: TripSelection) => void>();

/** The picked entry, or null. */
export const selectedTrip = (): TripSelection => selection;

/**
 * Pick an entry to mark, or unmark one that is already marked: a second click on
 * a selected row clears the selection, which is the only way back to a chart
 * with nothing emphasised.
 */
export function pickTrip(next: TripSelection): void {
  const same = isSameSelection(next, selection);
  const value = same ? null : next;
  if (value === null && selection === null) return;
  selection = value;
  for (const cb of selectionListeners) cb(value);
}

/** Drop the pick when it names a run that no longer exists. */
function clearPickFor(key: string): void {
  if (selection !== null && selection.key === key) pickTrip(null);
}

function isSameSelection(a: TripSelection, b: TripSelection): boolean {
  if (a === null || b === null) return a === b;
  if (a.key !== b.key || a.kind !== b.kind) return false;
  return a.kind === 'open' || (b.kind === 'trip' && a.index === b.index);
}

/**
 * Subscribe to picks. Fires immediately with the current one, so a table built
 * after the selection exists still shows it. Returns an unsubscribe.
 */
export function onTripSelection(cb: (sel: TripSelection) => void): () => void {
  selectionListeners.add(cb);
  cb(selection);
  return () => {
    selectionListeners.delete(cb);
  };
}

/**
 * Bring a span of traded time onto the chart, keeping the current zoom.
 *
 * The trip's ends are times and the viewport is a range of logical indices, so
 * the conversion goes through `dataLayer.timeToIndexFloat` — the very mapping the
 * chart places a shape with, which is why this lands on the bars the zone was
 * drawn on rather than near them.
 *
 * A span already inside the viewport is left alone: moving a chart the user just
 * framed is worse than doing nothing. Otherwise the viewport is *shifted* and not
 * resized, so selecting a trade never changes the zoom the row was read at.
 */
export function revealRange(widget: Widget, fromTime: number, toTime: number): void {
  const layer = widget.chart.dataLayer;
  const a = layer.timeToIndexFloat(fromTime);
  const b = layer.timeToIndexFloat(toTime);
  // NaN when no series has data: there is no index to scroll to.
  if (!Number.isFinite(a) || !Number.isFinite(b)) return;
  const lo = Math.min(a, b);
  const hi = Math.max(a, b);
  const view = widget.chart.getVisibleLogicalRange();
  if (lo >= view.from && hi <= view.to) return;
  const span = view.to - view.from;
  const centre = (lo + hi) / 2;
  widget.chart.setVisibleLogicalRange({ from: centre - span / 2, to: centre + span / 2 });
}

const isSell = (side: string): boolean => side.trim().toLowerCase().startsWith('s');

// ── position zones ────────────────────────────────────────────────────────
//
// A fill list is not a position list. These strategies scale in (mean_reversion
// buys five dips before it exits), so a zone is not "fill N to fill N+1": the
// open lot is carried at its own weighted-average entry and closed by whatever
// fill takes it to zero or flips it.

/** Protective levels a run declared, as drawn. */
export interface Bracket {
  stop: number;
  target: number;
}

/** One completed round trip: a real entry price and a real exit price. */
export interface RoundTrip {
  side: 'buy' | 'sell';
  entryTime: number;
  /** Weighted average of every fill that opened or added to the lot. */
  entryPrice: number;
  exitTime: number;
  exitPrice: number;
  /** Units closed by this trip, which may be less than the lot's size. */
  qty: number;
  /** What the opening order declared, or `null` when it declared nothing. */
  bracket: Bracket | null;
}

/** A position the run ended still holding — no exit fill exists for it. */
export interface OpenLot {
  side: 'buy' | 'sell';
  entryTime: number;
  entryPrice: number;
  qty: number;
  bracket: Bracket | null;
}

export interface PositionLedger {
  trips: RoundTrip[];
  open: OpenLot | null;
}

/**
 * Rebuild positions from the fills: signed lot, weighted-average entry, closed
 * by any fill that reduces it to zero or flips it. A flip closes the whole lot
 * and opens the remainder at the fill's own price, which is what the execution
 * engine did — it is one fill, but two positions' worth of fact.
 */
/** The bracket a fill's order carried, or `null`. */
export const bracketOf = (f: BacktestFill): Bracket | null =>
  f.stop === null || f.target === null ? null : { stop: f.stop, target: f.target };

export function pairRoundTrips(fills: readonly BacktestFill[]): PositionLedger {
  const trips: RoundTrip[] = [];
  let qty = 0;
  let avg = 0;
  let since = 0;
  let bracket: Bracket | null = null;
  for (const f of fills) {
    // A rejected fill never reached the book, and a zero price is not a price.
    if (f.rejected || f.price <= 0) continue;
    const size = Math.abs(f.qty ?? 0);
    if (size === 0) continue;
    const signed = isSell(f.side) ? -size : size;
    if (qty === 0) {
      qty = signed;
      avg = f.price;
      since = f.time;
      bracket = bracketOf(f);
      continue;
    }
    if (qty > 0 === signed > 0) {
      avg = (avg * Math.abs(qty) + f.price * size) / (Math.abs(qty) + size);
      qty += signed;
      // An add-on is a separate order with its own declaration; the lot keeps
      // the one it was opened with rather than silently adopting the newest.
      continue;
    }
    trips.push({
      side: qty > 0 ? 'buy' : 'sell',
      entryTime: since,
      entryPrice: avg,
      exitTime: f.time,
      exitPrice: f.price,
      qty: Math.min(Math.abs(qty), size),
      bracket,
    });
    qty += signed;
    // A flip leaves a position on the other side, opened by this same fill —
    // which is the fill that declared the bracket, so the remainder inherits
    // it. The declaration was validated against this fill's own price, which is
    // the remainder's entry too.
    if (qty !== 0) {
      avg = f.price;
      since = f.time;
      bracket = bracketOf(f);
    }
  }
  const open: OpenLot | null =
    qty === 0
      ? null
      : { side: qty > 0 ? 'buy' : 'sell', entryTime: since, entryPrice: avg, qty: Math.abs(qty), bracket };
  return { trips, open };
}

/**
 * The colours the results layer draws in, taken from the chart theme.
 *
 * Fixed hexes would be right for exactly one theme. `up`/`down` are the theme's
 * own candle pair, so a zone is filled in the same green as the body of a bar
 * that closed up, and `accent` is its line colour, which is what a selection is
 * outlined in.
 */
export interface DrawingPalette {
  up: string;
  down: string;
  accent: string;
}

/** For a caller with no chart to read a theme from (the builders are pure). */
const FALLBACK_PALETTE: DrawingPalette = { up: '#26a69a', down: '#ef5350', accent: '#2962ff' };

/**
 * How one run's geometry is drawn.
 *
 * Absent means the owner: the theme's own win/loss colours, filled zones, and —
 * when a row is picked — the accent outline. That is the treatment a single run
 * has always had, and it stays the default so the common case is unchanged.
 *
 * A comparison run is drawn *secondarily*, and the two cues are not decorative:
 *
 *   * **Outline only, in the run's own colour.** A `box` drawing has no
 *     `lineStyle`, so fill is the only channel left to say "this one is the run
 *     on the chart"; a comparison keeps the border and gives up the fill.
 *   * **The badge, on every caption.** `B +1.24%`, `B SL 1468.70`. The run is
 *     named where the number is, so the two are read together.
 *
 * Win/loss gives way to identity on a comparison run, which is the trade that
 * makes the comparison possible at all: the fill that carries win/loss on the
 * owner is the same channel that distinguishes one run from another.
 */
export interface RunStyle {
  /** Badge shown on captions. Omitted for the owner. */
  badge?: string;
  /** Identity colour. Omitted for the owner, whose colours come from the theme. */
  color?: string;
  /** Outline-only zones, no fill. */
  secondary?: boolean;
}

/** The style a run's geometry is drawn in: the owner's is empty. */
export function styleFor(run: RunRecord, isOwner: boolean): RunStyle {
  return isOwner ? {} : { badge: run.letter, color: runColor(run), secondary: true };
}

/** `A ` prefix on a caption, when the drawing carries a badge. */
const badgePrefix = (style: RunStyle): string =>
  style.badge === undefined ? '' : `${style.badge} `;

/**
 * Read per paint rather than once per module: a host can switch the theme at
 * runtime, and the zones have to follow the candles they are drawn over.
 */
const paletteOf = (widget: Widget): DrawingPalette => {
  const theme = widget.chart.theme();
  return { up: theme.upColor, down: theme.downColor, accent: theme.lineColor };
};

// Stop and target are risk and reward, not win and loss: a stop is reached at a
// loss and a target at a win, so they draw in the loss and win colours.

const pctText = (v: number): string => `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;

/** Gross return of a trip, signed by direction. */
export const tripReturn = (t: RoundTrip): number =>
  ((t.exitPrice - t.entryPrice) / t.entryPrice) * (t.side === 'buy' ? 1 : -1) * 100;

/**
 * A trip's profit in the currency the fill prices are quoted in, and as a
 * percentage.
 *
 * Both are gross, because the backend reports fees per run and not per trade.
 * The amount is the reason the table exists at all: the percentage is what the
 * zone chip already shows, and it cannot tell a ten-share trade from a
 * thousand-share one.
 */
export function tripPnl(t: RoundTrip): { amount: number; pct: number } {
  const direction = t.side === 'buy' ? 1 : -1;
  return { amount: (t.exitPrice - t.entryPrice) * t.qty * direction, pct: tripReturn(t) };
}

/**
 * Position zones as chart shapes: one box per round trip spanning entry time to
 * exit time and entry price to exit price, with the trip's return on a chip at
 * the zone's top-left corner.
 *
 * Anchors are times, not indices, so a zone stays on the bars it happened on
 * when the window is panned or older history is paged in.
 *
 * The region stays thin (the candles are the subject) and the chip carries the
 * number. The return is the **price** return: the backend reports fees per run,
 * not per trade, so a net figure here would be a guess. The statistics block
 * carries the run's total.
 *
 * `selected` is the trip the trades table has picked, or null. It is outlined in
 * the theme's accent colour and filled harder, because a thicker outline in the
 * trip's own green is not a selection anyone can find among a dozen neighbouring
 * zones — and the outline is the one part a box on top of another still shows.
 *
 * The chip is its own `label` item rather than the box's `text`, because a box
 * caption is plated in the box's own colour — passing a matching `textColor`
 * paints it solid and hides the number, and passing a different one hard-codes a
 * contrast the theme cannot re-decide.
 */
export function zoneDrawings(
  trips: readonly RoundTrip[],
  selected: number | null = null,
  palette: DrawingPalette = FALLBACK_PALETTE,
  style: RunStyle = {},
): IndicatorDrawing[] {
  const out: IndicatorDrawing[] = [];
  trips.forEach((t, index) => {
    const { pct } = tripPnl(t);
    const color = pct >= 0 ? palette.up : palette.down;
    const picked = index === selected;
    if (style.secondary === true) {
      // Outline only (a box has no `lineStyle` to dash), in the run's colour,
      // with its badge on the chip: the fill channel belongs to the owner.
      out.push({
        kind: 'box',
        from: { time: t.entryTime, price: t.entryPrice },
        to: { time: t.exitTime, price: t.exitPrice },
        color: style.color,
        lineWidth: picked ? 2 : 1,
      } satisfies IndicatorDrawing);
      out.push({
        kind: 'label',
        at: { time: t.entryTime, price: Math.max(t.entryPrice, t.exitPrice) },
        text: `${badgePrefix(style)}${pctText(pct)}`,
        color: style.color,
        align: 'left',
      } satisfies IndicatorDrawing);
      return;
    }
    out.push({
      kind: 'box',
      from: { time: t.entryTime, price: t.entryPrice },
      to: { time: t.exitTime, price: t.exitPrice },
      color: picked ? palette.accent : color,
      fillColor: color,
      opacity: picked ? 0.22 : 0.08,
      lineWidth: picked ? 2 : 1,
    } satisfies IndicatorDrawing);
    out.push({
      kind: 'label',
      // The zone's top-left corner: the box's upper edge at the entry.
      at: { time: t.entryTime, price: Math.max(t.entryPrice, t.exitPrice) },
      text: `${badgePrefix(style)}${pctText(pct)}`,
      color: picked ? palette.accent : color,
      align: 'left',
    } satisfies IndicatorDrawing);
  });
  return out;
}

/**
 * The protective levels a position ran under, as horizontal lines from open to
 * close plus an `SL`/`TP` caption.
 *
 * The lines span exactly the period the levels were live — entry to exit — which
 * is why they are drawn from the position and not from the order: a bracket is
 * only meaningful while the position it protects is open.
 *
 * The captions sit at the right-hand end, `align: 'right'` so the plate extends
 * back inside the bracket rather than past the exit. Two labels at their own
 * price levels read as a pair; putting them at the entry end would collide with
 * the return chip there.
 */
export function bracketDrawings(
  bracket: Bracket | null,
  fromTime: number,
  toTime: number,
  selected = false,
  palette: DrawingPalette = FALLBACK_PALETTE,
  style: RunStyle = {},
): IndicatorDrawing[] {
  if (bracket === null || toTime <= fromTime) return [];
  // A selected position's levels thicken with its zone: the legs of the bracket
  // are part of what was picked, but they keep their risk/reward colours, since
  // the accent is what says "this one" and the pair says which leg is which.
  const width = selected ? 2 : 1;
  // A comparison run's legs take the run's colour and keep `SL`/`TP` in the text:
  // on the price pane two dashed lines are read as *this run's bracket*, and
  // which leg is which is the label's job. The owner keeps the risk/reward pair.
  const stopColor = style.secondary === true ? style.color : palette.down;
  const targetColor = style.secondary === true ? style.color : palette.up;
  return [
    {
      kind: 'line',
      from: { time: fromTime, price: bracket.stop },
      to: { time: toTime, price: bracket.stop },
      color: stopColor,
      lineStyle: 'dashed',
      lineWidth: width,
    },
    {
      kind: 'label',
      at: { time: toTime, price: bracket.stop },
      text: `${badgePrefix(style)}SL ${bracket.stop.toFixed(2)}`,
      color: stopColor,
      align: 'right',
    },
    {
      kind: 'line',
      from: { time: fromTime, price: bracket.target },
      to: { time: toTime, price: bracket.target },
      color: targetColor,
      lineStyle: 'dashed',
      lineWidth: width,
    },
    {
      kind: 'label',
      at: { time: toTime, price: bracket.target },
      text: `${badgePrefix(style)}TP ${bracket.target.toFixed(2)}`,
      color: targetColor,
      align: 'right',
    },
  ];
}

/**
 * The lot the run ended holding, as a level plus a caption — a position with no
 * exit has no box to draw, and drawing one from the last close would claim a
 * fill that never happened. The average entry price is real, so that is drawn.
 */
export function openLotDrawings(
  open: OpenLot | null,
  lastBarTime: number,
  selected = false,
  palette: DrawingPalette = FALLBACK_PALETTE,
  style: RunStyle = {},
): IndicatorDrawing[] {
  if (open === null || lastBarTime <= open.entryTime) return [];
  // A position with no exit fill has no box to select, so the level itself has
  // to carry the selection: accent colour and weight, the same two cues a
  // selected zone takes.
  const colour = open.side === 'buy' ? palette.up : palette.down;
  const color = style.secondary === true ? style.color : selected ? palette.accent : colour;
  return [
    {
      kind: 'line',
      from: { time: open.entryTime, price: open.entryPrice },
      to: { time: lastBarTime, price: open.entryPrice },
      color,
      lineStyle: 'dashed',
      lineWidth: selected ? 2 : 1,
    },
    {
      kind: 'label',
      at: { time: lastBarTime, price: open.entryPrice },
      text: `${badgePrefix(style)}open ${open.side === 'buy' ? '+' : '-'}${open.qty} @ ${open.entryPrice.toFixed(2)}`,
      color,
    },
  ];
}

/**
 * Map fills to the marker primitive's trade shape.
 *
 * `TradingTrade.timestamp` is **milliseconds** — the primitive floors it to
 * seconds before snapping to a bar index — while fills carry UTC seconds, so
 * this is where the unit changes. Rejected fills are skipped: they have no price
 * to draw (the backend sends 0) and are reported in the statistics instead.
 */
export const fillsToTrades = (
  fills: readonly BacktestFill[],
  color?: string,
): TradingTrade[] =>
  fills
    .filter((f) => !f.rejected && f.price > 0)
    .map((f, i) => ({
      id: `${f.time}:${f.price}:${i}`,
      side: isSell(f.side) ? 'sell' : 'buy',
      price: f.price,
      size: f.qty ?? 0,
      timestamp: f.time * 1000,
      variant: 'chevron' as const,
      // The marker primitive takes a per-trade colour, so a comparison run's
      // chevrons say which run they belong to without a legend lookup. The
      // owner keeps the layer's own buy/sell pair.
      color,
      label: f.reason === '' ? undefined : f.reason,
    }));

/**
 * Draw entry/exit markers for the owner and every comparison run.
 *
 * Markers are cleared when the instrument or interval changes — `clearBacktestRuns`
 * empties the registry — because the previous runs described a window that is no
 * longer on screen, and leaving their fills on new candles would claim trades
 * that never happened there.
 */
export function mountBacktestMarkers(widget: Widget): void {
  onRuns((state) => {
    const trades: TradingTrade[] = [];
    if (state.owner !== null) trades.push(...fillsToTrades(state.owner.view.fills));
    for (const run of state.comparing) trades.push(...fillsToTrades(run.view.fills, runColor(run)));
    widget.chart.trading.setTrades(trades);
  });
}

/**
 * Paint position zones and protective brackets on the price pane from whatever
 * the Backtest Equity pane last ran.
 *
 * Host-side rather than a study of its own: the markers are drawn the same way,
 * and a second picker entry for "the other half of the backtest" would make the
 * user add the same run twice. `IndicatorDrawings` is the engine's own shape
 * primitive, so the zones pan, zoom and cull with everything else instead of
 * needing a private canvas overlay.
 */
export function mountBacktestZones(widget: Widget): void {
  const zones = new IndicatorDrawings();
  // Pane 0 is the price pane — the instrument's own scale is what a price-anchored
  // box has to be measured against, and it is also where the fills' bars are.
  widget.chart.addPrimitive(zones, 0);

  /** One run's geometry, in its own style, with the pick applied if it owns it. */
  const shapesFor = (
    run: RunRecord,
    isOwner: boolean,
    picked: TripSelection,
    palette: DrawingPalette,
    lastBarTime: number,
  ): IndicatorDrawing[] => {
    const style = styleFor(run, isOwner);
    const { trips, open } = run.ledger;
    const mine = picked !== null && picked.key === run.key;
    const pickedTrip = mine && picked.kind === 'trip' ? picked.index : null;
    const pickedOpen = mine && picked.kind === 'open';
    return [
      ...zoneDrawings(trips, pickedTrip, palette, style),
      // A closed position's bracket ran from its entry to its exit...
      ...trips.flatMap((t, i) =>
        bracketDrawings(t.bracket, t.entryTime, t.exitTime, i === pickedTrip, palette, style),
      ),
      ...openLotDrawings(open, lastBarTime, pickedOpen, palette, style),
      // ...and an open one's is still live, so it runs to the newest bar.
      ...bracketDrawings(
        open?.bracket ?? null,
        open?.entryTime ?? 0,
        lastBarTime,
        pickedOpen,
        palette,
        style,
      ),
    ];
  };

  const paint = (): void => {
    // Re-read on every paint: an open lot's level runs to the newest bar.
    const bars = widget.series.getData();
    const lastBarTime = bars[bars.length - 1]?.time ?? 0;
    const picked = selectedTrip();
    const palette = paletteOf(widget);
    const state = runState();
    const shapes: IndicatorDrawing[] = [];
    // Comparisons first, the owner last: the run on the chart is what a
    // fill/outline stack should read as the subject, and the owner's filled
    // zones are the ones worth keeping on top.
    for (const run of state.comparing) shapes.push(...shapesFor(run, false, picked, palette, lastBarTime));
    if (state.owner !== null) shapes.push(...shapesFor(state.owner, true, picked, palette, lastBarTime));
    zones.setItems(shapes);
  };

  /** The time span of the picked entry, or null when there is nothing to show. */
  const spanOf = (picked: NonNullable<TripSelection>): { from: number; to: number } | null => {
    const run = runFor(picked.key);
    if (run === null) return null;
    const bars = widget.series.getData();
    const lastBarTime = bars[bars.length - 1]?.time ?? 0;
    if (picked.kind === 'open') {
      const open = run.ledger.open;
      return open === null ? null : { from: open.entryTime, to: lastBarTime };
    }
    const trip = run.ledger.trips[picked.index];
    return trip === undefined ? null : { from: trip.entryTime, to: trip.exitTime };
  };

  onRuns(paint);
  // Picking a row repaints — the zone is what gets emphasised — and brings the
  // trade onto the chart when it is off screen: a selection nobody can see is not
  // a selection. Unpicking only repaints.
  onTripSelection((picked) => {
    paint();
    if (picked === null) return;
    const span = spanOf(picked);
    if (span !== null) revealRange(widget, span.from, span.to);
  });
  // Theme switch restyles the candles the zones sit on; the palette is read per
  // paint, so this is what makes it take effect.
  widget.on('theme', () => paint());
}

/** The descriptor id whose instances produce runs. */
export const BACKTEST_INDICATOR_ID = 'backtest-equity';

/**
 * Keep the registry in step with the panes that produce runs.
 *
 * Two things the panes themselves cannot say, and neither is cosmetic:
 *
 *   * **A closed pane's run goes with it.** Otherwise its markers and brackets
 *     outlive the pane, on a chart where nothing left on screen explains them,
 *     and ownership could sit on a run whose producer no longer exists.
 *   * **A new instrument clears everything.** A zone is anchored on the times it
 *     traded, so on another symbol's bars it is a claim about bars the run never
 *     saw. (The registry drops one run when *its own* fetch finds no data; this
 *     drops all of them when the chart moves.)
 *
 * Liveness is read from the chart's instances rather than tracked from pane
 * events: `indicatorRemoved` does not say what settings an instance had, and a
 * pane that is destroyed with its pane (or restored from a workspace) never
 * fires an add the host would see. Asking the chart what exists is the only
 * answer that stays right through a restore.
 */
export function mountBacktestRuns(widget: Widget): void {
  const liveStrategies = (): Set<string> => {
    const out = new Set<string>();
    for (const instance of widget.chart.indicators()) {
      if (instance.indicatorId !== BACKTEST_INDICATOR_ID) continue;
      const strategy = instance.settings().strategy;
      if (typeof strategy === 'string' && strategy !== '') out.add(strategy);
    }
    return out;
  };

  let reconciling = false;
  const reconcile = (): void => {
    // Reentrant by construction: `removeRun` notifies, and this is a subscriber.
    // The second pass finds nothing to remove and returns, so the recursion is
    // bounded to one level — the guard is here to keep it there if that changes.
    if (reconciling) return;
    reconciling = true;
    try {
      const live = liveStrategies();
      for (const run of runState().runs) {
        if (!live.has(run.key) && !live.has(run.view.strategy)) removeRun(run.key);
      }
    } finally {
      reconciling = false;
    }
  };

  // `layout` is the widget's bus, and it fires for `indicatorRemoved` among the
  // pane events — there is no separate indicator event on the widget surface.
  widget.on('layout', reconcile);
  // A publish that lands after its pane was removed would otherwise resurrect the
  // run and keep it there until the next layout event, which may never come.
  onRuns(reconcile);
  widget.on('symbol', () => clearBacktestRuns());
  widget.on('interval', () => clearBacktestRuns());
  reconcile();
}

