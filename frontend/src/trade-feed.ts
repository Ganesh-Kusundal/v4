// Trade wiring: maps openalgo-charts' TradeFeed onto the v4 backend's
// REST + WebSocket. Orders ride POST/PUT/DELETE /orders (execution spine);
// order/fill deltas stream over /ws/stream control queue (fastapi_app.py
// CONTROL_QUEUE_MAX) and trigger an immediate book refetch so the chart's
// TradeController.reconcile sees fresh snapshots without 1s polling.
import type { PlaceOrder, TradeFeed, UnsubscribeFn } from "openalgo-charts";
import { expectJson } from "./http";

export interface ChartBook {
  orders: unknown[];
  positions: unknown[];
}

/** Fetch one full book snapshot. Idempotent — safe on reconnect. */
export async function fetchBook(): Promise<ChartBook> {
  const resp = await fetch("/api/charts/book");
  return expectJson<ChartBook>(resp);
}

// Single WS multiplexer for order/fill control messages. One socket, many
// subscribeOrders/Positions callers; each control message triggers a refetch.
class TradeWsHub {
  private ws: WebSocket | null = null;
  private readonly orderCbs = new Set<(orders: unknown[]) => void>();
  private readonly positionCbs = new Set<(positions: unknown[]) => void>();
  private pollingFallback: number | null = null;
  private reconnectMs = 1000;
  onLiveChange: ((live: boolean) => void) | null = null;

  ensure(): void {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) return;
    this.open();
  }

  private open(): void {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/stream`);
    this.ws = ws;
    ws.onopen = () => {
      this.reconnectMs = 1000;
      this.onLiveChange?.(true);
      void this.refetchAll();
      this.startPollFallback(false);
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as Record<string, unknown>;
        const t = msg["type"] as string;
        if (t === "order" || t === "fill" || t === "order_update" || t === "control") {
          void this.refetchAll();
        }
      } catch { /* ignore */ }
    };
    ws.onclose = () => {
      this.ws = null;
      this.onLiveChange?.(false);
      if (this.orderCbs.size === 0 && this.positionCbs.size === 0) {
        this.stopPollFallback();
        return;
      }
      this.startPollFallback(true);
      window.setTimeout(() => this.open(), this.reconnectMs);
      this.reconnectMs = Math.min(this.reconnectMs * 2, 15000);
    };
  }

  private async refetchAll(): Promise<void> {
    try {
      const book = await fetchBook();
      for (const cb of this.orderCbs) cb(book.orders);
      for (const cb of this.positionCbs) cb(book.positions);
    } catch { /* transient: next control message heals */ }
  }

  private startPollFallback(isFallback: boolean): void {
    this.stopPollFallback();
    // Poll only when WS is down; when WS is healthy we still poll at 30s as
    // a staleness guard (covers missed control frames).
    const interval = isFallback ? 1500 : 30000;
    this.pollingFallback = window.setInterval(() => { void this.refetchAll(); }, interval);
  }

  private stopPollFallback(): void {
    if (this.pollingFallback !== null) { clearInterval(this.pollingFallback); this.pollingFallback = null; }
  }

  subscribeOrders(cb: (orders: unknown[]) => void): UnsubscribeFn {
    this.orderCbs.add(cb);
    this.ensure();
    void this.refetchAll();
    return () => {
      this.orderCbs.delete(cb);
      if (this.orderCbs.size === 0 && this.positionCbs.size === 0) {
        this.ws?.close();
        this.stopPollFallback();
      }
    };
  }

  subscribePositions(cb: (positions: unknown[]) => void): UnsubscribeFn {
    this.positionCbs.add(cb);
    this.ensure();
    void this.refetchAll();
    return () => {
      this.positionCbs.delete(cb);
      if (this.orderCbs.size === 0 && this.positionCbs.size === 0) {
        this.ws?.close();
        this.stopPollFallback();
      }
    };
  }
}

const tradeHub = new TradeWsHub();

export class TradexTradeFeed implements TradeFeed {
  setLiveCallback(cb: (live: boolean) => void): void { tradeHub.onLiveChange = cb; }
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
        trigger_price: o.triggerPrice,
      }),
    });
    const body = await expectJson<{ order_id: string }>(resp);
    return { orderId: body.order_id };
  }

  async modifyOrder(orderId: string, patch: Partial<PlaceOrder>): Promise<void> {
    const resp = await fetch(`/orders/${encodeURIComponent(orderId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(patch.symbol && { symbol: patch.symbol }),
        ...(patch.side && { side: patch.side }),
        ...(patch.type && { order_type: patch.type }),
        ...(patch.qty && { quantity: patch.qty }),
        ...(patch.price && { price: patch.price }),
      }),
    });
    await expectJson(resp);
  }

  async cancelOrder(orderId: string): Promise<void> {
    const resp = await fetch(`/orders/${encodeURIComponent(orderId)}`, { method: "DELETE" });
    await expectJson(resp);
  }

  subscribeOrders(cb: (orders: unknown[]) => void): UnsubscribeFn {
    return tradeHub.subscribeOrders(cb);
  }

  subscribePositions(cb: (positions: unknown[]) => void): UnsubscribeFn {
    return tradeHub.subscribePositions(cb);
  }
}
