// Trade tier host: maps the backend /api/charts/book onto openalgo-charts'
// TradeController (read path) + OrderEngine (write path). Two independent peers
// consume the same book snapshots:
//
//   - TradeController.reconcile(orders, positions) → on-chart primitives
//   - OrderEngine.placeOrder() → intent tracking, OCO, drag-modify
//
// Order mutations ride the existing TradexTradeFeed (/orders); this module only
// feeds state, draws, and tracks intent.
import { OrderEngine, type OrderFeed } from "./trade/order-engine";
import { TradeController, type TradeHost } from "./trade/trade-controller";
import type { Order, Position } from "./trade/types";
import { TradexTradeFeed, fetchBook, type ChartBook } from "./trade-feed";
import { expectJson } from "./http";

/** One /api/charts/book order row (trade-tier vocabulary). */
export interface BookRow {
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

/** One /api/charts/book position row. */
export interface BookPosition {
  symbol: string;
  exchange: string;
  netQty: number;
  avgPrice: number;
}

/** Map a backend book row to the trade-tier Order shape. */
function mapBookRowToOrder(o: BookRow): Order {
  return {
    id: o.id,
    symbol: o.symbol,
    // The base OrderSide union is 'BUY'|'SELL' — direct pass-through.
    side: o.side as Order["side"],
    // The base OrderType union is 'MARKET'|'LIMIT'|'SL'|'SL-M' — direct pass-through.
    type: o.type as Order["type"],
    qty: o.qty,
    filledQty: o.filledQty,
    price: o.price,
    triggerPrice: o.triggerPrice ?? undefined,
    // Backend status strings already match the trade-tier OrderStatus union
    // (pending/working/partial/filled/cancelled/rejected).
    status: o.status as Order["status"],
  };
}

/** Map a backend book position to the trade-tier Position shape. */
function mapBookPositionToPosition(p: BookPosition): Position {
  return {
    symbol: p.symbol,
    netQty: p.netQty,
    avgPrice: p.avgPrice,
  };
}

export interface TradingHostHandle {
  sync(book: ChartBook): void;
  start(): Promise<void>;
  stop(): void;
  /** The OrderEngine peer — exposes placeOrder/cancelOrder for UI wiring. */
  readonly orderEngine: OrderEngine;
}

/**
 * Build the on-chart trade host. The chart itself implements TradeHost
 * (reference core/chart.ts constructs `new TradeController(this)`), so we
 * pass it directly as the controller host.
 *
 * Two peers are wired:
 *   - TradeController: reconciles book snapshots → primitives
 *   - OrderEngine: tracks client intent via TradexTradeFeed (OrderFeed)
 */
export function createTradingHost(
  chart: unknown,
  tradeFeed: TradexTradeFeed,
  orderFeed: OrderFeed,
  getLtp: (symbol: string) => number | undefined,
): TradingHostHandle {
  const controller = new TradeController(chart as TradeHost);
  const orderEngine = new OrderEngine({
    feed: orderFeed,
    constraints: { tickSize: 0.05 }, // NSE equity default; overridden by priceBand below
    armed: true, // fire immediately — no confirm gate for v4
  });

  let unsubOrders: (() => void) | null = null;
  let unsubPositions: (() => void) | null = null;

  const sync = (book: ChartBook): void => {
    const orders = (book.orders as BookRow[]).map(mapBookRowToOrder);
    const positions = (book.positions as BookPosition[]).map(mapBookPositionToPosition);
    // Push LTP into the controller for live P&L / distance labels.
    for (const o of orders) {
      const ltp = getLtp(o.symbol);
      if (ltp !== undefined) controller.onLtp(o.symbol, ltp);
    }
    for (const p of positions) {
      const ltp = getLtp(p.symbol);
      if (ltp !== undefined) controller.onLtp(p.symbol, ltp);
    }
    controller.reconcile(orders, positions);
  };

  const refresh = async (): Promise<void> => {
    try {
      sync(await fetchBook());
    } catch {
      // transient: the next WS control message / poll fallback heals us
    }
  };

  const start = async (): Promise<void> => {
    await refresh();
    unsubOrders?.();
    unsubPositions?.();
    unsubOrders = tradeFeed.subscribeOrders(() => {
      void refresh();
    });
    unsubPositions = tradeFeed.subscribePositions(() => {
      void refresh();
    });
  };

  const stop = (): void => {
    unsubOrders?.();
    unsubOrders = null;
    unsubPositions?.();
    unsubPositions = null;
  };

  return { sync, start, stop, orderEngine };
}
