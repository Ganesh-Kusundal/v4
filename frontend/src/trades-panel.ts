import type { Widget } from 'openalgo-charts/widget';
import {
  onRunLoading,
  onRuns,
  onTripSelection,
  pickTrip,
  runLabel,
  runState,
  selectedTrip,
  tripPnl,
  type RunRecord,
  type RunState,
  type RoundTrip,
  type TripSelection,
} from './backtest';
import type { Dock } from './dock';
import { createPanel } from './dock';

/**
 * The per-trade table: one row per round trip, docked as the widget root's last
 * grid row, below the replay and quantity bars.
 *
 * A canvas table was the first idea — the library ships `ChartTable` and this
 * host already draws the statistics block with it — but it cannot work here: a
 * `ChartTable` hit-tests to its own box (its `hitTest` returns the table's
 * `externalId` and nothing else), so no click can name a row. Rows that select
 * something need real elements, and a list of trades wants the DOM's scrolling,
 * focus and text selection anyway.
 *
 * Rows are built from the run's own ledger — paired once at publish time — which
 * is exactly the list the zone painter draws, so `data-trip` is an index into the
 * zones on screen rather than a parallel count that can drift from them. The open
 * lot gets the last row: no exit fill exists for it in this run, so its exit and
 * P&L cells say that instead of marking it against a price the run never traded
 * at.
 *
 * **The table follows the overlay owner.** With several runs on a chart there are
 * several fill lists, and only one of them is the run drawn on the price pane;
 * a table of any other run would be a list of rows that select nothing. Switching
 * the owner in the runs strip is how you switch which run this describes, and the
 * heading names it so the two are never ambiguous.
 */

/** Every column, its heading and which edge its values read from. */
const COLUMNS: ReadonlyArray<{ label: string; align: 'left' | 'right' }> = [
  { label: '#', align: 'right' },
  { label: 'Side', align: 'left' },
  { label: 'Opened', align: 'left' },
  { label: 'Entry', align: 'right' },
  { label: 'Closed', align: 'left' },
  { label: 'Exit', align: 'right' },
  { label: 'Size', align: 'right' },
  { label: 'P&L', align: 'right' },
  { label: 'Return', align: 'right' },
];

const money = (v: number): string =>
  v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const signed = (v: number): string => `${v < 0 ? '-' : '+'}${money(Math.abs(v))}`;

const size = (v: number): string => v.toLocaleString('en-US', { maximumFractionDigits: 4 });

const pct = (v: number): string => `${v < 0 ? '' : '+'}${v.toFixed(2)}%`;

const UP = 'var(--oac-buy)';
const DOWN = 'var(--oac-sell)';
const MUTED = 'var(--oac-mut)';

/** Injected once per document, like the widget's own sheet. Host chrome, scoped. */
const STYLE_ID = 'v4-trades-css';
const CSS = `
.v4-trades__summary { font-size: 12px; }
.v4-trades__body table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.v4-trades__body th { position: sticky; top: 0; z-index: 1; background: var(--oac-panel); color: var(--oac-mut);
  font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  padding: 4px 12px; border-bottom: 1px solid var(--oac-bd-soft); white-space: nowrap; }
.v4-trades__body td { padding: 3px 12px; font-size: 12px; white-space: nowrap;
  border-bottom: 1px solid var(--oac-bd-soft); }
.v4-trades__body tbody tr { cursor: pointer; }
.v4-trades__body tbody tr:hover { background: var(--oac-elev); }
.v4-trades__body tbody tr.is-picked { background: var(--oac-on-bg); box-shadow: inset 2px 0 0 var(--oac-acc); }
`;

function injectStyle(doc: Document): void {
  if (doc.getElementById(STYLE_ID) !== null) return;
  const style = doc.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

const el = <K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] => {
  const node = doc.createElement(tag);
  if (className !== undefined) node.className = className;
  return node;
};

/** UTC seconds in the chart's own zone, so a row's time matches the axis's. */
const stampMaker = (timeZone: string): Intl.DateTimeFormat =>
  new Intl.DateTimeFormat('en-GB', {
    timeZone,
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });

export function mountTradesPanel(widget: Widget, dock: Dock): void {
  const doc = dock.ownerDocument;
  injectStyle(doc);

  const panel = createPanel(dock, {
    title: 'Trades',
    hint: 'Click a row to mark its zone',
    collapsed: false,
    maxHeight: 200,
  });
  panel.root.setAttribute('aria-label', 'Trades');
  panel.root.dataset.panel = 'trades';

  // The summary goes in the header, after the hint.
  const summary = el(doc, 'span', 'v4-trades__summary');
  summary.setAttribute('aria-live', 'polite');
  panel.head.insertBefore(summary, panel.head.querySelector('.v4-dock__spacer'));

  const table = el(doc, 'table');
  // role=grid is what makes `aria-selected` legal on a row, and this is one: rows
  // are focusable and picking one acts on the chart.
  table.setAttribute('role', 'grid');
  const thead = el(doc, 'thead');
  const headRow = el(doc, 'tr');
  for (const column of COLUMNS) {
    const th = el(doc, 'th');
    th.scope = 'col';
    th.textContent = column.label;
    th.style.textAlign = column.align;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);
  const tbody = el(doc, 'tbody');
  table.append(thead, tbody);
  panel.body.appendChild(table);

  // The chart's zone, resolved lazily and rebuilt when it changes: a host can set
  // a new timezone at runtime, and a table of the wrong hour is worse than none.
  let zone = '';
  let formatter = stampMaker('UTC');
  const stamp = (time: number): string => {
    const current = widget.chart.timezone();
    if (current !== zone) {
      zone = current;
      formatter = stampMaker(current);
    }
    return formatter.format(new Date(time * 1000));
  };

  const cell = (text: string, align: 'left' | 'right', color?: string): HTMLTableCellElement => {
    const td = el(doc, 'td');
    td.textContent = text;
    td.style.textAlign = align;
    if (color !== undefined) td.style.color = color;
    return td;
  };

  /** One closed round trip: the row that can be picked. */
  const tripRow = (trip: RoundTrip, index: number): HTMLTableRowElement => {
    const { amount, pct: ret } = tripPnl(trip);
    const tone = amount >= 0 ? UP : DOWN;
    const row = el(doc, 'tr');
    row.dataset.trip = String(index);
    row.tabIndex = 0;
    row.setAttribute('aria-selected', 'false');
    const dir = trip.side === 'buy' ? UP : DOWN;
    row.append(
      cell(String(index + 1), 'right', MUTED),
      cell(trip.side.toUpperCase(), 'left', dir),
      cell(stamp(trip.entryTime), 'left'),
      cell(money(trip.entryPrice), 'right'),
      cell(stamp(trip.exitTime), 'left'),
      cell(money(trip.exitPrice), 'right'),
      cell(size(trip.qty), 'right'),
      cell(signed(amount), 'right', tone),
      cell(pct(ret), 'right', tone),
    );
    return row;
  };

  /**
   * The lot the run ended holding. Its cells are dashes rather than numbers: the
   * fill list has no exit price for it, and the run's own metrics already carry
   * whatever it was worth at the last bar.
   */
  const openRow = (open: { side: 'buy' | 'sell'; entryTime: number; entryPrice: number; qty: number }): HTMLTableRowElement => {
    const row = el(doc, 'tr');
    row.dataset.open = '1';
    row.tabIndex = 0;
    row.setAttribute('aria-selected', 'false');
    row.title = 'Open at the end of the run: no exit fill to report.';
    row.append(
      cell('—', 'right', MUTED),
      cell(open.side.toUpperCase(), 'left', open.side === 'buy' ? UP : DOWN),
      cell(stamp(open.entryTime), 'left'),
      cell(money(open.entryPrice), 'right'),
      cell('open', 'left', MUTED),
      cell('—', 'right', MUTED),
      cell(size(open.qty), 'right'),
      cell('—', 'right', MUTED),
      cell('—', 'right', MUTED),
    );
    return row;
  };

  /** The run the table is describing: the one the price pane is drawing. */
  let shown: RunRecord | null = null;

  const render = (state: RunState): void => {
    const run = state.owner;
    const ledger = run?.ledger ?? { trips: [] as RoundTrip[], open: null };
    shown = run;
    tbody.replaceChildren();

    for (const [index, trip] of ledger.trips.entries()) tbody.appendChild(tripRow(trip, index));
    if (ledger.open !== null) tbody.appendChild(openRow(ledger.open));

    // What the panel is showing, as attributes: the same numbers the summary
    // states, in a form a test can assert without reading prose.
    panel.root.dataset.trips = String(ledger.trips.length);
    panel.root.dataset.open = ledger.open === null ? '0' : '1';
    panel.root.dataset.strategy = run?.view.strategy ?? '';
    panel.root.dataset.letter = run?.letter ?? '';

    // The letter is only meaningful when there is something to distinguish it
    // from; a lone run is just its strategy.
    const name = run === null ? '' : runLabel(run, state.runs.length > 1);

    if (run === null) {
      summary.textContent = '';
      panel.setEmpty('No backtest run yet — add a Backtest Equity pane above to run a strategy.');
    } else if (ledger.trips.length === 0 && ledger.open === null) {
      summary.textContent = name;
      panel.setEmpty('The run produced no fills in this window.');
    } else {
      // The run's total belongs here rather than in a `<tfoot>`: the list is
      // capped and a 27-row run scrolls it out of sight, which is exactly when a
      // total is wanted. It is the sum of the rows above, before fees.
      const gross = ledger.trips.reduce((sum, t) => sum + tripPnl(t).amount, 0);
      const open = ledger.open === null
        ? ''
        : ` · 1 open (${ledger.open.side === 'buy' ? 'long' : 'short'} ${size(ledger.open.qty)})`;
      summary.textContent = `${name} · ${ledger.trips.length} closed${open} · gross ${signed(gross)}`;
      summary.title =
        'Sum of the closed trades, before fees: the backend reports fees per run, not per trade.';
      panel.setEmpty(null);
    }

    // A run replaces the rows, and publishing one has already dropped the pick
    // that named its trades, so this is a no-op on a fresh run and the sync on a
    // re-render.
    syncPick();
  };

  /** Show a loading message while a run for the owner strategy is in flight. */
  onRunLoading((keys) => {
    const ownerKey = runState().owner?.key;
    if (ownerKey !== undefined && keys.has(ownerKey)) {
      panel.setLoading(`Running ${ownerKey.replace(/_/g, ' ')}…`);
    } else {
      panel.setLoading(null);
    }
  });

  /**
   * Mirror the picked entry onto the rows. The painter reads the same pick.
   *
   * A pick is only ever shown against the run it names: switching the owner
   * leaves the old pick in place (the run still exists), so without this the row
   * of the new run with the same index would look selected and its zone would sit
   * unmarked on the chart.
   */
  const syncPick = (): void => {
    const picked = selectedTrip();
    const mine = shown !== null && picked !== null && picked.key === shown.key;
    for (const row of tbody.querySelectorAll<HTMLElement>('tr[data-trip], tr[data-open]')) {
      const marked =
        mine &&
        picked !== null &&
        (row.dataset.trip !== undefined
          ? picked.kind === 'trip' && String(picked.index) === row.dataset.trip
          : picked.kind === 'open');
      row.setAttribute('aria-selected', marked ? 'true' : 'false');
      row.classList.toggle('is-picked', marked);
    }
  };

  // Null before the first run: a row click with nothing shown cannot happen, and
  // if it somehow does it picks nothing rather than an arbitrary run.
  const pickOf = (row: HTMLElement): TripSelection => {
    if (shown === null) return null;
    return row.dataset.trip === undefined
      ? { key: shown.key, kind: 'open' }
      : { key: shown.key, kind: 'trip', index: Number(row.dataset.trip) };
  };

  const rowFrom = (target: EventTarget | null): HTMLElement | null =>
    target instanceof Element ? target.closest<HTMLElement>('tr[data-trip], tr[data-open]') : null;

  tbody.addEventListener('click', (event) => {
    const row = rowFrom(event.target);
    if (row !== null) pickTrip(pickOf(row));
  });
  // Rows are focusable, so the same pick has to be reachable without a pointer.
  tbody.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const row = rowFrom(event.target);
    if (row === null) return;
    event.preventDefault();
    pickTrip(pickOf(row));
  });

  onRuns(render);
  onTripSelection(syncPick);
  // # ponytail: teardown — the host never calls widget.destroy (page-lifetime app),
  // so the subscriptions above are never released; wire it up if a destroy hook appears.
}
