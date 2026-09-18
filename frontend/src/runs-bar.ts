import type { Widget } from 'openalgo-charts/widget';
import {
  MAX_COMPARISONS,
  onRunLoading,
  onRuns,
  runColor,
  runLabel,
  setRunOwner,
  toggleRunComparison,
  type RunRecord,
  type RunState,
} from './backtest';
import type { Dock } from './dock';
import { createPanel } from './dock';

/**
 * The runs strip: every live backtest run as one row, side by side.
 *
 * A chart can hold several Backtest Equity panes and until now the only thing
 * that said which run the price pane was drawing was *whichever fetch landed
 * last* — a decision made by request timing, and unknowable from the screen. This
 * strip is where that decision is made and shown:
 *
 *   Chart    which run owns the price pane (one, exclusive)
 *   Compare  which runs are drawn beside it (up to `MAX_COMPARISONS`)
 *
 * The metrics are the same rows the pane's statistics block draws, in the same
 * words and the same units — `Net Profit`, `Sharpe`, `Max Drawdown`, `Trades`,
 * `Fees`, with `total_return`/`max_drawdown` as percentages and the rest to two
 * decimals. A comparison that reformatted the numbers would be inviting the
 * reader to reconcile two renderings of one engine value.
 *
 * **It does not rank the runs.** Numbering a "winner" per column is the obvious
 * next step and it would be a claim the data does not support: the metrics are
 * gross of exposure and turnover, a Sharpe from nine trades is not comparable
 * with one from five hundred, and the engine reports neither. The strip puts the
 * numbers beside each other, which is what "compare" means; the judgement is the
 * trader's.
 *
 * DOM and not a chart overlay, for the reason the trades table is: these rows
 * carry controls, and a control needs a real element with a hit target.
 */

const UP = 'var(--oac-buy)';
const DOWN = 'var(--oac-sell)';
const MUTED = 'var(--oac-mut)';
const ACCENT = 'var(--oac-acc)';

/** The metrics, in the order the pane's statistics block lists them. */
const COLUMNS: ReadonlyArray<{ label: string; align: 'left' | 'right' }> = [
  { label: 'Net Profit', align: 'right' },
  { label: 'Sharpe', align: 'right' },
  { label: 'Max Drawdown', align: 'right' },
  { label: 'Trades', align: 'right' },
  { label: 'Fees', align: 'right' },
];

const STYLE_ID = 'v4-runs-css';
const CSS = `
.v4-runs__market { font-size: 12px; color: var(--oac-mut); }
.v4-runs__body table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.v4-runs__body th { position: sticky; top: 0; z-index: 1; background: var(--oac-panel); color: var(--oac-mut);
  font-size: 11px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
  padding: 4px 12px; border-bottom: 1px solid var(--oac-bd-soft); white-space: nowrap; text-align: right; }
.v4-runs__body th.v4-runs__name { text-align: left; }
.v4-runs__body td { padding: 3px 12px; font-size: 12px; white-space: nowrap;
  border-bottom: 1px solid var(--oac-bd-soft); text-align: right; }
.v4-runs__body th[scope="row"] { display: flex; align-items: center; gap: 6px; text-align: left;
  font-size: 12px; font-weight: 500; text-transform: none; letter-spacing: 0;
  color: var(--oac-tx); padding: 3px 12px; border-bottom: 1px solid var(--oac-bd-soft);
  position: static; background: none; }
.v4-runs__badge { display: inline-flex; align-items: center; justify-content: center;
  min-width: 16px; height: 16px; padding: 0 3px; border-radius: 4px; font-size: 10px;
  font-weight: 700; color: var(--oac-bg); background: var(--v4-run-color); }
.v4-runs__body tbody tr.is-owner th[scope="row"] { box-shadow: inset 2px 0 0 var(--oac-acc); }
.v4-runs__body tbody tr.is-compared { background: var(--oac-on-bg); }
.v4-runs__ctl { display: flex; gap: 4px; justify-content: flex-end; }
.v4-runs__ctl button { font: inherit; font-size: 11px; padding: 1px 7px; border-radius: 4px;
  border: 1px solid var(--oac-bd); background: var(--oac-elev); color: var(--oac-mut); cursor: pointer; }
.v4-runs__ctl button[aria-pressed="true"] { border-color: var(--oac-acc); color: var(--oac-tx);
  background: var(--oac-on-bg); }
.v4-runs__ctl button:disabled { opacity: .45; cursor: default; }
.v4-runs__loading-row td { text-align: center; color: var(--oac-faint); padding: 8px 12px; }
`;

const el = <K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] => {
  const node = doc.createElement(tag);
  if (className !== undefined) node.className = className;
  return node;
};

function injectStyle(doc: Document): void {
  if (doc.getElementById(STYLE_ID) !== null) return;
  const style = doc.createElement('style');
  style.id = STYLE_ID;
  style.textContent = CSS;
  doc.head.appendChild(style);
}

/** Matches the pane's statistics block, character for character. */
const pct = (v: number): string => `${(v * 100).toFixed(2)}%`;
const fixed = (v: number): string => v.toFixed(2);

export function mountRunsBar(widget: Widget, dock: Dock): void {
  const doc = dock.ownerDocument;
  injectStyle(doc);

  const panel = createPanel(dock, {
    title: 'Runs',
    hint: `Chart picks the run on the price pane · Compare draws it beside (up to ${MAX_COMPARISONS})`,
    collapsed: false,
    maxHeight: 200,
  });

  // The market label goes in the header, after the hint.
  const market = el(doc, 'span', 'v4-runs__market');
  panel.head.insertBefore(market, panel.head.querySelector('.v4-dock__spacer'));
  panel.root.setAttribute('data-panel', 'runs');

  // Build the table structure once; rows are swapped on render.
  const table = el(doc, 'table');
  table.setAttribute('role', 'grid');
  const thead = el(doc, 'thead');
  const headRow = el(doc, 'tr');
  const nameHead = el(doc, 'th', 'v4-runs__name');
  nameHead.scope = 'col';
  nameHead.textContent = 'Run';
  headRow.appendChild(nameHead);
  for (const column of COLUMNS) {
    const th = el(doc, 'th');
    th.scope = 'col';
    th.textContent = column.label;
    th.style.textAlign = column.align;
    headRow.appendChild(th);
  }
  const ctlHead = el(doc, 'th');
  ctlHead.scope = 'col';
  ctlHead.textContent = 'Price pane';
  headRow.appendChild(ctlHead);
  thead.appendChild(headRow);
  const tbody = el(doc, 'tbody');
  table.append(thead, tbody);
  panel.body.appendChild(table);

  const cell = (text: string, color?: string, title?: string): HTMLTableCellElement => {
    const td = el(doc, 'td');
    td.textContent = text;
    if (color !== undefined) td.style.color = color;
    if (title !== undefined) td.title = title;
    return td;
  };

  /** Net Profit and Sharpe get a colour: two of them are read against each other. */
  const tone = (v: number): string => (v >= 0 ? UP : DOWN);

  const rowFor = (run: RunRecord, state: RunState): HTMLTableRowElement => {
    const m = run.view.metrics;
    const isOwner = state.owner?.key === run.key;
    const isCompared = state.comparing.some((r) => r.key === run.key);

    const row = el(doc, 'tr');
    row.dataset.run = run.view.strategy;
    row.dataset.letter = run.letter;
    row.dataset.owner = isOwner ? '1' : '0';
    row.dataset.compare = isCompared ? '1' : '0';
    row.classList.toggle('is-owner', isOwner);
    row.classList.toggle('is-compared', isCompared);

    const name = el(doc, 'th');
    name.scope = 'row';
    const badge = el(doc, 'span', 'v4-runs__badge');
    badge.textContent = run.letter;
    badge.style.setProperty('--v4-run-color', isOwner ? ACCENT : runColor(run));
    const label = el(doc, 'span');
    label.textContent = runLabel(run, false);
    name.append(badge, label);
    row.appendChild(name);

    row.append(
      cell(pct(m.total_return), tone(m.total_return)),
      cell(fixed(m.sharpe), tone(m.sharpe)),
      cell(pct(m.max_drawdown), MUTED, 'Reported by the engine as the worst peak-to-trough fall.'),
      cell(String(m.num_trades), undefined, m.num_rejected > 0 ? `${m.num_rejected} rejected` : undefined),
      cell(fixed(m.total_fees), MUTED),
    );

    const ctl = el(doc, 'td', 'v4-runs__ctl');
    const chart = el(doc, 'button');
    chart.type = 'button';
    chart.textContent = 'Chart';
    chart.dataset.act = 'owner';
    chart.setAttribute('aria-pressed', String(isOwner));
    chart.title = `Show ${run.letter} ${run.view.strategy} on the price pane`;
    chart.addEventListener('click', () => {
      if (isOwner) {
        const others = state.runs.filter((r) => r.key !== run.key);
        const target = others.reduce((a, b) => (a.seq >= b.seq ? a : b));
        if (target) {
          setRunOwner(target.key);
        }
      } else {
        setRunOwner(run.key);
      }
    });

    const compare = el(doc, 'button');
    compare.type = 'button';
    compare.textContent = 'Compare';
    compare.dataset.act = 'compare';
    compare.setAttribute('aria-pressed', String(isCompared));
    compare.setAttribute('aria-label', `Compare ${run.letter} ${run.view.strategy} on the price pane`);
    if (isOwner) {
      compare.disabled = true;
      compare.title = 'The run on the price pane — it is already drawn.';
    } else if (!isCompared && state.comparing.length >= MAX_COMPARISONS) {
      compare.disabled = true;
      compare.title = `${MAX_COMPARISONS} comparisons is where the outlines hide the candles they are drawn over.`;
    }
    compare.addEventListener('click', () => toggleRunComparison(run.key));

    ctl.append(chart, compare);
    row.appendChild(ctl);
    return row;
  };

  /** A loading row for a run that is being computed. */
  const loadingRowFor = (key: string): HTMLTableRowElement => {
    const row = el(doc, 'tr');
    row.classList.add('v4-runs__loading-row');
    const cell = el(doc, 'td');
    cell.colSpan = COLUMNS.length + 2;
    cell.textContent = `Running ${key.replace(/_/g, ' ')}…`;
    row.appendChild(cell);
    return row;
  };

  let loadingKeys: ReadonlySet<string> = new Set();
  let latestState: RunState = { runs: [], owner: null, comparing: [] };

  /** Re-render the table from the latest known state and loading keys. */
  const draw = (): void => {
    tbody.replaceChildren();
    for (const run of latestState.runs) tbody.appendChild(rowFor(run, latestState));
    for (const key of loadingKeys) {
      if (!latestState.runs.some((r) => r.key === key)) tbody.appendChild(loadingRowFor(key));
    }

    const nothingHappening = latestState.runs.length === 0 && loadingKeys.size === 0;
    // Hide the panel entirely when there is nothing to show — the trades panel
    // already tells the user how to create a run, and two hints in one dock
    // reads as two widgets. Show the empty message only when something is
    // loading (the panel is visible for the loading rows).
    panel.root.hidden = nothingHappening;
    panel.setEmpty(null);
    panel.root.dataset.runs = latestState.runs.map((r) => r.letter).join('');
    panel.root.dataset.owner = latestState.owner?.letter ?? '';
    panel.root.dataset.compare = latestState.comparing.map((r) => r.letter).join('');
    panel.root.dataset.owner_strategy = latestState.owner?.view.strategy ?? '';
    market.textContent =
      latestState.owner === null
        ? ''
        : `${latestState.owner.market.symbol} · ${latestState.owner.market.interval}`;
  };

  const render = (state: RunState): void => {
    latestState = state;
    draw();
  };

  onRuns(render);
  onRunLoading((keys) => {
    loadingKeys = keys;
    draw();
  });
  // # ponytail: teardown — the host never calls widget.destroy (page-lifetime app),
  // so the subscriptions above are never released; wire them up if a destroy hook appears.
}
