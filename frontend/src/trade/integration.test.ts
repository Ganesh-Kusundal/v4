import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { OrderEngine } from "./order-engine";
import { TradexTradeFeed } from "../trade-feed";
import type { OrderConstraints } from "./validation";

/** Mock response helper. */
function mockResponse(init: { ok: boolean; statusText: string; body?: unknown }): Response {
  const { ok, statusText, body } = init;
  if (body === undefined) {
    return { ok, statusText, json: async () => ({}) } as Response;
  }
  return { ok, statusText, json: async () => body } as Response;
}

const constraints: OrderConstraints = {
  tickSize: 0.05,
  priceBand: { lower: 90, upper: 110 },
};

// Mock WebSocket for subscription tests.
class MockWebSocket {
  static instances: MockWebSocket[] = [];
  readyState: number = 0; // CONNECTING
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(public url: string) {
    MockWebSocket.instances.push(this);
  }

  close(): void {
    this.readyState = 3; // CLOSED
  }

  /** Test helper: simulate connection open. */
  open(): void {
    this.readyState = 1; // OPEN;
    this.onopen?.();
  }

  /** Test helper: simulate a control message. */
  sendControl(): void {
    this.onmessage?.({ data: JSON.stringify({ type: "control" }) });
  }
}

describe("OrderEngine + TradexTradeFeed integration", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock as unknown as typeof fetch;
    MockWebSocket.instances = [];
    (global as Record<string, unknown>).WebSocket = MockWebSocket;
    // Mock location for WS URL construction
    (global as Record<string, unknown>).location = {
      protocol: "http:",
      host: "localhost:8000",
    };
    // Mock window for setInterval/clearInterval
    (global as Record<string, unknown>).window = {
      setInterval: vi.fn(() => 1 as unknown as ReturnType<typeof setInterval>),
      clearInterval: vi.fn(),
      setTimeout: vi.fn(() => 1 as unknown as ReturnType<typeof setTimeout>),
    };
  });

  afterEach(() => {
    vi.restoreAllMocks();
    delete (global as Record<string, unknown>).WebSocket;
    delete (global as Record<string, unknown>).location;
    delete (global as Record<string, unknown>).window;
  });

  it("placeOrder → POST /orders → intent = SUBMITTED", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: { order_id: "broker-123" } }),
    );

    const feed = new TradexTradeFeed();
    const engine = new OrderEngine({ feed, constraints, armed: true });
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
    });

    expect(res.ok).toBe(true);
    expect(res.intent).toBe("SUBMITTED");
    expect(fetchMock).toHaveBeenCalledWith(
      "/orders",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("modifyOrder → PUT /orders/{id}", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: { order_id: "broker-456" } }),
    );
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: {} }),
    );

    const feed = new TradexTradeFeed();
    const engine = new OrderEngine({ feed, constraints, armed: true, minModifyIntervalMs: 100000 });
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
    });

    // Request a modify (coalesced, not flushed due to minModifyIntervalMs)
    engine.requestModify(res.clientId!, 105);
    // Force send
    await engine.commitModify(res.clientId!);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/orders/broker-456",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("cancelOrder → DELETE /orders/{id}", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: { order_id: "broker-789" } }),
    );
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: {} }),
    );

    const feed = new TradexTradeFeed();
    const engine = new OrderEngine({ feed, constraints, armed: true });
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
    });

    await engine.cancelOrder(res.clientId!);
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/orders/broker-789",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("subscribeOrders callback triggers on WS control message", async () => {
    // Mock fetch for the initial refetchAll after subscribe
    fetchMock.mockResolvedValue(
      mockResponse({ ok: true, statusText: "OK", body: { orders: [], positions: [] } }),
    );

    const feed = new TradexTradeFeed();
    const cb = vi.fn();
    feed.subscribeOrders(cb);

    // WebSocket should have been created
    expect(MockWebSocket.instances.length).toBe(1);
    const ws = MockWebSocket.instances[0];

    // Simulate connection open — triggers refetchAll
    ws.open();
    await new Promise((r) => setTimeout(r, 10));
    expect(cb).toHaveBeenCalled();

    // Simulate control message — triggers refetchAll again
    cb.mockClear();
    ws.sendControl();
    await new Promise((r) => setTimeout(r, 10));
    expect(cb).toHaveBeenCalled();
  });

  it("maps broker fill to SETTLED intent via onBrokerUpdate", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({ ok: true, statusText: "OK", body: { order_id: "broker-fill" } }),
    );

    const feed = new TradexTradeFeed();
    const engine = new OrderEngine({ feed, constraints, armed: true });
    const res = await engine.placeOrder({
      symbol: "AAPL",
      side: "BUY",
      type: "LIMIT",
      qty: 10,
      price: 100,
    });

    // Simulate broker fill event
    engine.onBrokerUpdate("broker-fill", "filled");
    expect(engine.intentState(res.clientId!)).toBe("SETTLED");
  });
});
