import type { Widget } from 'openalgo-charts/widget';
import { API_BASE, barSocket } from './feed';
import type { Dock } from './dock';
import { createPanel } from './dock';

/**
 * The account panel: a docked summary of the trading account.
 *
 * Shows balance, margin, net P&L, working orders and open positions — the
 * numbers a trader glances at to know where they stand. Data comes from two
 * sources:
 *
 *   * ``GET /api/charts/account`` — balance, margin, unrealized P&L, positions
 *   * ``GET /api/charts/book``    — working orders (the book endpoint already
 *     exists; we read its orders here instead of re-fetching)
 *
 * The panel refreshes on a 5-second polling loop and immediately after any
 * order/fill frame over the shared WebSocket, so a placed order shows up
 * without waiting for the next tick.
 */

interface AccountPosition {
  symbol: string;
  exchange: string;
  net_qty: number;
  avg_price: number;
  unrealized_pnl: number;
}

interface AccountSnapshot {
  account_id: string;
  balance: string;
  margin: string;
  unrealized_pnl: string;
  positions: AccountPosition[];
}

interface BookOrder {
  id: string;
  symbol: string;
  exchange: string;
  side: string;
  type: string;
  qty: number;
  filledQty: number;
  price: number;
  triggerPrice: number | null;
  status: string;
}

const TERMINAL_STATUSES = new Set(['filled', 'cancelled', 'rejected']);

const money = (v: string): string => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const signed = (v: number): string => `${v >= 0 ? '+' : ''}${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const UP = 'var(--oac-buy)';
const DOWN = 'var(--oac-sell)';
const MUTED = 'var(--oac-mut)';

const STYLE_ID = 'v4-account-css';
const CSS = `
.v4-account__body { padding: 0 0 4px; }
.v4-account__grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; padding: 6px 12px; }
.v4-account__stat { display: flex; flex-direction: column; gap: 1px; }
.v4-account__stat-label { font-size: 10px; color: var(--oac-mut); text-transform: uppercase; letter-spacing: .04em; }
.v4-account__stat-value { font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.v4-account__stat-value.is-positive { color: var(--oac-buy); }
.v4-account__stat-value.is-negative { color: var(--oac-sell); }
.v4-account__section-title { font-size: 10px; font-weight: 600; color: var(--oac-mut); text-transform: uppercase; letter-spacing: .04em; padding: 4px 12px 2px; }
.v4-account__table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
.v4-account__table th { text-align: left; font-size: 10px; font-weight: 600; color: var(--oac-mut); text-transform: uppercase; letter-spacing: .04em; padding: 2px 12px; border-bottom: 1px solid var(--oac-bd-soft); }
.v4-account__table td { padding: 2px 12px; font-size: 11px; border-bottom: 1px solid var(--oac-bd-soft); }
.v4-account__table tr:last-child td { border-bottom: none; }
.v4-account__empty { padding: 4px 12px 6px; font-size: 11px; color: var(--oac-faint); }
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

export function mountAccountPanel(widget: Widget, dock: Dock): void {
  const doc = dock.ownerDocument;
  injectStyle(doc);

  const panel = createPanel(dock, {
    title: 'Account',
    hint: 'Balance, margin, orders & positions',
    collapsed: false,
    maxHeight: 260,
  });

  // -- stats grid (balance / margin / P&L) -------------------------------
  const grid = el(doc, 'div', 'v4-account__grid');
  const balanceStat = el(doc, 'div', 'v4-account__stat');
  const balanceLabel = el(doc, 'span', 'v4-account__stat-label');
  balanceLabel.textContent = 'Balance';
  const balanceValue = el(doc, 'span', 'v4-account__stat-value');
  balanceValue.textContent = '—';
  balanceStat.append(balanceLabel, balanceValue);

  const marginStat = el(doc, 'div', 'v4-account__stat');
  const marginLabel = el(doc, 'span', 'v4-account__stat-label');
  marginLabel.textContent = 'Margin Used';
  const marginValue = el(doc, 'span', 'v4-account__stat-value');
  marginValue.textContent = '—';
  marginStat.append(marginLabel, marginValue);

  const pnlStat = el(doc, 'div', 'v4-account__stat');
  const pnlLabel = el(doc, 'span', 'v4-account__stat-label');
  pnlLabel.textContent = 'Unrealized P&L';
  const pnlValue = el(doc, 'span', 'v4-account__stat-value');
  pnlValue.textContent = '—';
  pnlStat.append(pnlLabel, pnlValue);

  grid.append(balanceStat, marginStat, pnlStat);
  panel.body.appendChild(grid);

  // -- positions --------------------------------------------------------
  const posTitle = el(doc, 'div', 'v4-account__section-title');
  posTitle.textContent = 'Positions';
  panel.body.appendChild(posTitle);

  const posTable = el(doc, 'table', 'v4-account__table');
  const posThead = el(doc, 'thead');
  const posHeadRow = el(doc, 'tr');
  for (const heading of ['Symbol', 'Side', 'Qty', 'Avg Price', 'P&L']) {
    const th = el(doc, 'th');
    th.textContent = heading;
    posHeadRow.appendChild(th);
  }
  posThead.appendChild(posHeadRow);
  const posTbody = el(doc, 'tbody');
  posTable.append(posThead, posTbody);
  panel.body.appendChild(posTable);

  const posEmpty = el(doc, 'div', 'v4-account__empty');
  posEmpty.textContent = 'No open positions.';
  posEmpty.hidden = true;
  panel.body.appendChild(posEmpty);

  // -- working orders ---------------------------------------------------
  const orderTitle = el(doc, 'div', 'v4-account__section-title');
  orderTitle.textContent = 'Working Orders';
  panel.body.appendChild(orderTitle);

  const orderTable = el(doc, 'table', 'v4-account__table');
  const orderThead = el(doc, 'thead');
  const orderHeadRow = el(doc, 'tr');
  for (const heading of ['Symbol', 'Side', 'Type', 'Qty', 'Price']) {
    const th = el(doc, 'th');
    th.textContent = heading;
    orderHeadRow.appendChild(th);
  }
  orderThead.appendChild(orderHeadRow);
  const orderTbody = el(doc, 'tbody');
  orderTable.append(orderThead, orderTbody);
  panel.body.appendChild(orderTable);

  const orderEmpty = el(doc, 'div', 'v4-account__empty');
  orderEmpty.textContent = 'No working orders.';
  orderEmpty.hidden = true;
  panel.body.appendChild(orderEmpty);

  // -- render -----------------------------------------------------------
  let posData: AccountPosition[] = [];
  let orderData: BookOrder[] = [];

  const renderPositions = (): void => {
    posTbody.replaceChildren();
    const hasPositions = posData.length > 0;
    posTable.hidden = !hasPositions;
    posEmpty.hidden = hasPositions;
    for (const p of posData) {
      const row = el(doc, 'tr');
      const side = p.net_qty > 0 ? 'LONG' : 'SHORT';
      const sideColor = p.net_qty > 0 ? UP : DOWN;
      row.append(
        cell(`${p.exchange}:${p.symbol}`, 'left'),
        cell(side, 'left', sideColor),
        cell(String(Math.abs(p.net_qty)), 'right'),
        cell(money(String(p.avg_price)), 'right'),
        cell(signed(p.unrealized_pnl), 'right', p.unrealized_pnl >= 0 ? UP : DOWN),
      );
      posTbody.appendChild(row);
    }
  };

  const renderOrders = (): void => {
    orderTbody.replaceChildren();
    const working = orderData.filter((o) => !TERMINAL_STATUSES.has(o.status));
    const hasOrders = working.length > 0;
    orderTable.hidden = !hasOrders;
    orderEmpty.hidden = hasOrders;
    for (const o of working) {
      const row = el(doc, 'tr');
      row.append(
        cell(`${o.exchange}:${o.symbol}`, 'left'),
        cell(o.side, 'left', o.side === 'BUY' ? UP : DOWN),
        cell(o.type, 'left'),
        cell(`${o.filledQty}/${o.qty}`, 'right'),
        cell(o.price > 0 ? money(String(o.price)) : 'MKT', 'right'),
      );
      orderTbody.appendChild(row);
    }
  };

  const cell = (text: string, align: 'left' | 'right', color?: string): HTMLTableCellElement => {
    const td = el(doc, 'td');
    td.textContent = text;
    td.style.textAlign = align;
    if (color !== undefined) td.style.color = color;
    return td;
  };

  // -- data fetch -------------------------------------------------------
  const fetchAccount = async (): Promise<void> => {
    try {
      const res = await fetch(`${API_BASE}/api/charts/account`);
      if (!res.ok) return;
      const body = (await res.json()) as AccountSnapshot;
      balanceValue.textContent = money(body.balance);
      marginValue.textContent = money(body.margin);
      const pnl = Number(body.unrealized_pnl);
      pnlValue.textContent = signed(pnl);
      pnlValue.classList.toggle('is-positive', pnl >= 0);
      pnlValue.classList.toggle('is-negative', pnl < 0);
      posData = Array.isArray(body.positions) ? body.positions : [];
      renderPositions();
    } catch {
      balanceValue.textContent = '—';
      marginValue.textContent = '—';
      pnlValue.textContent = '—';
    }
  };

  const fetchBook = async (): Promise<void> => {
    try {
      const res = await fetch(`${API_BASE}/api/charts/book`);
      if (!res.ok) return;
      const body = (await res.json().catch(() => undefined)) as
        | { orders?: BookOrder[] }
        | undefined;
      orderData = Array.isArray(body?.orders) ? body?.orders : [];
      renderOrders();
    } catch {
      /* keep last known orders */
    }
  };

  let inFlight = false;
  const refresh = async (): Promise<void> => {
    if (inFlight) return;
    inFlight = true;
    try {
      await Promise.all([fetchAccount(), fetchBook()]);
    } finally {
      inFlight = false;
    }
  };

  // -- lifecycle --------------------------------------------------------
  barSocket.on('order', refresh);
  barSocket.on('fill', refresh);
  void refresh();
  const interval = setInterval(refresh, 5_000);
  // # ponytail: teardown — the host never calls widget.destroy (page-lifetime
  // app), so this interval and the WS handlers are never released; wire them
  // up if a destroy hook appears.
  void widget;
}
