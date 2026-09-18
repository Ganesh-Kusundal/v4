import type { Widget } from 'openalgo-charts/widget';
import type { Dock } from './dock';

/**
 * Screener panel — runs the backend's discovered scanners and surfaces the
 * candidates in a table. Each candidate carries a "Send to chart" action that
 * flips the widget to that symbol, so a trader can go from scan to chart in
 * one click.
 *
 * The backend exposes three scanners (momentum, pullback, nifty500_technical)
 * via `GET /api/charts/strategies` and runs them via `POST /api/charts/scanner/run`.
 * The scan is offline and deterministic — the market provider reads the datalake,
 * no broker calls — so results are reproducible.
 */

export interface ScannerMeta {
  id: string;
  universe_size: number;
  conditions: string[];
  rank_by: string;
  limit: number;
}

export interface ScannerCandidate {
  symbol: string;
  exchange: string;
  score: number;
  matched: string[];
  values: Record<string, number | null>;
  rank: number;
}

interface ScannerRun {
  scanner: string;
  window_days: number;
  results: ScannerCandidate[];
}

const API_BASE = '';

async function fetchScanners(): Promise<ScannerMeta[]> {
  const res = await fetch(`${API_BASE}/api/charts/strategies`);
  if (!res.ok) throw new Error(`strategies ${res.status}`);
  const data = await res.json();
  return data.scanners ?? [];
}

async function runScanner(id: string, windowDays: number): Promise<ScannerRun> {
  const res = await fetch(`${API_BASE}/api/charts/scanner/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, window_days: windowDays }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error((detail as Record<string, string>).detail ?? `scan ${res.status}`);
  }
  return res.json();
}

/**
 * Mount the screener panel in the dock. Returns nothing — the panel owns its
 * own lifecycle for the lifetime of the dock.
 */
export function mountScreener(widget: Widget, dock: Dock): void {
  const panel = createScreenerPanel(dock);
  const state = panel;

  // -- scanner selector row -----------------------------------------------
  const selectorRow = el(state.body.ownerDocument, 'div', 'v4-screener__selector');
  selectorRow.style.cssText = 'display:flex;gap:8px;align-items:center;padding:6px 12px;flex-wrap:wrap;';

  const scannerSelect = el(state.body.ownerDocument, 'select');
  scannerSelect.style.cssText = 'background:var(--oac-elev);color:var(--oac-tx);border:1px solid var(--oac-bd);border-radius:4px;padding:3px 6px;font:inherit;font-size:12px;';

  const windowLabel = el(state.body.ownerDocument, 'span');
  windowLabel.textContent = 'Window (days):';
  windowLabel.style.cssText = 'font-size:11px;color:var(--oac-mut);';

  const windowInput = el(state.body.ownerDocument, 'input');
  windowInput.type = 'number';
  windowInput.min = '5';
  windowInput.max = '120';
  windowInput.value = '30';
  windowInput.style.cssText = 'width:56px;background:var(--oac-elev);color:var(--oac-tx);border:1px solid var(--oac-bd);border-radius:4px;padding:3px 6px;font:inherit;font-size:12px;';

  const runBtn = el(state.body.ownerDocument, 'button');
  runBtn.type = 'button';
  runBtn.textContent = 'Run scan';
  runBtn.style.cssText = 'font:inherit;font-size:12px;padding:3px 10px;border-radius:4px;border:1px solid var(--oac-bd);background:var(--oac-acc);color:#fff;cursor:pointer;';

  const spacer = el(state.body.ownerDocument, 'span');
  spacer.style.flex = '1';

  const status = el(state.body.ownerDocument, 'span');
  status.style.cssText = 'font-size:11px;color:var(--oac-faint);';

  selectorRow.append(scannerSelect, windowLabel, windowInput, runBtn, spacer, status);
  state.body.appendChild(selectorRow);

  // -- results table -------------------------------------------------------
  const tableWrap = el(state.body.ownerDocument, 'div');
  tableWrap.style.cssText = 'overflow:auto;max-height:160px;';

  const table = el(state.body.ownerDocument, 'table');
  table.style.cssText = 'width:100%;border-collapse:collapse;font-size:12px;';

  const colgroup = el(state.body.ownerDocument, 'colgroup');
  for (const _ of [0, 1, 2, 3, 4]) {
    const col = el(state.body.ownerDocument, 'col');
    colgroup.appendChild(col);
  }
  // last column (actions) narrow
  const lastCol = el(state.body.ownerDocument, 'col');
  lastCol.style.width = '90px';
  colgroup.appendChild(lastCol);
  table.appendChild(colgroup);

  const thead = el(state.body.ownerDocument, 'thead');
  thead.style.cssText = 'position:sticky;top:0;background:var(--oac-panel);';
  const headerRow = el(state.body.ownerDocument, 'tr');
  for (const header of ['Rank', 'Symbol', 'Score', 'Matched', 'Conditions', '']) {
    const th = el(state.body.ownerDocument, 'th');
    th.textContent = header;
    th.style.cssText = 'text-align:left;padding:4px 8px;font-size:11px;color:var(--oac-mut);border-bottom:1px solid var(--oac-bd-soft);white-space:nowrap;';
    headerRow.appendChild(th);
  }
  thead.appendChild(headerRow);
  table.appendChild(thead);

  const tbody = el(state.body.ownerDocument, 'tbody');
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  state.body.appendChild(tableWrap);

  // -- populate scanner selector ------------------------------------------
  async function loadScanners(): Promise<void> {
    state.setLoading('Loading scanners…');
    try {
      const scanners = await fetchScanners();
      scannerSelect.innerHTML = '';
      if (scanners.length === 0) {
        const opt = el(state.body.ownerDocument, 'option');
        opt.textContent = 'No scanners available';
        opt.value = '';
        scannerSelect.appendChild(opt);
        state.setEmpty('No scanners discovered. Add a ScannerDefinition to the scanners package.');
        return;
      }
      for (const sc of scanners) {
        const opt = el(state.body.ownerDocument, 'option');
        opt.value = sc.id;
        opt.textContent = `${sc.id} (${sc.universe_size} names, ${sc.conditions.length} conditions)`;
        scannerSelect.appendChild(opt);
      }
      // Clear the empty/loading state so the body (and the select) shows.
      state.setEmpty(null);
      state.setLoading(null);
    } catch (err) {
      state.setError(`Failed to load scanners: ${(err as Error).message}`);
    }
  }

  // -- run a scan ----------------------------------------------------------
  async function executeScan(): Promise<void> {
    const id = scannerSelect.value;
    if (!id) {
      state.setError('No scanner selected.');
      return;
    }
    const windowDays = Math.max(5, Math.min(120, parseInt(windowInput.value, 10) || 30));
    state.setLoading(`Running ${id} over ${windowDays} days…`);
    runBtn.disabled = true;
    try {
      const run = await runScanner(id, windowDays);
      // Clear the loading state so the body (and results) become visible.
      state.setLoading(null);
      renderResults(run);
      state.setEmpty(null);
      status.textContent = `${run.results.length} candidates · ${run.window_days}d window`;
    } catch (err) {
      state.setError(`Scan failed: ${(err as Error).message}`);
    } finally {
      runBtn.disabled = false;
    }
  }

  // -- render results ------------------------------------------------------
  function renderResults(run: ScannerRun): void {
    tbody.innerHTML = '';
    if (run.results.length === 0) {
      state.setEmpty('No candidates matched the screen. Try a wider window or a different scanner.');
      return;
    }
    state.setEmpty(null);
    const frag = state.body.ownerDocument.createDocumentFragment();
    for (const c of run.results) {
      const tr = el(state.body.ownerDocument, 'tr');
      tr.style.cssText = 'border-bottom:1px solid var(--oac-bd-soft);';

      const cells = [
        `#${c.rank}`,
        c.symbol,
        c.score.toFixed(2),
        `${c.matched.length} condition${c.matched.length === 1 ? '' : 's'}`,
        c.matched.join(', ') || '—',
      ];
      for (const text of cells) {
        const td = el(state.body.ownerDocument, 'td');
        td.textContent = text;
        td.style.cssText = 'padding:4px 8px;white-space:nowrap;';
        tr.appendChild(td);
      }
      // action cell
      const actionTd = el(state.body.ownerDocument, 'td');
      actionTd.style.cssText = 'padding:4px 8px;';
      const sendBtn = el(state.body.ownerDocument, 'button');
      sendBtn.type = 'button';
      sendBtn.textContent = 'Chart';
      sendBtn.style.cssText = 'font:inherit;font-size:11px;padding:1px 8px;border-radius:4px;border:1px solid var(--oac-bd);background:var(--oac-elev);color:var(--oac-tx);cursor:pointer;';
      sendBtn.addEventListener('click', () => {
        widget.setSymbol(c.symbol, c.exchange);
        status.textContent = `Viewing ${c.symbol} · ${run.scanner}`;
      });
      actionTd.appendChild(sendBtn);
      tr.appendChild(actionTd);
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
  }

  // -- wire events ---------------------------------------------------------
  runBtn.addEventListener('click', () => void executeScan());

  // initial load
  void loadScanners();
}

// ---------------------------------------------------------------------------
// Minimal panel creation — mirrors the dock's createPanel but with a simpler
// contract tailored to the screener (no collapse toggle, no header click).
// ---------------------------------------------------------------------------

interface ScreenerPanel {
  body: HTMLElement;
  setLoading: (message: string | null) => void;
  setEmpty: (message: string | null) => void;
  setError: (message: string | null) => void;
}

function createScreenerPanel(dock: Dock): ScreenerPanel {
  const doc = dock.ownerDocument;

  const panel = el(doc, 'section', 'v4-dock__panel');
  panel.setAttribute('aria-label', 'Screener');
  panel.dataset.panel = 'screener';

  // header
  const head = el(doc, 'div', 'v4-dock__head');
  const title = el(doc, 'span', 'v4-dock__title');
  title.textContent = 'Screener';
  head.appendChild(title);
  const spacer = el(doc, 'span', 'v4-dock__spacer');
  head.appendChild(spacer);
  const toggle = el(doc, 'button', 'v4-dock__toggle');
  toggle.type = 'button';
  toggle.textContent = 'Hide';
  toggle.setAttribute('aria-expanded', 'true');
  head.appendChild(toggle);

  // body
  const body = el(doc, 'div', 'v4-dock__body');

  // loading
  const loading = el(doc, 'div', 'v4-dock__loading');
  loading.hidden = true;
  const spinner = el(doc, 'span', 'v4-dock__spinner');
  const loadingText = el(doc, 'span');
  loading.append(spinner, loadingText);

  // empty
  const empty = el(doc, 'div', 'v4-dock__empty');
  empty.hidden = true;

  // error
  const error = el(doc, 'div', 'v4-dock__error');
  error.hidden = true;
  const errorText = el(doc, 'span');
  error.appendChild(errorText);
  const errorDismiss = el(doc, 'button', 'v4-dock__error-dismiss');
  errorDismiss.type = 'button';
  errorDismiss.textContent = 'Dismiss';
  error.appendChild(errorDismiss);

  panel.append(head, loading, empty, error, body);
  dock.appendChild(panel);

  let collapsed = false;
  let loadingMsg: string | null = null;
  let emptyMsg: string | null = null;
  let errorMsg: string | null = null;

  const apply = (): void => {
    const showLoading = loadingMsg !== null;
    const showEmpty = !showLoading && emptyMsg !== null;
    const showError = errorMsg !== null;
    loading.hidden = !showLoading;
    empty.hidden = !showEmpty;
    error.hidden = !showError;
    body.hidden = showLoading || showEmpty;
    body.classList.toggle('is-loading', showLoading);
    panel.classList.toggle('is-collapsed', collapsed);
    toggle.textContent = collapsed ? 'Show' : 'Hide';
    toggle.setAttribute('aria-expanded', String(!collapsed));
  };

  toggle.addEventListener('click', () => {
    collapsed = !collapsed;
    apply();
  });
  head.addEventListener('click', (ev) => {
    if (ev.target === toggle) return;
    collapsed = !collapsed;
    apply();
  });
  errorDismiss.addEventListener('click', () => {
    errorMsg = null;
    apply();
  });

  return {
    body,
    setLoading: (msg) => {
      loadingMsg = msg;
      if (msg !== null) loadingText.textContent = msg;
      apply();
    },
    setEmpty: (msg) => {
      emptyMsg = msg;
      if (msg !== null) empty.textContent = msg;
      apply();
    },
    setError: (msg) => {
      errorMsg = msg;
      if (msg !== null) errorText.textContent = msg;
      apply();
    },
  };
}

// ---------------------------------------------------------------------------
// Shared DOM helper (local copy to avoid a second import site).
// ---------------------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
  doc: Document,
  tag: K,
  className?: string,
): HTMLElementTagNameMap[K] {
  const node = doc.createElement(tag);
  if (className !== undefined) node.className = className;
  return node;
}
