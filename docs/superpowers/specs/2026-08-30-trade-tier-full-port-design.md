# Trade Tier Full Port — Design Spec

**Date:** 2026-08-30
**Status:** Approved
**Scope:** Port openalgo-charts-master trade tier to v4, integrate with backend

---

## 1. Architecture

### 1.1 Principle

**Backend owns order state. Client tracks intent. Peers, not layers.**

The backend `ExecutionEngine` is the single source of truth for order lifecycle (NEW→PENDING→ACK→FILLED). The client-side `OrderEngine` tracks **intent** — what *this client* submitted — without duplicating backend state. Two independent peers consume the same book snapshots:

- `TradeController` (read path): reconciles backend book → on-chart primitives
- `OrderEngine` (write path): tracks client intent, OCO linking, drag-modify coalescing

### 1.2 Component Diagram

```
                    Backend (single source of truth)
                    ┌─────────────────────────────────┐
                    │  ExecutionEngine + Domain OMS    │
                    │  /api/charts/book + WS control   │
                    └──────────┬──────────────────────┘
                               │ book snapshots
              ┌────────────────┼────────────────┐
              │                │                │
    ┌─────────┴─────────┐     │     ┌──────────┴──────────┐
    │   TradeController  │     │     │   OrderEngine        │
    │   (read path)      │     │     │   (intent tracking)  │
    │                    │     │     │                      │
    │  reconcile(book)   │     │     │  placeOrder()        │
    │  → primitives      │     │     │  → intent = SUBMITTED│
    │                     │     │     │  onBrokerUpdate()    │
    └────────┬────────────┘     │     │  → intent = ACK'D    │
             │                  │     │  OCO linking         │
    ┌────────┴────────┐        │     │  drag coalescing     │
    │  Primitives      │        │     └──────────┬───────────┘
    │  WorkingOrderLine│        │                │
    │  PositionMarker  │        │                │
    │  BracketGroup    │        │                │
    └──────────────────┘        │                │
                                │                │
                                └────────────────┘
                                      │
                              ┌───────┴────────┐
                              │ TradexTradeFeed │
                              │ (write path)    │
                              │ implements      │
                              │ OrderFeed       │
                              └─────────────────┘
```

### 1.3 Data Flows

**Flow 1 — Place Order:**
```
User clicks Buy → OrderEngine.placeOrder(req)
  → validate (tick/band/freeze)
  → claim idempotency token
  → TradexTradeFeed.place(req) → POST /orders
  → backend ACK → intent = SUBMITTED
  → WS control queue → OrderEngine.onBrokerUpdate()
  → intent = ACKNOWLEDGED
```

**Flow 2 — Reconcile (read path, independent):**
```
WS control queue / poll → fetchBook() → ChartBook
  → TradeController.reconcile(orders, positions)
  → update WorkingOrderLine / PositionMarker / BracketGroup
```

**Flow 3 — OCO:**
```
Order A fills → OrderEngine.onBrokerUpdate(A, 'filled')
  → cancel OCO peer B → TradexTradeFeed.cancel(B)
  → backend cancels B
```

**Flow 4 — Drag-Modify:**
```
User drags order line → OrderEngine.requestModify(clientId, price)
  → coalesce (rate-limit 150ms)
  → validate new price
  → TradexTradeFeed.modify(brokerId, patch) → PUT /orders/{id}
```

---

## 2. File Map

### 2.1 New Files (ported from reference)

| File | Source | Purpose |
|------|--------|---------|
| `frontend/src/trade/types.ts` | `trade/types.ts` | Order, Position, OrderStatus, OrderType, OrderRole, isWorking |
| `frontend/src/trade/order-state-machine.ts` | `trade/order-state-machine.ts` | ClientOrderState, transition, isTerminal |
| `frontend/src/trade/order-engine.ts` | `trade/order-engine.ts` | OrderEngine, OrderFeed interface, IntentState |
| `frontend/src/trade/trade-controller.ts` | `trade/trade-controller.ts` | TradeController.reconcile() |
| `frontend/src/trade/order-line.ts` | `trade/order-line.ts` | WorkingOrderLine primitive |
| `frontend/src/trade/position.ts` | `trade/position.ts` | PositionMarker primitive |
| `frontend/src/trade/bracket.ts` | `trade/bracket.ts` | BracketGroup primitive |
| `frontend/src/trade/pnl.ts` | `trade/pnl.ts` | unrealizedPnl, unrealizedPnlPercent, riskReward, bracketValid |
| `frontend/src/trade/validation.ts` | `trade/validation.ts` | validateOrder, validateQuantity, validatePrice, OrderConstraints |
| `frontend/src/trade/pill.ts` | `render/pill.ts` | drawPill, drawPillGroup, withAlpha, shade, contrastText |

### 2.2 Modified Files

| File | Change |
|------|--------|
| `frontend/src/trade.ts` | Remove mapOrdersToTrading/mapPositionsToTrading (moved to TradeController). Update createTradingHost to wire OrderEngine + TradeController as peers. Keep placeBracket. |
| `frontend/src/trade-feed.ts` | TradexTradeFeed implements OrderFeed interface. Add place() signature match. |
| `frontend/src/main.ts` | Wire OrderEngine + TradeController as peers. OrderEngine gets TradexTradeFeed. TradeController gets book snapshots from TradeWsHub. |

### 2.3 Test Files

| File | Coverage |
|------|----------|
| `frontend/src/trade/order-engine.test.ts` | Place/modify/cancel lifecycle, OCO cancel-on-fill, idempotency (duplicate token), drag-modify coalescing |
| `frontend/src/trade/order-state-machine.test.ts` | All state transitions, terminal detection |
| `frontend/src/trade/pnl.test.ts` | unrealizedPnl, unrealizedPnlPercent, riskReward, bracketValid |
| `frontend/src/trade/validation.test.ts` | tick-size snap, price band, freeze qty, lot size, fractional qty |
| `frontend/src/trade/bracket.test.ts` | R:R calculation, bracket validity for BUY/SELL |
| `frontend/src/trade/trade-controller.test.ts` | Reconcile with mock book snapshot, primitive add/update/remove |
| `frontend/src/trade/integration.test.ts` | OrderEngine + TradexTradeFeed with mock fetch |

---

## 3. Key Interfaces

### 3.1 OrderFeed (write path — TradexTradeFeed implements this)

```typescript
interface OrderFeed {
  place(req: PlaceRequest): Promise<{ orderId: string }>;
  modify(orderId: string, patch: ModifyPatch): Promise<void>;
  cancel(orderId: string): Promise<void>;
}
```

### 3.2 PlaceRequest / ModifyPatch

```typescript
interface PlaceRequest {
  symbol: string;
  exchange?: string;
  side: 'BUY' | 'SELL';
  type: 'MARKET' | 'LIMIT' | 'SL' | 'SL-M';
  qty: number;
  price?: number;
  triggerPrice?: number;
  clientToken?: string;  // idempotency token
}

interface ModifyPatch {
  price?: number;
  triggerPrice?: number;
  qty?: number;
}
```

### 3.3 Order / Position (backend book row shapes)

```typescript
interface Order {
  id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  type: 'MARKET' | 'LIMIT' | 'SL' | 'SL-M';
  qty: number;
  filledQty: number;
  price: number;
  triggerPrice?: number;
  status: 'pending' | 'working' | 'partial' | 'filled' | 'cancelled' | 'rejected';
  parentId?: string;   // for OCO linking
  role?: 'entry' | 'sl' | 'tp';
}

interface Position {
  symbol: string;
  netQty: number;      // signed: +long, -short
  avgPrice: number;
}
```

### 3.4 OrderConstraints (pre-trade validation)

```typescript
interface OrderConstraints {
  tickSize: number;
  priceBand?: { lower: number; upper: number };
  freezeQty?: number;
  lotSize?: number;
  allowFractionalQty?: boolean;
}
```

---

## 4. Integration Points

### 4.1 Backend ↔ Frontend Contract

| Backend | Frontend | Mapping |
|---------|----------|---------|
| `OrderStatus.NEW/PENDING/SUBMITTED` | `'pending'` | map_order_status |
| `OrderStatus.ACK` | `'working'` | map_order_status |
| `OrderStatus.PARTIALLY_FILLED` | `'partial'` | map_order_status |
| `OrderStatus.FILLED` | `'filled'` | map_order_status |
| `OrderStatus.CANCELLED/REJECTED/UNKNOWN` | `'cancelled'/'rejected'` | map_order_status |
| `OrderType.MARKET/LIMIT/SL/SL-M` | same strings | direct pass-through |
| `BookRow.triggerPrice` | `Order.triggerPrice` | direct pass-through |

### 4.2 OrderEngine ↔ TradexTradeFeed

`TradexTradeFeed` currently has `placeOrder(o: PlaceOrder)`. After refactoring:
- `place(req: PlaceRequest)` — rename/adapt
- `modify(orderId, patch: ModifyPatch)` — already exists, adapt signature
- `cancel(orderId)` — already exists

### 4.3 TradeController ↔ TradeWsHub

`TradeWsHub` already delivers `ChartBook` snapshots via `subscribeOrders`/`subscribePositions`. `TradeController.reconcile()` consumes these directly.

---

## 5. Refactoring Steps

### Phase 1: Pure utilities (no dependencies)
1. Port `pnl.ts` — pure math
2. Port `validation.ts` — pure guards
3. Port `order-state-machine.ts` — pure state machine
4. Port `types.ts` — data model

### Phase 2: OrderEngine (depends on Phase 1)
5. Port `order-engine.ts` — wire OrderFeed interface
6. Define `OrderFeed` interface matching `TradexTradeFeed`

### Phase 3: Primitives (depends on Phase 1)
7. Port `pill.ts` — rendering helpers
8. Port `order-line.ts` — WorkingOrderLine
9. Port `position.ts` — PositionMarker
10. Port `bracket.ts` — BracketGroup

### Phase 4: TradeController (depends on Phase 3)
11. Port `trade-controller.ts` — reconcile logic

### Phase 5: Integration
12. Modify `trade.ts` — remove old mapping functions, wire peers
13. Modify `trade-feed.ts` — implement OrderFeed
14. Modify `main.ts` — wire OrderEngine + TradeController

### Phase 6: Tests
15. Add all test files from Section 2.3
16. Run full suite, verify coverage

---

## 6. Verification Strategy

| Level | Target | Method |
|-------|--------|--------|
| Unit | pnl, validation, state machine | Vitest, no mocks |
| Unit | OrderEngine | Vitest + mock OrderFeed |
| Unit | TradeController | Vitest + mock book |
| Integration | OrderEngine + TradexTradeFeed | Vitest + mock fetch |
| Integration | Full stack | Backend test suite pattern |
| E2E | Drag-modify, OCO | Playwright (future) |

---

## 7. Risks / Decisions

| Risk | Decision |
|------|----------|
| Reference uses client-side state machine, backend uses domain state machine | Keep both — they track different things (intent vs truth) |
| `TradexTradeFeed` currently uses `PlaceOrder` type from library | Adapt to `PlaceRequest` — same fields, different name |
| `map_order_status` in backend maps to chart vocabulary | Keep as-is — TradeController reads chart-vocabulary strings from backend |
| Bracket order placement currently uses `placeBracket()` | Keep — backend `/orders/bracket` endpoint handles entry+SL+TP |
| OCO linking is new to v4 | Add via OrderEngine — backend doesn't need changes |

---

## 8. Out of Scope

- DOM ladder (`dom-ladder.ts`) — depth visualization, not core trading
- `FakeBroker` — v4 has real backend
- Drag-to-modify UI wiring — primitive supports it, but chart interaction code comes later
- E2E browser tests — unit + integration tests first

---

*Spec approved 2026-08-30. Proceed to implementation planning.*
