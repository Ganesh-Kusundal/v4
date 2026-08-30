// Trade tier host: maps the backend /api/charts/book onto openalgo-charts'
// TradingController + TradeMarkersPrimitive. Every order mutation rides the
// existing TradexTradeFeed (/orders); this module only feeds state and draws.
import {
  TradingController,
  TradeMarkersPrimitive,
  DEFAULT_TRADING_COLORS,
  type IPrimitive,
  type TradingOrder,
  type TradingPosition,
} from "openalgo-charts";
import { unrealizedPnl, isWorking } from "openalgo-charts/trade";
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

/** Place a bracket (super) order — entry + protective stop/target legs. */
export async function placeBracket(opts: {
  exchange: string; symbol: string; side: "BUY" | "SELL";
  quantity: number; price: number; stopLoss: number; target: number;
}): Promise<string> {
  const resp = await fetch("/orders/bracket", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      exchange: opts.exchange, symbol: opts.symbol, side: opts.side,
      order_type: "LIMIT", quantity: opts.quantity, price: opts.price,
      stop_loss_price: opts.stopLoss, target_price: opts.target,
    }),
  });
  const body = await expectJson<{ order_id: string }>(resp);
  return body.order_id;
}

/** Map /book orders into the reference TradingOrder shape, working only. */
export function mapOrdersToTrading(
  orders: BookRow[],
  _ltpBySymbol: Record<string, number>,
): TradingOrder[] {
  const out: TradingOrder[] = [];
  for (const o of orders) {
    // /book status strings already match the trade tier's OrderStatus union, so
    // isWorking applies directly (pending/working/partial stay, terminal drops).
    if (!isWorking(o as never)) continue;
    out.push({
      id: o.id,
      // The base TradingOrderType union is 'limit'|'stop'|'stop_limit' — no
      // "market" member. A working MARKET order still reads as a resting price
      // line; the controller only uses `type` as the pill label, so map it
      // through and cast.
      type: (o.type === "LIMIT" ? "limit" : "market") as TradingOrder["type"],
      side: (o.side === "BUY" ? "buy" : "sell") as TradingOrder["side"],
      price: o.price,
      size: Math.max(o.qty - o.filledQty, 0),
    });
  }
  return out;
}

/** Map /book positions into the reference TradingPosition shape + PnL text. */
export function mapPositionsToTrading(
  positions: BookPosition[],
  ltpBySymbol: Record<string, number>,
): TradingPosition[] {
  const out: TradingPosition[] = [];
  for (const p of positions) {
    if (p.netQty === 0) continue;
    const ltp = ltpBySymbol[p.symbol] ?? p.avgPrice;
    // unrealizedPnl(position, ltp) = (ltp - avgPrice) * netQty — signed, so a
    // short's PnL is already correct with no side argument.
    const uPnl = unrealizedPnl({ symbol: p.symbol, netQty: p.netQty, avgPrice: p.avgPrice }, ltp);
    out.push({
      id: `${p.exchange}:${p.symbol}`,
      side: p.netQty >= 0 ? "long" : "short",
      entryPrice: p.avgPrice,
      size: Math.abs(p.netQty),
      pnlText: uPnl.toFixed(2),
    });
  }
  return out;
}

export interface TradingHostHandle {
  sync(book: ChartBook): void;
  start(): Promise<void>;
  stop(): void;
}

/** The slice of the Chart instance the host touches (chart implements TradingHost). */
interface ChartHostLike {
  addPrimitive(p: IPrimitive, where?: number): void;
  removePrimitive(p: IPrimitive): void;
}

/**
 * Build the on-chart trade host. The chart itself implements TradingHost
 * (reference core/chart.ts constructs `new TradingController(this)`), so we
 * pass it directly as the controller host; markers attach to pane 0 the same
 * way the P2 profile primitives do.
 */
export function createTradingHost(
  chart: unknown,
  tradeFeed: TradexTradeFeed,
  getLtp: (symbol: string) => number | undefined,
): TradingHostHandle {
  const controller = new TradingController(chart as never);
  const markers = new TradeMarkersPrimitive(DEFAULT_TRADING_COLORS);
  const host = chart as ChartHostLike;
  let markersAttached = false;
  const attachMarkers = (): void => {
    if (markersAttached) return;
    host.addPrimitive(markers, 0);
    markersAttached = true;
  };
  attachMarkers();

  let unsubOrders: (() => void) | null = null;
  let unsubPositions: (() => void) | null = null;

  const sync = (book: ChartBook): void => {
    const orders = book.orders as BookRow[];
    const positions = book.positions as BookPosition[];
    const ltp: Record<string, number> = {};
    for (const o of orders) {
      const v = getLtp(o.symbol);
      if (v !== undefined) ltp[o.symbol] = v;
    }
    for (const p of positions) {
      const v = getLtp(p.symbol);
      if (v !== undefined) ltp[p.symbol] = v;
    }
    controller.syncState({
      orders: mapOrdersToTrading(orders, ltp),
      positions: mapPositionsToTrading(positions, ltp),
    });
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
    attachMarkers();
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
    controller.clear();
    host.removePrimitive(markers);
    markersAttached = false;
  };

  return { sync, start, stop };
}