// Trade wiring: maps openalgo-charts' OrderFeed/TradeFeed onto the v4
// backend's REST + WebSocket. All validation that matters (risk, funds,
// buying power) happens in the backend execution spine; the chart tier's
// own UX validation stays on for immediate feedback.
import type { Order, Position } from "openalgo-charts/trade";
import type { PlaceOrder, TradeFeed, UnsubscribeFn } from "openalgo-charts";

export interface ChartBook {
  orders: (Order & { exchange: string })[];
  positions: (Position & { exchange: string })[];
}

/** Fetch one full book snapshot. Idempotent — safe to poll and on reconnect. */
export async function fetchBook(): Promise<ChartBook> {
  const resp = await fetch("/api/charts/book");
  if (!resp.ok) throw new Error(`book fetch failed (${resp.status})`);
  return resp.json() as Promise<ChartBook>;
}

/**
 * TradexTradeFeed implements the library's TradeFeed contract. Orders ride
 * the existing /orders REST endpoints; the host folds ws control messages
 * (`order`, `fill`) into fresh snapshots and calls reconcile.
 */
export class TradexTradeFeed implements TradeFeed {
  async placeOrder(o: PlaceOrder): Promise<{ orderId: string }> {
    const resp = await fetch("/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        exchange: o.exchange,
        symbol: o.symbol,
        side: o.side,
        order_type: o.type,
        quantity: o.qty,
        price: o.price,
        // SL/SL-M carry their trigger in triggerPrice
        trigger_price: o.triggerPrice,
      }),
    });
    if (!resp.ok) throw new Error(`place failed (${resp.status}): ${await resp.text()}`);
    const body = (await resp.json()) as { order_id: string };
    return { orderId: body.order_id };
  }

  async modifyOrder(orderId: string, patch: Partial<PlaceOrder>): Promise<void> {
    const resp = await fetch(`/orders/${encodeURIComponent(orderId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: patch.symbol,
        side: patch.side,
        order_type: patch.type,
        quantity: patch.qty,
        price: patch.price,
      }),
    });
    if (!resp.ok) throw new Error(`modify failed (${resp.status}): ${await resp.text()}`);
  }

  async cancelOrder(orderId: string): Promise<void> {
    const resp = await fetch(`/orders/${encodeURIComponent(orderId)}`, { method: "DELETE" });
    if (!resp.ok) throw new Error(`cancel failed (${resp.status}): ${await resp.text()}`);
  }

  subscribeOrders(cb: (orders: unknown[]) => void): UnsubscribeFn {
    let active = true;
    const poll = window.setInterval(async () => {
      if (!active) return;
      try {
        const book = await fetchBook();
        cb(book.orders);
      } catch {
        // transient failure: keep polling; a later snapshot heals
      }
    }, 1000);
    return () => {
      active = false;
      window.clearInterval(poll);
    };
  }

  subscribePositions(cb: (positions: unknown[]) => void): UnsubscribeFn {
    let active = true;
    const poll = window.setInterval(async () => {
      if (!active) return;
      try {
        const book = await fetchBook();
        cb(book.positions);
      } catch {
        // as above
      }
    }, 1000);
    return () => {
      active = false;
      window.clearInterval(poll);
    };
  }
}
