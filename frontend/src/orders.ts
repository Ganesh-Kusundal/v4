import type { TradingOrder, TradingPosition } from 'openalgo-charts';
import type { OrderRequest, Widget } from 'openalgo-charts/widget';
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
  return {
    id: o.id,
    // Engine type union is limit|stop|stop_limit; SL-M (stop market) has no
    // exact member, stop_limit is the nearest renderable line.
    type: o.type === 'SL' ? 'stop' : o.type === 'SL-M' ? 'stop_limit' : 'limit',
    side: o.side.toLowerCase() === 'sell' ? 'sell' : 'buy',
    price: o.price > 0 ? o.price : (o.triggerPrice ?? 0),
    size: o.qty,
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
export function onOrderEntry(widget: Widget, order: OrderRequest): void {
  const body: Record<string, unknown> = {
    exchange: widget.exchange(),
    symbol: widget.symbol(),
    side: order.side,
    order_type: order.type,
    quantity: 1,
  };
  if (order.type !== 'MARKET' && order.price !== null) body.price = order.price;
  void (async () => {
    const res = await fetch(`${API_BASE}/orders`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify(body),
    });
    const payload = (await res.json().catch(() => undefined)) as
      | { order_id?: string; status?: string; detail?: unknown }
      | undefined;
    if (res.ok) {
      widget.context.toast(`Order ${payload?.order_id ?? ''} ${payload?.status ?? 'submitted'}`.trim(), 'success');
      refreshBook?.();
    } else {
      // FastAPI error envelope: {"detail": "..."}.
      widget.context.toast(`Order rejected: ${String(payload?.detail ?? res.status)}`, 'error');
    }
  })().catch((err) =>
    widget.context.toast(`Order failed: ${err instanceof Error ? err.message : String(err)}`, 'error'),
  );
}
