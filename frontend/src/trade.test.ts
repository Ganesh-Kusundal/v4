import { describe, it, expect } from "vitest";
import { mapOrdersToTrading, type BookRow } from "./trade";

// isWorking from openalgo-charts/trade: working = pending | working | partial.
function row(p: Partial<BookRow>): BookRow {
  return {
    id: "x", symbol: "A", exchange: "NSE", side: "BUY", type: "LIMIT",
    qty: 10, filledQty: 0, price: 100, triggerPrice: null, status: "working", ...p,
  };
}

describe("mapOrdersToTrading", () => {
  it("includes working orders and filters terminal ones", () => {
    const orders = [
      row({ id: "w1", status: "working" }),
      row({ id: "w2", status: "pending" }),
      row({ id: "w3", status: "partial" }),
      row({ id: "t1", status: "filled" }),
      row({ id: "t2", status: "cancelled" }),
    ];
    const out = mapOrdersToTrading(orders, {});
    expect(out.map((o) => o.id)).toEqual(["w1", "w2", "w3"]);
  });

  it("maps BUY/SELL side correctly", () => {
    const orders = [
      row({ id: "b", side: "BUY" }),
      row({ id: "s", side: "SELL" }),
    ];
    const out = mapOrdersToTrading(orders, {});
    expect(out.find((o) => o.id === "b")?.side).toBe("buy");
    expect(out.find((o) => o.id === "s")?.side).toBe("sell");
  });

  it("computes remaining size as qty - filledQty", () => {
    const orders = [row({ id: "p", qty: 10, filledQty: 3 })];
    const out = mapOrdersToTrading(orders, {});
    expect(out[0].size).toBe(7);
  });
});
