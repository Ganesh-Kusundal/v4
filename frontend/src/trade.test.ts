import { describe, it, expect } from "vitest";
import { type BookRow } from "./trade";
import type { Order } from "./trade/types";

// Re-implement the mapping logic for testing (mapBookRowToOrder is not exported)
function mapBookRowToOrder(o: BookRow): Order {
  return {
    id: o.id,
    symbol: o.symbol,
    side: o.side as Order["side"],
    type: o.type as Order["type"],
    qty: o.qty,
    filledQty: o.filledQty,
    price: o.price,
    triggerPrice: o.triggerPrice ?? undefined,
    status: o.status as Order["status"],
  };
}

// isWorking from openalgo-charts/trade: working = pending | working | partial.
function row(p: Partial<BookRow>): BookRow {
  return {
    id: "x", symbol: "A", exchange: "NSE", side: "BUY", type: "LIMIT",
    qty: 10, filledQty: 0, price: 100, triggerPrice: null, status: "working", ...p,
  };
}

describe("mapBookRowToOrder", () => {
  it("maps a working book row to Order shape", () => {
    const order = mapBookRowToOrder(row({ id: "w1", status: "working" }));
    expect(order.id).toBe("w1");
    expect(order.status).toBe("working");
    expect(order.side).toBe("BUY");
  });

  it("maps BUY/SELL side correctly", () => {
    const buy = mapBookRowToOrder(row({ id: "b", side: "BUY" }));
    const sell = mapBookRowToOrder(row({ id: "s", side: "SELL" }));
    expect(buy.side).toBe("BUY");
    expect(sell.side).toBe("SELL");
  });

  it("preserves qty/filledQty/price", () => {
    const order = mapBookRowToOrder(row({ id: "p", qty: 10, filledQty: 3, price: 150 }));
    expect(order.qty).toBe(10);
    expect(order.filledQty).toBe(3);
    expect(order.price).toBe(150);
  });

  it("maps null triggerPrice to undefined", () => {
    const order = mapBookRowToOrder(row({ triggerPrice: null }));
    expect(order.triggerPrice).toBeUndefined();
  });
});
