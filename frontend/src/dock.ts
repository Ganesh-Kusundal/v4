import type { Widget } from 'openalgo-charts/widget';

/**
 * The dock: a single container below the chart widget that hosts every host
 * panel (runs, trades, replay, account, screener).
 *
 * Styled with OpenAlgo Charts reference design tokens (--panel, --elev, --bd, --tx, --acc).
 */

const STYLE_ID = 'v4-dock-css';

const CSS = `
#app { display: flex; flex-direction: column; height: 100vh; }
#app > .oac-widget { flex: 1 1 auto; min-height: 0; height: auto !important; }

.v4-dock {
  flex: 0 0 auto; display: flex; flex-direction: column;
  border-top: 1px solid var(--bd); background: var(--panel);
  color: var(--tx); max-height: 280px; overflow: auto;
  transition: max-height .2s ease;
}
.v4-dock:empty { display: none; }

.v4-dock__tabbar {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  background: #0c0f16; border-bottom: 1px solid var(--bd-soft);
  flex: none; min-height: 28px; overflow-x: auto;
}
.v4-dock__tab {
  font: inherit; font-size: 11px; font-weight: 500; padding: 2px 9px;
  border-radius: 5px; border: 1px solid var(--bd-soft); background: var(--elev);
  color: var(--mut); cursor: pointer; transition: all .15s; white-space: nowrap;
}
.v4-dock__tab:hover { border-color: #33405a; color: var(--tx); background: var(--elev-2); }
.v4-dock__tab.is-active { border-color: var(--acc); color: var(--acc); background: var(--elev-2); font-weight: 600; }
.v4-dock__tab-toggle {
  margin-left: auto; font-size: 10px; text-transform: uppercase; letter-spacing: .5px;
}

.v4-dock__panel {
  display: flex; flex-direction: column; flex: 0 0 auto;
  border-bottom: 1px solid var(--bd-soft); background: var(--panel);
}
.v4-dock__panel[hidden] { display: none !important; }
.v4-dock__panel:last-child { border-bottom: none; }
.v4-dock__panel.is-collapsed .v4-dock__body { display: none; }
.v4-dock__panel.is-collapsed .v4-dock__empty,
.v4-dock__panel.is-collapsed .v4-dock__error { display: none; }

.v4-dock__head {
  display: flex; align-items: center; gap: 8px; padding: 4px 12px;
  flex: none; min-height: 24px; cursor: pointer; user-select: none;
  background: var(--elev); border-bottom: 1px solid var(--bd-soft);
}
.v4-dock__head:hover { background: var(--elev-2); }
.v4-dock__title {
  font-size: 11px; font-weight: 600; letter-spacing: .06em;
  text-transform: uppercase; color: var(--tx);
}
.v4-dock__hint { font-size: 11px; color: var(--faint); }
.v4-dock__spacer { flex: 1; }
.v4-dock__toggle {
  font: inherit; font-size: 11px; padding: 1px 7px; border-radius: 4px;
  border: 1px solid var(--bd); background: var(--elev); color: var(--mut);
  cursor: pointer;
}
.v4-dock__toggle:hover { color: var(--tx); border-color: #33405a; }

.v4-dock__body {
  overflow: auto; overscroll-behavior: contain; background: var(--bg);
  padding: 4px 8px;
}
.v4-dock__body.is-loading { opacity: .45; pointer-events: none; }
.v4-dock__loading {
  display: flex; align-items: center; gap: 8px; padding: 8px 12px;
  font-size: 12px; color: var(--faint);
}
.v4-dock__spinner {
  width: 12px; height: 12px; border: 2px solid var(--bd);
  border-top-color: var(--acc); border-radius: 50%;
  animation: v4-spin .7s linear infinite;
}
@keyframes v4-spin { to { transform: rotate(360deg); } }

.v4-dock__empty {
  padding: 8px 12px; font-size: 12px; color: var(--faint); line-height: 1.4;
}
.v4-dock__empty code { background: var(--elev); padding: 0 4px; border-radius: 3px; }

.v4-dock__error {
  padding: 6px 12px; font-size: 12px; color: var(--sell);
  background: rgba(239, 83, 80, 0.08);
  display: flex; align-items: center; gap: 8px; line-height: 1.3;
}
.v4-dock__error button {
  margin-left: auto; font: inherit; font-size: 11px; padding: 1px 7px;
  border-radius: 4px; border: 1px solid var(--bd); background: var(--elev);
  color: var(--tx); cursor: pointer;
}
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

export type Dock = HTMLDivElement;

let dock: HTMLDivElement | null = null;
let tabbar: HTMLDivElement | null = null;
const registeredPanels: DockPanel[] = [];

export function createDock(widget: Widget): HTMLDivElement {
  if (dock !== null) return dock;
  const doc = widget.root.ownerDocument;
  injectStyle(doc);

  dock = el(doc, 'div', 'v4-dock');
  dock.setAttribute('aria-label', 'Panels');

  tabbar = el(doc, 'div', 'v4-dock__tabbar');
  const collapseAllBtn = el(doc, 'button', 'v4-dock__tab v4-dock__tab-toggle');
  collapseAllBtn.textContent = '▾ Collapse All';
  collapseAllBtn.onclick = () => {
    const allCollapsed = registeredPanels.every((p) => p.isCollapsed());
    for (const p of registeredPanels) {
      p.setCollapsed(!allCollapsed);
    }
    collapseAllBtn.textContent = allCollapsed ? '▾ Collapse All' : '▴ Expand';
  };
  tabbar.appendChild(collapseAllBtn);
  dock.appendChild(tabbar);

  const app = widget.root.parentElement;
  if (app !== null && widget.root.nextSibling !== null) {
    app.insertBefore(dock, widget.root.nextSibling);
  } else {
    widget.root.after(dock);
  }
  return dock;
}

export function dockElement(): HTMLDivElement | null {
  return dock;
}

export interface DockPanel {
  root: HTMLElement;
  head: HTMLElement;
  title: HTMLElement;
  body: HTMLElement;
  tab?: HTMLButtonElement;
  setLoading: (message: string | null) => void;
  setEmpty: (message: string | null) => void;
  setError: (message: string | null) => void;
  setCollapsed: (collapsed: boolean) => void;
  isCollapsed: () => boolean;
}

export interface PanelOptions {
  title: string;
  hint?: string;
  collapsed?: boolean;
  maxHeight?: number;
  onToggle?: (collapsed: boolean) => void;
}

export function createPanel(dockEl: HTMLElement, options: PanelOptions): DockPanel {
  const doc = dockEl.ownerDocument;

  const panel = el(doc, 'section', 'v4-dock__panel');
  panel.setAttribute('aria-label', options.title);

  // Derive panelId for automated tests (e.g. data-panel="screener")
  const id = options.title.toLowerCase().replace(/[^a-z0-9]/g, '');
  panel.setAttribute('data-panel', id);

  // -- header -------------------------------------------------------------
  const head = el(doc, 'div', 'v4-dock__head');
  const title = el(doc, 'span', 'v4-dock__title');
  title.textContent = options.title;
  head.appendChild(title);

  if (options.hint !== undefined) {
    const hint = el(doc, 'span', 'v4-dock__hint');
    hint.textContent = options.hint;
    head.appendChild(hint);
  }

  const spacer = el(doc, 'span', 'v4-dock__spacer');
  head.appendChild(spacer);

  const toggle = el(doc, 'button', 'v4-dock__toggle');
  toggle.type = 'button';
  toggle.textContent = 'Hide';
  toggle.setAttribute('aria-expanded', 'true');
  head.appendChild(toggle);

  // -- body ---------------------------------------------------------------
  const body = el(doc, 'div', 'v4-dock__body');
  if (options.maxHeight !== undefined) body.style.maxHeight = `${options.maxHeight}px`;

  // -- loading ------------------------------------------------------------
  const loading = el(doc, 'div', 'v4-dock__loading');
  loading.hidden = true;
  const spinner = el(doc, 'span', 'v4-dock__spinner');
  const loadingText = el(doc, 'span');
  loading.append(spinner, loadingText);

  // -- empty --------------------------------------------------------------
  const empty = el(doc, 'div', 'v4-dock__empty');
  empty.hidden = true;

  // -- error --------------------------------------------------------------
  const error = el(doc, 'div', 'v4-dock__error');
  error.hidden = true;
  const errorText = el(doc, 'span');
  error.appendChild(errorText);
  const errorDismiss = el(doc, 'button', 'v4-dock__error-dismiss');
  errorDismiss.type = 'button';
  errorDismiss.textContent = 'Dismiss';
  error.appendChild(errorDismiss);

  panel.append(head, loading, empty, error, body);
  dockEl.appendChild(panel);

  // -- tabbar chip --------------------------------------------------------
  let tabBtn: HTMLButtonElement | undefined;
  if (tabbar !== null) {
    tabBtn = el(doc, 'button', 'v4-dock__tab');
    tabBtn.textContent = options.title;
    tabbar.insertBefore(tabBtn, tabbar.lastElementChild);
  }

  // -- state --------------------------------------------------------------
  let collapsed = options.collapsed ?? false;
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
    if (tabBtn) tabBtn.classList.toggle('is-active', !collapsed);
  };

  const setLoading = (message: string | null): void => {
    loadingMsg = message;
    if (message !== null) loadingText.textContent = message;
    apply();
  };

  const setEmpty = (message: string | null): void => {
    emptyMsg = message;
    if (message !== null) empty.textContent = message;
    apply();
  };

  const setError = (message: string | null): void => {
    errorMsg = message;
    if (message !== null) errorText.textContent = message;
    apply();
  };

  const setCollapsed = (value: boolean): void => {
    collapsed = value;
    apply();
  };

  errorDismiss.addEventListener('click', () => setError(null));
  toggle.addEventListener('click', () => {
    collapsed = !collapsed;
    apply();
    options.onToggle?.(collapsed);
  });
  head.addEventListener('click', (ev) => {
    if (ev.target === toggle) return;
    collapsed = !collapsed;
    apply();
    options.onToggle?.(collapsed);
  });

  if (tabBtn) {
    tabBtn.addEventListener('click', () => {
      collapsed = !collapsed;
      apply();
      options.onToggle?.(collapsed);
      if (!collapsed) panel.scrollIntoView({ behavior: 'smooth' });
    });
  }

  if (collapsed) apply();

  const dockPanelObj: DockPanel = {
    root: panel,
    head,
    title,
    body,
    tab: tabBtn,
    setLoading,
    setEmpty,
    setError,
    setCollapsed,
    isCollapsed: () => collapsed,
  };

  registeredPanels.push(dockPanelObj);
  return dockPanelObj;
}
