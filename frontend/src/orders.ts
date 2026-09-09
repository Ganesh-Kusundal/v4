import type { TradingOrder, TradingPosition } from 'openalgo-charts';
import type { OrderRequest, Widget } from 'openalgo-charts/widget';
import { authHeaders } from './apikey';
import { API_BASE, barSocket } from './feed';

/** GET /api/charts/book row shapes (backend chart vocabulary, numeric). */
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

interface BookPosition {
  symbol: string;
  exchange: string;
  netQty: number;
  avgPrice: number;
}

/** Only working orders draw price lines; a filled market row has no price to sit at. */
const TERMINAL_STATUSES = new Set(['filled', 'cancelled', 'rejected']);

function mapOrder(o: BookOrder): TradingOrder | null {
  if (TERMINAL_STATUSES.has(o.status)) return null;
  // A working MARKET row carries neither price nor trigger — there is no
  // level for its line to sit at, so it draws no line (instead of one at 0).
  const price = o.price > 0 ? o.price : (o.triggerPrice ?? 0);
  if (price <= 0) return null;
  return {
    id: o.id,
    // Engine type union is limit|stop|stop_limit; backend SL (STOP, trigger
    // only) -> 'stop', SL-M (STOP_LIMIT) -> 'stop_limit'.
    type: o.type === 'SL' ? 'stop' : o.type === 'SL-M' ? 'stop_limit' : 'limit',
    side: o.side.toLowerCase() === 'sell' ? 'sell' : 'buy',
    price,
    size: Math.max(o.qty - o.filledQty, 0),
  };
}

function mapPosition(p: BookPosition): TradingPosition {
  return {
    id: `${p.exchange}:${p.symbol}`,
    side: p.netQty > 0 ? 'long' : 'short',
    entryPrice: p.avgPrice,
    size: Math.abs(p.netQty),
  };
}

/** Set by mountOrders so a context-menu placement can trigger an eager refetch. */
let refreshBook: (() => void) | null = null;

/**
 * Order + position lines from the engine's push-render TradingController
 * (chart.trading, the chart-owned instance). Book snapshots every 5s plus an
 * immediate throttled refetch whenever an order/fill frame arrives over WS.
 */
export function mountOrders(widget: Widget): void {
  const trading = widget.chart.trading;
  let inFlight = false;
  const refresh = (): void => {
    if (inFlight) return;
    inFlight = true;
    void (async () => {
      const res = await fetch(`${API_BASE}/api/charts/book`);
      if (!res.ok) return;
      const body = (await res.json().catch(() => undefined)) as
        | { orders?: BookOrder[]; positions?: BookPosition[] }
        | undefined;
      trading.setOrders((body?.orders ?? []).map(mapOrder).filter((o): o is TradingOrder => o !== null));
      trading.setPositions((body?.positions ?? []).map(mapPosition));
    })()
      .catch((err) => console.warn('book refresh failed', err))
      .finally(() => {
        inFlight = false;
      });
  };
  refreshBook = refresh;

  // Connection-scoped: re-armed on every (re)open so a server restart resubscribes.
  const sendSubscribe = (): void => barSocket.send({ type: 'subscribe_orders' });
  barSocket.onOpen(sendSubscribe);
  sendSubscribe();
  barSocket.on('order', refresh);
  barSocket.on('fill', refresh);

  void refresh();
  setInterval(refresh, 5000);
  // Page-lifetime surface; the host has no teardown path.
}

/**
 * Widget right-click order entry (context menu onOrder hook). Sends
 * POST /orders with an idempotency key; the context menu carries no quantity
 * control, so M3 places one unit per click.
 */
/**
 * Engine context-menu type -> backend OrderType value. The menu emits
 * MARKET (price null), LIMIT (price = pointer price) and SL (price =
 * pointer price); the backend's paper broker accepts STOP with a
 * trigger_price only, so 'SL' rides STOP with trigger_price set. LIMIT
 * needs its price; anything else the menu may grow is suppressed here
 * rather than sent as a broken request.
 */
function mapMenuType(order: OrderRequest): { order_type: string; trigger_price?: number } | null {
  switch (order.type) {
    case 'MARKET':
      return { order_type: 'MARKET' };
    case 'LIMIT':
      return order.price === null ? null : { order_type: 'LIMIT' };
    case 'SL':
      return order.price === null ? null : { order_type: 'STOP', trigger_price: order.price };
    default:
      return null;
  }
}

/** FastAPI error envelopes carry string or structured 422 `detail`. */
function detailMessage(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail;
  if (detail === undefined || detail === null) return fallback;
  try {
    return JSON.stringify(detail);
  } catch {
    return fallback;
  }
}

export function onOrderEntry(widget: Widget, order: OrderRequest): void {
  const mapped = mapMenuType(order);
  if (mapped === null) {
    widget.context.toast(`Order rejected: ${order.type} is not supported by the backend`, 'error');
    return;
  }
  const body: Record<string, unknown> = {
    exchange: widget.exchange(),
    symbol: widget.symbol(),
    side: order.side,
    quantity: 1,
    ...mapped,
  };
  if (order.type === 'LIMIT') body.price = order.price;
  void (async () => {
    const res = await fetch(`${API_BASE}/orders`, {
      method: 'POST',
      // POST /orders sits behind verify_api_key; keyless paper omits the header.
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': crypto.randomUUID(),
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    });
    const payload = (await res.json().catch(() => undefined)) as
      | { order_id?: string; status?: string; detail?: unknown }
      | undefined;
    if (res.ok) {
      widget.context.toast(`Order ${payload?.order_id ?? ''} ${payload?.status ?? 'submitted'}`.trim(), 'success');
      refreshBook?.();
    } else {
      widget.context.toast(`Order rejected: ${detailMessage(payload?.detail, String(res.status))}`, 'error');
    }
  })().catch((err) =>
    widget.context.toast(`Order failed: ${err instanceof Error ? err.message : String(err)}`, 'error'),
  );
}
