import { describe, it, expect, vi } from "vitest";
import { TradeController, type TradeHost } from "./trade-controller";
import type { Order, Position } from "./types";

/** Minimal mock TradeHost that tracks add/remove calls. */
class MockTradeHost implements TradeHost {
  public added: unknown[] = [];
  public removed: unknown[] = [];

  addPrimitive(p: unknown): void {
    this.added.push(p);
  }

  removePrimitive(p: unknown): void {
    this.removed.push(p);
  }
}

function makeOrder(p: Partial<Order>): Order {
  return {
    id: "o1",
    symbol: "AAPL",
    side: "BUY",
    type: "LIMIT",
    qty: 10,
    filledQty: 0,
    price: 100,
    status: "working",
    ...p,
  };
}

function makePosition(p: Partial<Position>): Position {
  return {
    symbol: "AAPL",
    netQty: 10,
    avgPrice: 100,
    ...p,
  };
}

describe("TradeController.reconcile", () => {
  it("adds new order lines and position markers", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [makeOrder({ id: "o1" })];
    const positions = [makePosition({ symbol: "AAPL" })];
    controller.reconcile(orders, positions);
    expect(controller.orderLineCount()).toBe(1);
    expect(controller.positionCount()).toBe(1);
    expect(host.added.length).toBe(2);
  });

  it("updates existing order lines and markers without adding new ones", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    controller.reconcile([makeOrder({ id: "o1" })], [makePosition({ symbol: "AAPL" })]);
    host.added.length = 0; // clear

    controller.reconcile(
      [makeOrder({ id: "o1", price: 105 })],
      [makePosition({ symbol: "AAPL", avgPrice: 105 })],
    );
    expect(controller.orderLineCount()).toBe(1);
    expect(controller.positionCount()).toBe(1);
    expect(host.added.length).toBe(0); // no new primitives added
  });

  it("removes stale order lines and markers", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    controller.reconcile(
      [makeOrder({ id: "o1" }), makeOrder({ id: "o2" })],
      [makePosition({ symbol: "AAPL" }), makePosition({ symbol: "MSFT" })],
    );
    expect(controller.orderLineCount()).toBe(2);
    expect(controller.positionCount()).toBe(2);

    // Reconcile with only o1 and AAPL — o2 and MSFT should be removed
    controller.reconcile([makeOrder({ id: "o1" })], [makePosition({ symbol: "AAPL" })]);
    expect(controller.orderLineCount()).toBe(1);
    expect(controller.positionCount()).toBe(1);
    expect(host.removed.length).toBe(2);
  });

  it("removes order line when order becomes terminal", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    controller.reconcile([makeOrder({ id: "o1", status: "working" })], []);
    expect(controller.orderLineCount()).toBe(1);

    controller.reconcile([makeOrder({ id: "o1", status: "filled" })], []);
    expect(controller.orderLineCount()).toBe(0);
  });

  it("removes position marker when netQty is 0", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    controller.reconcile([], [makePosition({ symbol: "AAPL", netQty: 10 })]);
    expect(controller.positionCount()).toBe(1);

    controller.reconcile([], [makePosition({ symbol: "AAPL", netQty: 0 })]);
    expect(controller.positionCount()).toBe(0);
  });
});

describe("TradeController bracket planning", () => {
  it("creates BracketGroup when SL/TP orders and position exist", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [
      makeOrder({ id: "sl1", symbol: "AAPL", role: "sl", price: 95, triggerPrice: 95 }),
      makeOrder({ id: "tp1", symbol: "AAPL", role: "tp", price: 110 }),
    ];
    const positions = [makePosition({ symbol: "AAPL", netQty: 10, avgPrice: 100 })];
    controller.reconcile(orders, positions);
    expect(controller.bracketCount()).toBe(1);
    // SL/TP orders are covered by bracket — no separate order lines
    expect(controller.orderLineCount()).toBe(0);
  });

  it("does not create bracket when only one leg exists", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [makeOrder({ id: "sl1", symbol: "AAPL", role: "sl", price: 95 })];
    const positions = [makePosition({ symbol: "AAPL", netQty: 10 })];
    controller.reconcile(orders, positions);
    expect(controller.bracketCount()).toBe(0);
    expect(controller.orderLineCount()).toBe(1); // SL gets its own line
  });

  it("does not create bracket when position is flat", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [
      makeOrder({ id: "sl1", symbol: "AAPL", role: "sl", price: 95 }),
      makeOrder({ id: "tp1", symbol: "AAPL", role: "tp", price: 110 }),
    ];
    const positions = [makePosition({ symbol: "AAPL", netQty: 0 })];
    controller.reconcile(orders, positions);
    expect(controller.bracketCount()).toBe(0);
    expect(controller.orderLineCount()).toBe(2);
  });

  it("updates existing bracket on subsequent reconcile", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    controller.reconcile(
      [
        makeOrder({ id: "sl1", symbol: "AAPL", role: "sl", price: 95 }),
        makeOrder({ id: "tp1", symbol: "AAPL", role: "tp", price: 110 }),
      ],
      [makePosition({ symbol: "AAPL", netQty: 10, avgPrice: 100 })],
    );
    expect(controller.bracketCount()).toBe(1);
    host.added.length = 0;

    // Update bracket with new prices
    controller.reconcile(
      [
        makeOrder({ id: "sl1", symbol: "AAPL", role: "sl", price: 94 }),
        makeOrder({ id: "tp1", symbol: "AAPL", role: "tp", price: 115 }),
      ],
      [makePosition({ symbol: "AAPL", netQty: 10, avgPrice: 100 })],
    );
    expect(controller.bracketCount()).toBe(1);
    expect(host.added.length).toBe(0); // no new bracket added
  });
});

describe("TradeController.isWorking filter", () => {
  it("does not create lines for terminal orders", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [
      makeOrder({ id: "o1", status: "filled" }),
      makeOrder({ id: "o2", status: "cancelled" }),
      makeOrder({ id: "o3", status: "rejected" }),
    ];
    controller.reconcile(orders, []);
    expect(controller.orderLineCount()).toBe(0);
  });

  it("creates lines for pending, working, and partial orders", () => {
    const host = new MockTradeHost();
    const controller = new TradeController(host);
    const orders = [
      makeOrder({ id: "o1", status: "pending" }),
      makeOrder({ id: "o2", status: "working" }),
      makeOrder({ id: "o3", status: "partial" }),
    ];
    controller.reconcile(orders, []);
    expect(controller.orderLineCount()).toBe(3);
  });
});
