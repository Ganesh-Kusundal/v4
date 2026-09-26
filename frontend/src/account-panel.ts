import type { Widget } from 'openalgo-charts/widget';
import { authHeaders } from './apikey';
import { API_BASE, barSocket } from './feed';
import type { Dock } from './dock';
import { createPanel } from './dock';

export interface AccountPosition {
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

export interface BookOrder {
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

export interface AccountPanelOptions {
  canSubmit: () => boolean;
  onError: (message: string) => void;
}

export interface AccountPanelController {
  refresh: () => Promise<void>;
  syncControls: () => void;
}

const TERMINAL_STATUSES = new Set(['filled', 'cancelled', 'canceled', 'rejected']);

const money = (v: string): string => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
};

const signed = (v: number): string =>
  `${v >= 0 ? '+' : ''}${v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const UP = 'var(--oac-buy)';
const DOWN = 'var(--oac-sell)';
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
.v4-account__actions { display: flex; align-items: center; justify-content: flex-end; gap: 4px; white-space: nowrap; }
.v4-account__actions input { width: 58px; height: 22px; padding: 1px 4px; border: 1px solid var(--oac-bd); border-radius: 3px; background: var(--oac-elev); color: var(--oac-tx); font: inherit; font-size: 10px; }
.v4-account__actions button { height: 22px; padding: 1px 6px; border: 1px solid var(--oac-bd); border-radius: 3px; background: var(--oac-elev); color: var(--oac-tx); cursor: pointer; font: inherit; font-size: 10px; }
.v4-account__actions button:hover { border-color: var(--oac-acc); color: var(--oac-acc); }
.v4-account__actions button:disabled { opacity: .4; cursor: not-allowed; }
.v4-account__error { padding: 3px 12px; color: var(--oac-sell); font-size: 10px; }
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

const cell = (doc: Document, text: string, align: 'left' | 'right', color?: string): HTMLTableCellElement => {
  const td = el(doc, 'td');
  td.textContent = text;
  td.style.textAlign = align;
  if (color !== undefined) td.style.color = color;
  return td;
};

const idempotencyHeaders = (json: boolean): Record<string, string> => ({
  ...(json ? { 'Content-Type': 'application/json' } : {}),
  'Idempotency-Key': crypto.randomUUID(),
  ...authHeaders(),
});

const responseMessage = async (res: Response): Promise<string> => {
  const body = (await res.json().catch(() => undefined)) as
    | { error?: { message?: unknown }; detail?: unknown; message?: unknown }
    | undefined;
  const value = body?.error?.message ?? body?.detail ?? body?.message;
  if (typeof value === 'string') return value;
  if (value !== undefined) return JSON.stringify(value);
  return `HTTP ${res.status}`;
};

const numeric = (value: unknown, fallback = 0): number => {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};

const normalizePosition = (value: unknown): AccountPosition | null => {
  if (typeof value !== 'object' || value === null) return null;
  const row = value as Record<string, unknown>;
  const symbol = typeof row.symbol === 'string' ? row.symbol : '';
  const exchange = typeof row.exchange === 'string' ? row.exchange : '';
  if (!symbol || !exchange) return null;
  return {
    symbol,
    exchange,
    net_qty: numeric(row.net_qty ?? row.netQty),
    avg_price: numeric(row.avg_price ?? row.avgPrice),
    unrealized_pnl: numeric(row.unrealized_pnl ?? row.unrealizedPnl),
  };
};

export function mountAccountPanel(
  widget: Widget,
  dock: Dock,
  options: AccountPanelOptions = { canSubmit: () => true, onError: () => undefined },
): AccountPanelController {
  const doc = dock.ownerDocument;
  injectStyle(doc);

  const panel = createPanel(dock, {
    title: 'Account',
    hint: 'Balance, margin, orders & positions',
    collapsed: false,
    maxHeight: 260,
  });

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

  const posTitle = el(doc, 'div', 'v4-account__section-title');
  posTitle.textContent = 'Positions';
  panel.body.appendChild(posTitle);
  const posWrap = el(doc, 'div');
  posWrap.id = 'account-positions';
  const posTable = el(doc, 'table', 'v4-account__table');
  const posThead = el(doc, 'thead');
  const posHeadRow = el(doc, 'tr');
  for (const heading of ['Symbol', 'Side', 'Qty', 'Avg Price', 'P&L', 'Actions']) {
    const th = el(doc, 'th');
    th.textContent = heading;
    posHeadRow.appendChild(th);
  }
  posThead.appendChild(posHeadRow);
  const posTbody = el(doc, 'tbody');
  posTable.append(posThead, posTbody);
  const posEmpty = el(doc, 'div', 'v4-account__empty');
  posEmpty.textContent = 'No open positions.';
  posWrap.append(posTable, posEmpty);
  panel.body.appendChild(posWrap);

  const orderTitle = el(doc, 'div', 'v4-account__section-title');
  orderTitle.textContent = 'Working Orders';
  panel.body.appendChild(orderTitle);
  const orderWrap = el(doc, 'div');
  orderWrap.id = 'account-orders';
  const orderTable = el(doc, 'table', 'v4-account__table');
  const orderThead = el(doc, 'thead');
  const orderHeadRow = el(doc, 'tr');
  for (const heading of ['Symbol', 'Side', 'Type', 'Qty', 'Price', 'Actions']) {
    const th = el(doc, 'th');
    th.textContent = heading;
    orderHeadRow.appendChild(th);
  }
  orderThead.appendChild(orderHeadRow);
  const orderTbody = el(doc, 'tbody');
  orderTable.append(orderThead, orderTbody);
  const orderEmpty = el(doc, 'div', 'v4-account__empty');
  orderEmpty.textContent = 'No working orders.';
  orderWrap.append(orderTable, orderEmpty);
  panel.body.appendChild(orderWrap);

  let posData: AccountPosition[] = [];
  let orderData: BookOrder[] = [];
  let disposed = false;

  const canSubmit = (): boolean => {
    try {
      return options.canSubmit();
    } catch {
      return false;
    }
  };
  const syncControls = (): void => {
    const disabled = !canSubmit();
    for (const button of posWrap.querySelectorAll<HTMLButtonElement>('button[data-action]')) {
      button.disabled = disabled;
    }
    for (const button of orderWrap.querySelectorAll<HTMLButtonElement>('button[data-action]')) {
      button.disabled = disabled;
    }
  };
  const showError = (row: HTMLTableRowElement, message: string): void => {
    let error = row.querySelector<HTMLTableCellElement>('.v4-account__error');
    if (error === null) {
      error = el(doc, 'td', 'v4-account__error');
      error.colSpan = 7;
      error.setAttribute('role', 'alert');
      row.appendChild(error);
    }
    error.hidden = false;
    error.textContent = message;
    options.onError(message);
  };
  const clearError = (row: HTMLTableRowElement): void => {
    const error = row.querySelector<HTMLTableCellElement>('.v4-account__error');
    if (error !== null) {
      error.hidden = true;
      error.textContent = '';
    }
  };

  const cancelOrder = async (order: BookOrder, row: HTMLTableRowElement): Promise<void> => {
    if (!canSubmit()) return;
    try {
      const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(order.id)}`, {
        method: 'DELETE',
        headers: idempotencyHeaders(false),
      });
      if (!res.ok) {
        showError(row, `Cancel failed: ${await responseMessage(res)}`);
        return;
      }
      orderData = orderData.filter((candidate) => candidate.id !== order.id);
      renderOrders();
    } catch (error) {
      showError(row, `Cancel failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const modifyOrder = async (
    order: BookOrder,
    row: HTMLTableRowElement,
    quantityInput: HTMLInputElement,
    priceInput: HTMLInputElement,
  ): Promise<void> => {
    if (!canSubmit()) return;
    const quantity = quantityInput.value.trim();
    const price = priceInput.value.trim();
    if (!/^\d+$/.test(quantity) || Number(quantity) < 1) {
      showError(row, 'Modify failed: quantity must be a positive integer');
      return;
    }
    const body: Record<string, string> = { quantity };
    if (price !== '' && Number.isFinite(Number(price)) && Number(price) > 0) body.price = price;
    try {
      const res = await fetch(`${API_BASE}/orders/${encodeURIComponent(order.id)}`, {
        method: 'PUT',
        headers: idempotencyHeaders(true),
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        showError(row, `Modify failed: ${await responseMessage(res)}`);
        return;
      }
      const current = orderData.find((candidate) => candidate.id === order.id);
      if (current !== undefined) {
        current.qty = Number(quantity);
        if (body.price !== undefined) current.price = Number(body.price);
      }
      clearError(row);
      renderOrders();
    } catch (error) {
      showError(row, `Modify failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const exitPosition = async (position: AccountPosition, row: HTMLTableRowElement): Promise<void> => {
    if (!canSubmit() || position.net_qty === 0) return;
    const body = {
      exchange: position.exchange,
      symbol: position.symbol,
      side: position.net_qty > 0 ? 'SELL' : 'BUY',
      order_type: 'MARKET',
      quantity: Math.abs(position.net_qty),
    };
    try {
      const res = await fetch(`${API_BASE}/orders`, {
        method: 'POST',
        headers: idempotencyHeaders(true),
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        showError(row, `Exit failed: ${await responseMessage(res)}`);
        return;
      }
      posData = posData.filter(
        (candidate) => candidate.exchange !== position.exchange || candidate.symbol !== position.symbol,
      );
      renderPositions();
    } catch (error) {
      showError(row, `Exit failed: ${error instanceof Error ? error.message : String(error)}`);
    }
  };

  const actionButton = (label: string, action: string, id: string): HTMLButtonElement => {
    const button = el(doc, 'button');
    button.type = 'button';
    button.textContent = label;
    button.dataset.action = action;
    button.dataset.orderId = id;
    button.setAttribute('aria-label', label);
    return button;
  };

  function renderPositions(): void {
    posTbody.replaceChildren();
    const hasPositions = posData.length > 0;
    posTable.hidden = !hasPositions;
    posEmpty.hidden = hasPositions;
    for (const position of posData) {
      const row = el(doc, 'tr');
      const side = position.net_qty > 0 ? 'LONG' : 'SHORT';
      const sideColor = position.net_qty > 0 ? UP : DOWN;
      const action = el(doc, 'td');
      const actions = el(doc, 'div', 'v4-account__actions');
      const exit = actionButton('Exit', 'exit', `${position.exchange}:${position.symbol}`);
      exit.disabled = !canSubmit();
      exit.addEventListener('click', () => void exitPosition(position, row));
      actions.appendChild(exit);
      action.appendChild(actions);
      row.append(
        cell(doc, `${position.exchange}:${position.symbol}`, 'left'),
        cell(doc, side, 'left', sideColor),
        cell(doc, String(Math.abs(position.net_qty)), 'right'),
        cell(doc, money(String(position.avg_price)), 'right'),
        cell(doc, signed(position.unrealized_pnl), 'right', position.unrealized_pnl >= 0 ? UP : DOWN),
        action,
      );
      posTbody.appendChild(row);
    }
  }

  function renderOrders(): void {
    orderTbody.replaceChildren();
    const working = orderData.filter((order) => !TERMINAL_STATUSES.has(order.status.toLowerCase()));
    const hasOrders = working.length > 0;
    orderTable.hidden = !hasOrders;
    orderEmpty.hidden = hasOrders;
    for (const order of working) {
      const row = el(doc, 'tr');
      row.dataset.orderId = order.id;
      const action = el(doc, 'td');
      const actions = el(doc, 'div', 'v4-account__actions');
      const quantityInput = el(doc, 'input');
      quantityInput.type = 'number';
      quantityInput.min = '1';
      quantityInput.step = '1';
      quantityInput.value = String(order.qty);
      quantityInput.setAttribute('aria-label', `Quantity for ${order.id}`);
      const priceInput = el(doc, 'input');
      priceInput.type = 'number';
      priceInput.min = '0.01';
      priceInput.step = '0.01';
      priceInput.value = String(order.triggerPrice ?? (order.price > 0 ? order.price : ''));
      priceInput.setAttribute('aria-label', `Price for ${order.id}`);
      const cancel = actionButton('Cancel', 'cancel', order.id);
      const modify = actionButton('Modify', 'modify', order.id);
      cancel.disabled = !canSubmit();
      modify.disabled = !canSubmit();
      cancel.addEventListener('click', () => void cancelOrder(order, row));
      modify.addEventListener('click', () => void modifyOrder(order, row, quantityInput, priceInput));
      actions.append(quantityInput, priceInput, modify, cancel);
      action.appendChild(actions);
      row.append(
        cell(doc, `${order.exchange}:${order.symbol}`, 'left'),
        cell(doc, order.side, 'left', order.side === 'BUY' ? UP : DOWN),
        cell(doc, order.type, 'left'),
        cell(doc, `${order.filledQty}/${order.qty}`, 'right'),
        cell(doc, order.price > 0 ? money(String(order.price)) : order.triggerPrice !== null ? money(String(order.triggerPrice)) : 'MKT', 'right'),
        action,
      );
      orderTbody.appendChild(row);
    }
  }

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
      if (Array.isArray(body.positions)) {
        posData = body.positions.map(normalizePosition).filter((position): position is AccountPosition => position !== null);
        renderPositions();
      }
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
      const body = (await res.json().catch(() => undefined)) as { orders?: unknown; positions?: unknown } | undefined;
      if (Array.isArray(body?.orders)) {
        orderData = body.orders.filter((value): value is Record<string, unknown> => {
          if (typeof value !== 'object' || value === null) return false;
          const row = value as Record<string, unknown>;
          return typeof row.id === 'string' && typeof row.symbol === 'string' && typeof row.exchange === 'string';
        }).map((row) => ({
          id: String(row.id),
          symbol: String(row.symbol),
          exchange: String(row.exchange),
          side: typeof row.side === 'string' ? row.side : '',
          type: typeof row.type === 'string' ? row.type : '',
          qty: numeric(row.qty),
          filledQty: numeric(row.filledQty ?? row.filled_qty),
          price: numeric(row.price),
          triggerPrice: row.triggerPrice === null || row.triggerPrice === undefined ? null : numeric(row.triggerPrice ?? row.trigger_price),
          status: typeof row.status === 'string' ? row.status : '',
        }));
        renderOrders();
      }
      if (Array.isArray(body?.positions) && body.positions.length > 0) {
        posData = body.positions.map(normalizePosition).filter((position): position is AccountPosition => position !== null);
        renderPositions();
      }
    } catch {
      return;
    }
  };

  let inFlight = false;
  let refreshQueued = false;
  const refresh = async (): Promise<void> => {
    if (disposed) return;
    if (inFlight) {
      refreshQueued = true;
      return;
    }
    inFlight = true;
    try {
      await Promise.all([fetchAccount(), fetchBook()]);
    } finally {
      inFlight = false;
      if (refreshQueued) {
        refreshQueued = false;
        void refresh();
      }
    }
  };

  const armOrders = (): void => barSocket.send({ type: 'subscribe_orders' });
  barSocket.onOpen(armOrders);
  armOrders();
  barSocket.on('order', () => void refresh());
  barSocket.on('fill', () => void refresh());
  barSocket.on('replay_started', syncControls);
  barSocket.on('replay_done', syncControls);
  barSocket.on('replay_stopped', syncControls);
  void refresh();
  setInterval(() => void refresh(), 5_000);

  return {
    refresh,
    syncControls,
  };
}
