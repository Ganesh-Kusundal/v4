import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  OrderEngine,
  type OrderEngineOptions,
  type OrderFeed,
  type PlaceRequest,
  type PreflightFailure,
  isPreflightFailure,
} from "./order-engine";
import type { OrderConstraints } from "./validation";
import type { OrderStatus } from "./types";

/** A controllable mock OrderFeed for testing OrderEngine. */
class MockOrderFeed implements OrderFeed {
  public placeMock = vi.fn<(req: PlaceRequest & { mode: "live" | "analyzer" }) => Promise<{ orderId: string }>>();
  public modifyMock = vi.fn<(orderId: string, patch: { price?: number; triggerPrice?: number; qty?: number }) => Promise<void>>();
  public cancelMock = vi.fn<(orderId: string) => Promise<void>>();

  async place(req: PlaceRequest & { mode: "live" | "analyzer" }): Promise<{ orderId: string }> {
    return this.placeMock(req);
  }

  async modify(orderId: string, patch: { price?: number; triggerPrice?: number; qty?: number }): Promise<void> {
    return this.modifyMock(orderId, patch);
  }

  async cancel(orderId: string): Promise<void> {
    return this.cancelMock(orderId);
  }
}

const constraints: OrderConstraints = {
  tickSize: 0.05,
  priceBand: { lower: 90, upper: 110 },
};

function makeEngine(feed: MockOrderFeed, overrides: Partial<OrderEngineOptions> = {}): OrderEngine {
  return new OrderEngine({
    feed,
    constraints,
    armed: true,
    idGen: () => `id-${++makeEngine.counter}`,
    now: () => ++makeEngine.time,
    ...overrides,
  });
}
makeEngine.counter = 0;
makeEngine.time = 0;

describe("OrderEngine.placeOrder", () => {
  let feed: MockOrderFeed;
  let engine: OrderEngine;

  beforeEach(() => {
    feed = new MockOrderFeed();
    feed.placeMock.mockResolvedValue({ orderId: "broker-1" });
    makeEngine.counter = 0;
    makeEngine.time = 0;
    engine = makeEngine(feed);
  });

  it("succeeds and returns SUBMITTED intent on happy path", async () => {
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
    });
    expect(res.ok).toBe(true);
    expect(res.intent).toBe("SUBMITTED");
    expect(res.state).toBe("working");
    expect(res.clientId).toBe("id-1");
    expect(feed.placeMock).toHaveBeenCalledTimes(1);
  });

  it("fails validation for zero qty", async () => {
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 0,
      price: 100,
    });
    expect(res.ok).toBe(false);
    expect(res.intent).toBe("BLOCKED");
    expect(feed.placeMock).not.toHaveBeenCalled();
  });

  it("fails validation for out-of-band price", async () => {
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 200,
    });
    expect(res.ok).toBe(false);
    expect(res.intent).toBe("BLOCKED");
    expect(feed.placeMock).not.toHaveBeenCalled();
  });

  it("returns duplicate on same clientToken (idempotency)", async () => {
    const req: PlaceRequest = {
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-1",
    };
    const first = await engine.placeOrder(req);
    const second = await engine.placeOrder(req);
    expect(first.ok).toBe(true);
    expect(second.ok).toBe(false);
    expect(second.clientId).toBe("tok-1");
    expect(feed.placeMock).toHaveBeenCalledTimes(1);
  });

  it("returns BLOCKED on preflight failure and releases token", async () => {
    const preflightErr: PreflightFailure = { preflight: true };
    feed.placeMock.mockRejectedValueOnce(preflightErr);
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-preflight",
    });
    expect(res.ok).toBe(false);
    expect(res.intent).toBe("BLOCKED");
    // Token is released, so retry should attempt again
    feed.placeMock.mockResolvedValueOnce({ orderId: "broker-2" });
    const retry = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-preflight",
    });
    expect(retry.ok).toBe(true);
    expect(feed.placeMock).toHaveBeenCalledTimes(2);
  });

  it("returns AMBIGUOUS on transport failure (keeps token)", async () => {
    feed.placeMock.mockRejectedValueOnce(new Error("network down"));
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-ambig",
    });
    expect(res.ok).toBe(false);
    expect(res.intent).toBe("AMBIGUOUS");
    // Token is kept, so retry is blocked
    const retry = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-ambig",
    });
    expect(retry.ok).toBe(false);
    expect(retry.reason).toContain("duplicate");
    expect(feed.placeMock).toHaveBeenCalledTimes(1);
  });

  it("blocks when gate declines", async () => {
    const gate = vi.fn().mockResolvedValue(false);
    const gatedEngine = makeEngine(feed, { armed: false, gate });
    const res = await gatedEngine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "tok-gate",
    });
    expect(res.ok).toBe(false);
    expect(res.intent).toBe("BLOCKED");
    expect(feed.placeMock).not.toHaveBeenCalled();
    // Token released, can retry
    expect(gate).toHaveBeenCalledTimes(1);
  });
});

describe("OrderEngine OCO", () => {
  let feed: MockOrderFeed;
  let engine: OrderEngine;

  beforeEach(() => {
    feed = new MockOrderFeed();
    feed.placeMock.mockResolvedValue({ orderId: "broker-1" });
    feed.cancelMock.mockResolvedValue(undefined);
    makeEngine.counter = 0;
    makeEngine.time = 0;
    engine = makeEngine(feed);
  });

  it("cancels peer when one fills (OCO)", async () => {
    const resA = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "oco-a",
    });
    const resB = await engine.placeOrder({
      symbol: "AAPL",
      side: "SELL",
      type: "LIMIT",
      qty: 10,
      price: 110,
      clientToken: "oco-b",
    });
    engine.linkOco("oco-a", "oco-b");

    // Simulate broker fill on A
    feed.placeMock.mockResolvedValueOnce({ orderId: "broker-1" });
    engine.onFill("broker-1", true);

    // Peer B should be cancelled
    expect(feed.cancelMock).toHaveBeenCalled();
  });
});

describe("OrderEngine drag-modify", () => {
  let feed: MockOrderFeed;
  let engine: OrderEngine;

  beforeEach(() => {
    feed = new MockOrderFeed();
    feed.placeMock.mockResolvedValue({ orderId: "broker-1" });
    feed.modifyMock.mockResolvedValue(undefined);
    makeEngine.counter = 0;
    makeEngine.time = 0;
    engine = makeEngine(feed);
  });

  it("coalesces requestModify into one commit", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "mod-1",
    });
    // First requestModify flushes immediately (last = -Infinity)
    engine.requestModify("mod-1", 101);
    // Subsequent calls within the window are coalesced
    engine.requestModify("mod-1", 102);
    engine.requestModify("mod-1", 103);
    // Wait for async flush
    await new Promise((r) => setTimeout(r, 10));
    // Only one call made (first call flushed immediately)
    expect(feed.modifyMock).toHaveBeenCalledTimes(1);
    expect(feed.modifyMock).toHaveBeenCalledWith("broker-1", { price: 101 });
  });

  it("commitModify sends pending patch", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "mod-2",
    });
    // Use a fresh engine with a now() that prevents immediate flush
    const engine2 = makeEngine(feed, { minModifyIntervalMs: 100000 });
    await engine2.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "mod-2",
    });
    engine2.requestModify("mod-2", 105);
    await engine2.commitModify("mod-2");
    expect(feed.modifyMock).toHaveBeenCalledTimes(1);
  });

  it("rejects invalid modify price via onValidationError", async () => {
    const onValidationError = vi.fn();
    engine = makeEngine(feed, { onValidationError });
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "mod-3",
    });
    // Out of band price
    engine.requestModify("mod-3", 200);
    expect(onValidationError).toHaveBeenCalled();
    expect(feed.modifyMock).not.toHaveBeenCalled();
  });
});

describe("OrderEngine.onBrokerUpdate", () => {
  let feed: MockOrderFeed;
  let engine: OrderEngine;

  beforeEach(() => {
    feed = new MockOrderFeed();
    feed.placeMock.mockResolvedValue({ orderId: "broker-1" });
    makeEngine.counter = 0;
    makeEngine.time = 0;
    engine = makeEngine(feed);
  });

  it("maps pending status to ACKNOWLEDGED intent (no state change)", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "stat-1",
    });
    engine.onBrokerUpdate("broker-1", "pending");
    expect(engine.intentState("stat-1")).toBe("ACKNOWLEDGED");
    expect(engine.state("stat-1")).toBe("working"); // unchanged
  });

  it("maps filled status to SETTLED intent", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "stat-2",
    });
    engine.onBrokerUpdate("broker-1", "filled");
    expect(engine.intentState("stat-2")).toBe("SETTLED");
    expect(engine.state("stat-2")).toBe("filled");
  });

  it("maps cancelled status to SETTLED intent", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "stat-3",
    });
    engine.onBrokerUpdate("broker-1", "cancelled");
    expect(engine.intentState("stat-3")).toBe("SETTLED");
  });

  it("maps rejected status to SETTLED intent", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "stat-4",
    });
    engine.onBrokerUpdate("broker-1", "rejected");
    expect(engine.intentState("stat-4")).toBe("SETTLED");
  });
});

describe("OrderEngine reconcile", () => {
  let feed: MockOrderFeed;
  let engine: OrderEngine;

  beforeEach(() => {
    feed = new MockOrderFeed();
    feed.placeMock.mockResolvedValue({ orderId: "broker-1" });
    makeEngine.counter = 0;
    makeEngine.time = 0;
    engine = makeEngine(feed);
  });

  it("beginReconcile sets RECONCILING on non-terminal orders", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "recon-1",
    });
    engine.beginReconcile();
    expect(engine.intentState("recon-1")).toBe("RECONCILING");
  });

  it("onReconnect with present order returns ACKNOWLEDGED", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "recon-2",
    });
    engine.beginReconcile();
    engine.onReconnect(new Set(["broker-1"]));
    expect(engine.intentState("recon-2")).toBe("ACKNOWLEDGED");
  });

  it("onReconnect with missing order sets AMBIGUOUS", async () => {
    await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
      clientToken: "recon-3",
    });
    engine.beginReconcile();
    engine.onReconnect(new Set()); // broker-1 not present
    expect(engine.intentState("recon-3")).toBe("AMBIGUOUS");
  });
});

describe("isPreflightFailure", () => {
  it("returns true for PreflightFailure objects", () => {
    expect(isPreflightFailure({ preflight: true })).toBe(true);
  });

  it("returns false for other errors", () => {
    expect(isPreflightFailure(new Error("boom"))).toBe(false);
    expect(isPreflightFailure(null)).toBe(false);
    expect(isPreflightFailure(undefined)).toBe(false);
    expect(isPreflightFailure("string")).toBe(false);
    expect(isPreflightFailure({ preflight: false })).toBe(false);
  });
});
