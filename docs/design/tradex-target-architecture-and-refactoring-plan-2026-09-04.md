# TradeX — Design-Level Refactoring Plan and Target Architecture

**Date:** 2026-09-04  
**Status:** Design specification; implementation plan, not a claim that the target state is complete.  
**Audience:** Trading systems engineers, reviewers, SRE/operations, risk, and compliance.  
**Scope:** `domain/`, `trading/`, `brokers/`, `frontend/`, tests, persistence, and runtime operations.

---

## 0. Executive decision

TradeX should converge on a **hexagonal, event-driven trading core** with one authoritative execution path:

```text
                    ┌──────────────────────────────────────────────┐
                    │ Presentation                                │
                    │ Frontend: projections + user intent only    │
                    └───────────────────┬──────────────────────────┘
                                        │ versioned HTTP / WebSocket
                    ┌───────────────────▼──────────────────────────┐
                    │ Application                                  │
                    │ API handlers · session lifecycle · commands  │
                    └───────┬─────────────────┬────────────────────┘
                            │                 │
              ┌─────────────▼──────┐  ┌─────▼──────────────────────┐
              │ Strategy / Analytics│  │ Execution / Risk / OMS     │
              │ incremental studies │  │ idempotency · FSM · fills  │
              │ signals · scanners  │  │ MTM · fees · positions     │
              └─────────────┬────────┘  └──────────────┬─────────────┘
                            │                          │
                    ┌───────▼──────────────────────────▼───────────┐
                    │ Domain kernel                                │
                    │ Candle · Quote · Signal · Order · Fill        │
                    │ Position · Trade · RiskPolicy · Session       │
                    │ pure accounting · ports · domain events       │
                    └──────────────────────┬────────────────────────┘
                                           │ implemented by
                    ┌──────────────────────▼────────────────────────┐
                    │ Infrastructure                               │
                    │ Dhan · Upstox · Paper · datalake · EventLog    │
                    │ WebSockets · resilience · SQLite · metrics    │
                    └───────────────────────────────────────────────┘
```

The key design decision is not to rewrite the existing `execution/` spine. It is to make it the only spine, move durable event-log/recovery capabilities into it, and delete the competing `trading/events/` implementation after migration tests pass.

### Real-money release blockers

These are not optional cleanup items:

1. **End-to-end idempotency:** every order mutation must carry a server-enforced key. The browser currently creates `clientToken`, but `frontend/src/trade-feed.ts` drops it before `/orders`; `routes/orders.py` therefore creates no `OrderRequest.correlation_id`.
2. **Live mark-to-market:** `position_math.apply_fill()` creates positions with zero unrealized PnL, and no quote subscriber updates it. Daily-loss/drawdown checks therefore do not see open losses.
3. **Fail-closed risk dependencies:** live mode must refuse to start or refuse all opening exposure when positions, marks, account/cash, or session state cannot be verified.
4. **Replay isolation:** replay must use a replay-scoped session and clock; it must never route an order through a live session merely because the browser is showing replay data.
5. **Bracket path unification:** bracket/super orders currently call the broker directly from the route and bypass the canonical risk/idempotency/OMS path.
6. **Fill identity:** a fallback `(order_id, side, quantity, price)` cannot distinguish two genuine equal partial fills. A broker adapter must supply or synthesize a stable per-fill identity before the OMS applies it.

No live deployment is approved until each blocker has an automated test and an operational runbook.

---

## 1. What exists today

### 1.1 Current package responsibilities

| Package | Current implementation | Assessment |
|---|---|---|
| `domain/` | Frozen value objects, instruments, `Candle`, `Quote`, `Order`, `Fill`, `Position`, signals, events, broker protocol | Good kernel; needs explicit MTM metadata, risk policy, session grid, and stricter contracts |
| `trading/execution/` | `ExecutionEngine`, `RiskManager`, `FillModel`, fill sources, `PositionManager`, `position_math`, cache, optional SQLite order/idempotency persistence | Keep and harden; this is the production candidate |
| `trading/strategy/` | Reactive strategy engine, extensions, scanner | Keep; remove wall-clock fallback and ambiguous signal-to-size behavior |
| `trading/replay/` | Backtest engine, synthetic ticks, walk-forward | Extract common deterministic driver; eliminate legacy recording-only fill branch |
| `trading/runtime/` | Composition root, market feed, bar aggregator, live lifecycle, metrics | Keep composition root; add session-scoped replay and MTM wiring |
| `trading/interface/` | FastAPI routes and WebSocket stream | Keep as adapters; routes must not contain trading policy or broker calls |
| `trading/events/` | Separate actor/FSM/risk/store/projector/session implementation | Production-dead duplicate; port durable pieces then delete |
| `brokers/` | Dhan, Upstox, Paper adapters, provider clients, token/resilience/WS | Keep behind narrow ports; contract-test all adapters |
| `frontend/` | Chart, data feed, trade primitives, order intent engine, replay controls, strategy panel | Visual code is healthy; order state/PnL/validation/replay ownership must be reduced |

### 1.2 Current flow: live market data

```text
Broker WebSocket
  → provider decoder
  → MarketFeed
  → ThreadSafeReactiveBus.publish(Quote/Depth)
  → per-connection WebSocket stream
  → BarAggregator per client subscription
  → bar frames to frontend
```

This has a sound source-parity property: live quotes and synthetic replay quotes can enter the same `BarAggregator`. The weaknesses are:

- the WebSocket route owns per-connection aggregators and replay task state;
- there are multiple frontend WebSocket wrappers (`feed.ts`, `trade-feed.ts`, `main.ts`);
- there is no shared authoritative stream for quote → MTM → position state;
- bar replay in `main.ts` is a chart-side prefix-slice, not a trading session.

### 1.3 Current flow: order submission

```text
Frontend OrderEngine
  → generated clientToken (browser-only)
  → TradexTradeFeed.place()
  → POST /orders                  [clientToken currently omitted]
  → _build_order_request()        [correlation_id currently absent]
  → ExecutionEngine.submit()
  → _run_pipeline()
       → optional idempotency guard, only if cid is present
       → RiskManager
       → FillSource
       → TradingCache / PositionManager
       → OrderPlaced / OrderFilled
```

The engine's idempotency implementation is directionally correct: reservation precedes the external operation, completed keys replay the original result, and uncertain transport failures are not automatically retried. The boundary wiring is incomplete, which makes the protection ineffective for chart-originated orders.

### 1.4 Current flow: live fill

```text
Broker order stream
  → LiveFillBridge / broker decoder
  → cumulative quantity converted to delta fill
  → OrderFilled event
  → ExecutionEngine._apply_fill()
  → fingerprint dedup
  → PositionManager.on_fill()
  → OrderManager / cache
```

The design is good but has two correctness hazards:

- an absent venue `fill_id` causes an ambiguous composite fallback;
- the position is updated on fills, but not on subsequent quotes, so unrealized PnL is stale/zero.

### 1.5 Current flow: backtest and replay

```text
Backtest:
  Parquet candles
    → BacktestEngine
    → ReactiveBus
    → ReactiveStrategyEngine
    → deferred next-open command
    → ExecutionEngine + SimulatedFillSource
    → PositionManager + CashLedger + result metrics

Tick replay:
  datalake M1 candle
    → SyntheticTickGenerator
    → mini bus
    → per-WebSocket BarAggregator
    → frontend bar frames
```

Backtest uses the execution spine, but `BacktestEngine.run()` still contains a recording-only signal bridge that can fill at the current candle close, while returned signals use next-candle open. This means strategy callback style can alter execution timing. Tick replay does not create a replay trading session; it uses the current session context.

---

## 2. Target domain model

The domain must remain independent of UI, broker payloads, HTTP, RxPY implementation details, SQLite, and environment variables.

### 2.1 Canonical money and time rules

- Internal timestamps are UTC and timezone-aware.
- Datalake/broker timezone conversion happens at explicit ingestion/storage boundaries only.
- All monetary, price, fee, and quantity calculations use `Decimal`-backed value objects.
- Floats are allowed only at analytics/display boundaries, never in order acceptance, risk, fill, position, or fee accounting.
- Every event has an event timestamp and, where applicable, a causal correlation id.
- A pipeline component must use an injected `Clock`; direct `datetime.now()` in trading decisions is forbidden.

### 2.2 `Candle`

```text
Candle
  instrument: InstrumentId / Instrument reference
  timeframe: Timeframe
  timestamp: UTC instant of bucket start
  open, high, low, close: Price
  volume: Quantity
  closed: bool or separate forming/final event type
  source: market-data provenance outside the pure core, if needed
```

`Candle` validation must enforce:

- `high >= max(open, close)`;
- `low <= min(open, close)`;
- non-negative volume;
- valid, timezone-aware timestamp;
- a closed candle is immutable and cannot be replaced by a forming candle without an explicit correction event.

A single `SessionGrid`/bucketing service must define intraday and daily boundaries. Current `_bucketize()` uses UTC epoch bucketing while Indian-market storage is IST-dated; this can shift daily boundaries by 5h30m.

### 2.3 `Indicator`

The domain exposes a contract, not a UI descriptor:

```python
class IncrementalIndicator(Protocol):
    warmup: int

    def update(self, candle: Candle) -> IndicatorValue | None: ...
    def snapshot(self) -> bytes: ...

    @classmethod
    def restore(cls, state: bytes) -> "IncrementalIndicator": ...
```

Requirements:

- `update()` is O(1) or O(window), never full-history recomputation per candle;
- warmup behavior is explicit and identical in batch/live/replay;
- snapshot/restore supports deterministic seek;
- plot names/colors/placement live in registry/presentation metadata, not indicator state;
- batch `compute(series)` may exist for reports/scanners but must be proven equivalent to repeated `update()`.

### 2.4 `Signal`

Current `strength` is overloaded: the strategy engine converts `abs(strength)` into quantity. Replace the semantic ambiguity with:

```text
Signal
  signal_id: stable event id
  strategy_id: str
  strategy_version: semver
  instrument: InstrumentId
  direction: BUY | SELL | HOLD
  quantity: Quantity | None
  confidence: Decimal/normalized score
  reason: bounded audit text
  timestamp: UTC
  reference_price: Price | None
  metadata: typed strategy metadata, not UI state
```

During migration, preserve `strength` for old strategies but require an explicit adapter that maps it to quantity. New strategies must not use strength as quantity.

### 2.5 `Order` and `OrderRequest`

`OrderRequest` is intent; `Order` is the durable server-side lifecycle record.

```text
OrderRequest
  correlation_id: required for externally-originated mutations
  instrument, side, type, quantity
  limit/trigger prices
  time-in-force, product type
  reference_timestamp
  strategy / client origin

Order
  order_id
  correlation_id
  immutable request facts
  status
  filled_quantity
  average traded price
  broker reference in infrastructure mapping, not domain-specific provider fields
```

The only order state transition function is the domain FSM. A transport receipt is not an exchange acknowledgement. The domain should distinguish:

- `SUBMITTING` / `SUBMITTED` as application transport state if required;
- `NEW`, `ACK`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`, `UNKNOWN` as order state;
- `UNKNOWN` may only be produced by reconciliation/uncertain outcome, never converted silently into `REJECTED`.

### 2.6 `Fill`, `Trade`, and `Position`

`Fill` is an atomic execution fact. `Trade` is a completed round trip or position-lot accounting projection.

```text
Fill
  fill_id: required stable occurrence identity
  order_id
  instrument
  side
  quantity
  price
  timestamp

Trade
  trade_id
  instrument
  entry fills / exit fills
  quantity
  gross_pnl
  fees
  net_pnl
  opened_at / closed_at
  strategy_id / version

Position
  instrument
  signed quantity
  average entry price
  realized_pnl
  unrealized_pnl
  mark_price
  marked_at
  mark_source
  accounting currency
```

`mark_source` may be a domain enum (`LTP`, `BID`, `ASK`, `MID`, `MANUAL_RECONCILIATION`), not a broker name. For risk, use a conservative liquidation mark by policy:

- long position: bid if available, otherwise LTP;
- short position: ask if available, otherwise LTP;
- if no valid mark exists, mark is stale/unknown and live opening risk must fail closed.

A flat position may retain historical accounting state, but its current risk exposure is zero. An open position must have `marked_at` and a valid mark before it can pass live opening-risk checks.

### 2.7 `RiskPolicy` and `RiskDecision`

Replace scattered constructor flags with one immutable policy and one evaluated portfolio view:

```text
RiskPolicy
  live_orders_enabled
  max_order_value
  max_position_value
  max_orders_per_minute
  max_daily_loss
  max_drawdown
  max_open_orders
  allowed_instruments
  require_fresh_marks
  max_mark_age
  per-strategy budgets
  reject_unknown_market_value

RiskDecision
  approved: bool
  code: machine-readable enum
  reason: human-readable explanation
  observed values
  policy version
```

The mutable rate-window and daily session counters belong to a separate `RiskState` owned by the session. The pure decision function is:

```text
RiskPolicy + RiskState + PortfolioSnapshot + OrderRequest + Clock.now
  → RiskDecision + next RiskState
```

### 2.8 `TradingDay` and `TradingSession`

```text
TradingDay
  exchange
  local_date
  SessionGrid
  holiday / half-day facts

TradingSessionState
  NEW → RECOVERING → READY → HALTED → SETTLING → CLOSED
```

`HALTED` is a durable state. It is entered by kill switch, unresolved reconciliation drift, missing risk dependencies, stale marks, or a fatal pipeline error according to policy. A browser mode pill is only a projection of this state.

---

## 3. Target hexagonal architecture

### 3.1 Dependency direction

```text
frontend / HTTP / WS adapters
              ↓
application services and commands
              ↓
strategy, execution, replay orchestration
              ↓
domain types, pure policies, ports, events
              ↑
broker, datalake, SQLite, metrics adapters implement ports
```

The domain imports no `trading`, `brokers`, FastAPI, filesystem, RxPY, or frontend code.

### 3.2 Ports

Split the current fat `BrokerAdapter` into capability-specific ports:

```text
OrderPort
  submit(request, idempotency_key) → SubmissionReceipt
  modify(order_id, patch, idempotency_key) → OrderResult
  cancel(order_id, idempotency_key) → OrderResult
  get_order(order_id) → Order
  order_stream() → OrderUpdate stream

MarketDataPort
  quote(instrument) → Quote
  history(instrument, timeframe, window) → HistoricalSeries
  quote_stream() → Quote/Depth stream

PortfolioPort
  account() → AccountSnapshot
  positions() → PositionSnapshot
  orders() → OrderSnapshot

InstrumentPort
  resolve(canonical_id) → Instrument
  refresh_master() → registry snapshot
```

`DhanBroker`, `UpstoxBroker`, and `PaperBroker` compose these ports. Capability declarations must be checked against implemented methods in contract tests.

### 3.3 Application services

| Service | Owns | Must not own |
|---|---|---|
| `SessionService` | lifecycle, mode, trading-day state, halt/settle transitions | broker payload mapping |
| `OrderApplicationService` | command validation, idempotency, risk invocation, execution orchestration | chart rendering |
| `MarkToMarketService` | quote cache update, conservative mark selection, position MTM event | broker transport details |
| `StrategyRuntime` | incremental indicators, strategy callbacks, signal publication | direct broker calls |
| `ReplayDriver` | replay clock, seek/checkpoints, event source | alternate trading rules |
| `BacktestReporter` | equity curve, metrics, report serialization | order/risk semantics |
| `ReconciliationService` | broker-vs-local comparison and halt decisions | silent mutation of disputed state |
| `WireMapper` | domain ↔ REST/WS schemas | business policy |

### 3.4 Event log and projections

The active execution path should publish domain events to a durable append-only log before/with projection according to a documented transaction boundary:

```text
Command
  → idempotency reservation
  → risk decision
  → execution attempt
  → append accepted domain event(s)
  → update OMS projections
  → publish stream notification
```

A crash-recovery rule must identify whether the external broker boundary was crossed. An uncertain mutation is never replayed automatically; it enters `UNKNOWN`/`RECONCILING` and requires broker reconciliation.

Port the useful `trading/events/` `EventStore` and recovery concepts into `execution/`, then delete the duplicate stack. Do not run both stores as independent authorities.

---

## 4. Canonical trading pipeline

### 4.1 One pipeline for backtest, replay, paper, and live

```text
Source Driver
  ↓
Clock + SessionGrid normalization
  ↓
Preprocessor
  - timestamp/order validation
  - gap and correction policy
  - instrument resolution
  - closed/forming-bar policy
  ↓
Quote/Candle state update
  - cache quote/candle
  - MarkToMarketService on Quote
  - BarAggregator on Quote
  ↓
Incremental Indicator Set
  - update once per accepted closed candle
  - snapshot checkpoint
  ↓
Strategy Runtime
  - receives StrategyContext + domain event
  - emits Signal(s), never broker commands
  ↓
Signal-to-Order Translator
  - explicit quantity
  - reference timestamp/price
  - deterministic correlation id
  ↓
Risk Gate
  - current positions and marks
  - cash/margin/account facts
  - exposure, daily loss, drawdown, stale-data rules
  ↓
Order Factory + Idempotency
  - one canonical OrderRequest
  - reservation before external mutation
  ↓
Execution Port / Fill Model
  - live adapter, paper simulator, replay simulator, backtest simulator
  ↓
Order FSM + Fill Applier
  - exact fill identity
  - shared position accounting
  - fees and ledger
  ↓
Durable Events + Projections
  - orders, fills, trades, positions, PnL, audit
  ↓
REST/WS projections
```

### 4.2 Candle-by-candle event contract

For each instrument and timeframe:

1. accept a monotonic event or classify it as duplicate/correction;
2. update the forming bar without running strategy logic unless policy explicitly permits forming-bar signals;
3. on close, emit exactly one immutable `CandleClosed` event;
4. update every incremental indicator exactly once;
5. evaluate the strategy using the same context in every mode;
6. queue next-open orders with a deterministic correlation id;
7. process next bar open using the same `FillModel` policy;
8. publish fills, update position/fees/ledger, and mark to market;
9. checkpoint state for replay seek.

The source driver is the only mode-specific component. It supplies data and clock; it cannot alter risk or accounting rules.

### 4.3 Replay time travel

```text
Replay session
  → checkpoint {event_index, clock, indicators, orders, positions, risk state}
  → seek(target)
       if target >= current: consume forward
       else restore nearest checkpoint <= target
            reset projections
            replay events forward to target
  → same pipeline
```

A seek must not mutate the live account or live session. The frontend only sends `replay.seek`; the backend owns restore/replay and returns a new authoritative snapshot.

### 4.4 Mark-to-market flow (C2)

```text
Quote(instrument, ltp, bid, ask, timestamp)
  → QuoteStore.update_if_newer()
  → MarkToMarketService.mark(instrument, quote)
       choose policy mark: bid/ask/LTP
       if open position:
          unrealized = (mark - avg) * signed_quantity
          rounded = q2(unrealized)
          marked_at = quote.timestamp
       else:
          unrealized = 0
  → PositionUpdated(position)
  → cache / EventLog / WS projection
  → RiskManager reads the same Position snapshot
```

The service must be idempotent for duplicate/out-of-order quotes: an older quote cannot overwrite a newer `marked_at`. It must emit a metric and health signal when `now - marked_at > max_mark_age`.

A risk check that requires fresh marks must reject new/increasing exposure if:

- no mark exists;
- mark is older than policy;
- quote timestamp is invalid;
- bid/ask/LTP is non-positive or malformed;
- the mark source falls below the required quality for the configured policy.

Exits/reductions remain available during a loss halt, subject to broker and account safety rules.

### 4.5 Accounting invariants

For each instrument:

```text
signed_position_after = signed_position_before + signed_fill
realized_pnl changes only on closed quantity
unrealized_pnl = (risk_mark - average_entry) * signed_position
fees reduce net PnL exactly once
```

For a completed round trip:

```text
cash delta + position market value delta
  = realized PnL + unrealized PnL - fees
```

These invariants must be tested with exact `Decimal` values and property-based generated fill sequences.

---

## 5. Order lifecycle and failure flows

### 5.1 Successful order

```text
Client/strategy command
  → validate request shape
  → require non-empty idempotency key
  → reserve key atomically
  → load current portfolio + fresh marks
  → evaluate RiskDecision
  → if denied: persist rejection, release reservation, return rejection
  → create Order(NEW)
  → persist OrderPlaced intent
  → call FillSource/OrderPort
  → external result:
       acknowledged → ACK / SUBMITTED projection
       immediate fill → Fill event → PARTIALLY_FILLED/FILLED
  → record idempotency result
  → publish canonical order/fill/position projections
```

### 5.2 Duplicate request

```text
same idempotency key + same normalized request
  → return original receipt
  → no second broker call
  → no second fill
  → no second fee/position mutation
```

Same key with a materially different request must be rejected as `IDEMPOTENCY_KEY_REUSE_MISMATCH`, not treated as the original order.

### 5.3 Transport uncertainty

```text
request sent / boundary may be crossed
  → timeout, reset, 5xx, unknown response
  → do not release key
  → mark command/order AMBIGUOUS or UNKNOWN
  → reconcile broker by client tag/correlation/order book/trade book
  → only after authoritative resolution: complete or reject
```

Never automatically retry a mutation after an uncertain external outcome unless the broker contract itself guarantees idempotency using the same key.

### 5.4 Fill correction and duplicate

```text
broker update
  → adapter resolves stable fill_id
  → if already applied: no-op + duplicate metric
  → if new: apply delta exactly once
  → cumulative quantity cannot decrease silently
  → contradictory correction → reconciliation alert/halt policy
```

### 5.5 Kill switch and reconciliation

```text
risk breach / operator halt / critical drift
  → SessionState.HALTED persisted
  → stop new opening orders
  → attempt broker cancellation of working orders
  → report cancellation failures explicitly
  → retain exits/reductions according to policy
  → reconcile local/broker orders and positions
  → operator acknowledgement + explicit resume
```

A local cache transition to `CANCELLED` is not proof that the broker cancelled the order. The domain must retain external confirmation state.

---

## 6. API and WebSocket contracts

### 6.1 Versioned envelope

All external responses and events use a versioned envelope:

```json
{
  "schema": "tradex.v1",
  "event_id": "evt-...",
  "occurred_at": "2026-09-04T09:15:00.123Z",
  "correlation_id": "cid-...",
  "type": "position.updated",
  "payload": {}
}
```

Rules:

- major version changes are explicit and incompatible;
- minor additive changes remain compatible;
- unknown major versions are rejected;
- every contract has JSON fixtures and schema validation tests;
- snake_case is canonical externally; frontend mapping is centralized.

### 6.2 Order command contract

```http
POST /api/v1/orders
Idempotency-Key: cid-20260904-client-0001
Content-Type: application/json
```

```json
{
  "instrument_id": "NSE:RELIANCE",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": "10",
  "price": "2500.00",
  "trigger_price": null,
  "time_in_force": "DAY",
  "product_type": "INTRADAY",
  "client_reference": "chart-order-1"
}
```

The server must:

1. require and validate `Idempotency-Key`;
2. normalize the request and bind the key to its request hash;
3. build `OrderRequest.correlation_id` from the key;
4. pass it through the execution engine unchanged;
5. return the original response for an exact duplicate;
6. return a conflict for key reuse with a different request.

For transition, accept `client_token` in the JSON body only as a compatibility alias, but emit a deprecation metric and remove it after clients migrate to the header.

All mutations — place, modify, cancel, bracket/OCO — require idempotency keys. GET requests do not.

### 6.3 Order response

```json
{
  "schema": "tradex.v1",
  "order_id": "ord-123",
  "correlation_id": "cid-...",
  "status": "ACK",
  "execution_state": "SUBMITTED",
  "filled_quantity": "0",
  "average_price": null,
  "idempotent_replay": false,
  "reconciliation_required": false
}
```

Do not map `UNKNOWN` to `REJECTED`; that loses trading truth.

### 6.4 Position/PnL contract

```json
{
  "instrument_id": "NSE:RELIANCE",
  "quantity": "10",
  "average_price": "2500.00",
  "mark_price": "2488.50",
  "mark_source": "BID",
  "marked_at": "2026-09-04T09:20:01.000Z",
  "realized_pnl": "0.00",
  "unrealized_pnl": "-115.00",
  "fees": "0.00",
  "total_pnl": "-115.00",
  "mark_stale": false
}
```

The frontend displays these values and formats them. It does not recalculate them.

### 6.5 WebSocket topics

One authenticated multiplexed `/api/v1/stream` connection should carry:

```text
market.quote
market.depth
market.bar
order.updated
fill.created
position.updated
pnl.updated
signal.created
session.updated
replay.updated
risk.alert
error
```

Replay and live use identical market/order/fill/position schemas. Replay adds only:

```json
{
  "type": "replay.updated",
  "payload": {
    "session_id": "replay-...",
    "event_index": 120,
    "event_time": "...",
    "playing": false,
    "speed": 10
  }
}
```

The frontend must not create separate sockets for market bars, trades, and replay controls. One stream client owns authentication, reconnect, subscription restoration, backpressure, and sequence-gap detection.

---

## 7. Frontend target

### 7.1 Frontend responsibilities

Allowed:

- render candles, indicators, orders, positions, trades, and PnL projections;
- collect user intent;
- request place/modify/cancel/replay commands;
- maintain transient UI state such as hover, drag position, loading, and local layout;
- snap a value for visual feedback, while the server remains authoritative;
- render server-provided risk rejection and stale-mark warnings.

Forbidden:

- broker order state adjudication;
- client-only risk approval;
- PnL, fee, exposure, or drawdown decisions;
- trading clock decisions;
- direct broker calls;
- retrying an uncertain mutation with a new key;
- bar aggregation or indicator calculation;
- client-side OCO cancellation as the safety mechanism.

### 7.2 Refactoring sequence

1. Add a single `StreamClient` and migrate `feed.ts`, `trade-feed.ts`, and `ControlSocket` onto it.
2. Add a normalized frontend store keyed by `instrument_id`, `order_id`, `position_id`, and `event sequence`.
3. Keep `TradeController` as a renderer projection, but feed it canonical server rows.
4. Replace the browser's order FSM with a small intent tracker:
   - `BLOCKED`, `SUBMITTING`, `AMBIGUOUS`, `AWAITING_SERVER`, `SETTLED`.
5. Remove client claims that a resolved HTTP request means broker acknowledgement.
6. Keep drag throttling as UI behavior, but every mutation receives an idempotency key and server response.
7. Replace `pnl.ts` arithmetic with formatting helpers. `riskReward` may remain as a chart annotation only if it is clearly not a risk decision.
8. Rename `ReplayController` to `ChartScrubber` unless it is bound to a backend replay session. A chart scrub must never imply trading.
9. Disable trading controls when the server reports `LIVE`, `REPLAY`, `HALTED`, or `RECONCILING` states incompatible with the requested command; retain server enforcement as the real control.
10. Remove backend indicator polling (`REFRESH_MS`) after `indicator_update` stream support exists.

### 7.3 Frontend projection flow

```text
StreamClient
  → schema validation
  → sequence/gap handling
  → Store.reduce(event)
  → selectors
  → TradeController / chart primitives / panels
```

On a sequence gap, the store requests a full snapshot. It does not infer missing orders or fills.

---

## 8. Backend target

### 8.1 Composition root

`runtime.startup.boot()` remains the only production composition root:

```text
config + mode + trading_day
  → ports/adapters
  → clock
  → event log / persistence
  → quote store + MTM service
  → risk policy/state
  → execution engine
  → strategy runtime
  → session service
  → API/stream projections
```

No route, strategy, broker adapter, or frontend module may instantiate a second execution engine.

### 8.2 Execution engine decomposition

The current `ExecutionEngine` is large. Refactor internally without changing its external behavior in one step:

```text
ExecutionEngine facade
  ├─ IdempotencyService
  ├─ RiskGate
  ├─ OrderFactory
  ├─ ExecutionPort / FillSource
  ├─ OrderLifecycle / FSM
  ├─ FillApplier
  ├─ PositionAccounting
  ├─ FeeAccounting
  ├─ EventLog
  └─ ReconciliationService
```

The facade remains the only mutation entry point during migration. Components are extracted behind tests, not introduced as parallel public paths.

### 8.3 Mark-to-market service design

```text
class MarkToMarketService:
    on_quote(quote: Quote) -> tuple[PositionUpdated, ...]
```

Dependencies:

- `TradingCacheProtocol` for quote and position snapshots;
- `MarkPolicy` for bid/ask/LTP selection;
- injected `Clock` for freshness checks;
- event publisher;
- metrics registry.

Algorithm:

1. reject malformed/non-positive quote prices;
2. update quote only if timestamp is not older than cached quote;
3. read the current position for the instrument;
4. if quantity is zero, publish no exposure or a flat update;
5. select the risk mark using the policy;
6. compute `(mark - avg_price) * signed_quantity` using `Decimal`;
7. quantize using the existing `q2` accounting convention;
8. replace the frozen position with updated `unrealized_pnl`, `mark_price`, `marked_at`, `mark_source`;
9. publish `PositionUpdated` and update MTM freshness metrics.

This is the only service allowed to write `unrealized_pnl` after position creation. `PositionManager.on_fill()` preserves or resets the correct mark semantics: after a fill, the position must either retain a valid mark for the same/new quote or become explicitly stale until a new quote arrives.

### 8.4 Live startup requirements

In live mode, boot must fail closed if:

- `positions_provider` cannot be bound;
- quote/mark provider cannot be bound;
- account/cash provider is required by policy but unavailable;
- event persistence is required but cannot open;
- order stream/fill bridge is unavailable and no safe reconciliation mode exists;
- session state cannot be recovered/reconciled;
- default policy requires fresh marks but no initial marks exist for open positions.

Warnings are not sufficient for these conditions. The current best-effort live fill bridge warning is acceptable only for non-trading/read-only mode, never for an armed live order session.

### 8.5 Bracket/OCO

Represent bracket intent as a composite domain command:

```text
BracketOrderRequest
  → one idempotency key
  → one risk decision over worst-case entry/protective exposure
  → broker capability check
  → adapter submission
  → parent/child order lifecycle projection
  → server-owned OCO relation
```

The route must call the application service, never `broker.submit_super_order()` directly. If the broker cannot support the composite operation, the application must either use a tested synthetic bracket protocol or reject it; it must not silently degrade to unprotected entry trading.

---

## 9. Testing architecture

### 9.1 Test pyramid

```text
                       ┌─────────────────────────────┐
                       │ E2E / production-like        │
                       │ API + WS + paper broker     │
                       └──────────────┬──────────────┘
                  ┌───────────────────▼───────────────────┐
                  │ Contract / parity / failure injection  │
                  └───────────────────┬───────────────────┘
             ┌────────────────────────▼────────────────────────┐
             │ Unit + property-based + deterministic fixtures  │
             └─────────────────────────────────────────────────┘
```

### 9.2 Domain unit tests

- `Candle` OHLC/timezone/volume validation;
- `SessionGrid` open/close/holiday/half-day boundaries;
- bucketization: M1→M5 and M1→D1 across IST midnight/UTC boundaries;
- order FSM all legal and illegal transitions;
- `OrderRequest` normalization and required correlation semantics;
- Decimal `apply_fill()` for long, short, reduction, flip, zero, overfill;
- fee calculations, brokerage cap, tax rounding;
- MTM for long/short using bid/ask/LTP policies;
- stale mark classification and out-of-order quote rejection;
- risk decision for opening, increasing, reducing, flattening, and unknown marks.

### 9.3 Property-based tests

Add a property-testing dependency in the workspace or use the repository-approved equivalent. Generate:

- arbitrary valid fill sequences and assert position/accounting invariants;
- duplicate/reordered quote streams and assert latest valid mark wins;
- duplicate/reordered fill events and assert exactly-once application by `fill_id`;
- random prices/quantities/fees and assert no negative quantity overflow or Decimal drift;
- arbitrary replay seek sequences and assert final state equals a fresh run to the same event index;
- incremental indicator updates and batch computation produce equal values after warmup;
- idempotency retries with arbitrary transport failure points produce at most one broker mutation.

### 9.4 C2-specific tests

1. **MTM unit:** buy 10 at 100, quote bid 95 → unrealized `-50`; sell 10 at 95 → realized `-50`, unrealized zero.
2. **Short MTM:** sell 10 at 100, ask 105 → unrealized `-50`.
3. **Mark policy:** long uses bid, short uses ask; fallback to LTP only when side quote is absent.
4. **Out-of-order:** quote at `t2` then quote at `t1` leaves `marked_at=t2`.
5. **Stale mark:** mark older than policy causes opening order rejection in live mode.
6. **Missing mark:** live risk check rejects increasing exposure; reduction remains allowed.
7. **Quote stream integration:** publish quote through the actual live bus and assert cache position and emitted `PositionUpdated` change.
8. **Backtest parity:** point-in-time close mark and reactive MTM produce identical equity/PnL for the same tape.
9. **Restart:** persisted position and mark metadata recover without resetting an open loss to zero.

### 9.5 C1 end-to-end idempotency tests

- frontend unit test: `clientToken`/generated key becomes `Idempotency-Key`;
- route test: missing key returns 422/400 before engine invocation;
- route test: key is converted to `OrderRequest.correlation_id`;
- engine test: same correlation id returns the original receipt and fill source called once;
- request-hash test: same key with different order returns conflict;
- transport uncertainty test: timeout leaves key reserved/ambiguous and retry does not call broker twice;
- SQLite restart test: completed key replays original receipt after process restart;
- Playwright/API E2E: double-click and repeated POST yield one order and one position mutation.

### 9.6 Adapter contract tests

Run the same contract suite against Paper, Dhan fake transport, and Upstox fake transport:

- required order mapping and enum conversion;
- idempotency/reference propagation;
- ACK, partial, fill, reject, cancel, and modify state mapping;
- stable fill id extraction/synthesis;
- quote timestamp and bid/ask/LTP mapping;
- account/positions availability;
- capability claims match actual methods;
- provider errors map to typed domain errors;
- no retry of uncertain mutations unless explicitly supported.

### 9.7 Replay/live parity tests

Record a canonical event tape once, then run:

```text
BacktestDriver(tape)
ReplayDriver(tape)
PaperDriver(tape)
LiveDriver(fake broker replaying tape)
```

Assert equality of:

- accepted closed candles;
- indicator outputs after warmup;
- signal identity/timestamp/version;
- risk decisions;
- order requests/correlation ids;
- fill prices/quantities/timestamps;
- positions, realized/unrealized PnL, fees, equity;
- order/fill/position event sequence.

Only transport acknowledgements and replay-control events may differ. A bar-replay UI scrub is excluded from trading parity because it is not a trading driver.

### 9.8 E2E and operational tests

- paper order lifecycle through API and WS;
- partial fill ladder and duplicate updates;
- mark loss triggers daily-loss halt;
- kill switch cancels at broker or reports exact failure;
- reconnect/reconciliation with missing order;
- replay seek, checkpoint restore, and order isolation;
- stale quote health alert;
- backpressure and sequence-gap snapshot recovery;
- graceful shutdown and restart recovery;
- no secret values in logs, events, or frontend payloads.

### 9.9 CI release gates

A live-capable build cannot pass unless:

- domain and execution tests pass;
- parity goldens pass exactly;
- adapter contracts pass;
- API/WS schemas validate;
- no unapproved direct broker calls exist in routes/strategies/frontend;
- no mutation endpoint lacks idempotency tests;
- no live risk dependency silently defaults to disabled;
- coverage thresholds for position/risk/idempotency/MTM are met;
- static checks reject direct wall-clock use in deterministic pipeline modules.

---

## 10. Refactoring roadmap

Each phase is intentionally incremental. No phase should combine architecture deletion with new trading behavior without a parity gate.

### Phase 0 — Baseline and freeze

**Change**

- Freeze current event fixtures, parity outputs, API examples, and broker tapes.
- Document the active path as `execution/` + `runtime.startup.boot()`.
- Add architecture-decision records for order authority, mark policy, replay isolation, and idempotency.
- Add CI checks for frontend typecheck, package boundaries, and core test suites.

**Delete**

- Nothing.

**Do not touch**

- Fill math, fee math, broker mappings, and current order state behavior.

**Validation**

- Baseline all existing tests and golden outputs.
- Produce a dependency graph showing no production import from `trading/events/`.

### Phase 1 — Safety and C1/C2 controls

**Change**

- Require `Idempotency-Key` on all mutation endpoints.
- Forward the key from `TradexTradeFeed` and map it to `OrderRequest.correlation_id`.
- Bind `MarkToMarketService` to the canonical quote bus before strategy/order evaluation.
- Add `mark_price`, `marked_at`, `mark_source`, and stale semantics to position projections.
- Make live risk fail closed for missing positions/marks/account dependencies.
- Add C1 and C2 tests listed in §9.
- Add startup health state: `READY` only when required live dependencies are valid.

**Delete**

- No behavior deletion yet; delete only unreachable compatibility code after tests prove its callers are gone.

**Do not touch**

- Strategy signal semantics, broker payload shapes, bracket implementation, replay driver.

**Validation**

- Same idempotency key produces one broker mutation, one fill, one fee, one position update.
- Open loss is visible in `/book`, WS, risk decision, and persisted state.
- Missing/stale live mark rejects new exposure and still permits reductions.
- Paper/backtest behavior remains unchanged except where tests explicitly establish MTM.

### Phase 2 — Domain cleanup and accounting consolidation

**Change**

- Introduce `RiskPolicy`, `RiskDecision`, `TradingDay`, `SessionGrid`, and `Trade`.
- Extract MTM calculation into shared Decimal accounting module.
- Make `fill_id` required at adapter boundary; synthesize a monotonic adapter-local id only where venue has no native id.
- Replace `Signal.strength → quantity` with explicit signal quantity through a compatibility translator.
- Extract `TradeAssembler` from fills/position lifecycle.
- Make `reference_timestamp` mandatory for deterministic simulation paths.
- Remove recording-only current-close backtest bridge; migrate strategies to returned signals.

**Delete**

- Legacy signal bridge.
- Unused/fallback indicator context globals.
- Wall-clock scanner fallback.
- Duplicate order-construction helpers after `OrderFactory` is proven.

**Do not touch**

- Frontend wire shapes until versioned contracts are ready.
- Broker adapter internals except fill-id contract changes.

**Validation**

- Backtest and reactive execution produce identical fills/PnL for canonical tapes.
- Incremental/batch indicator tests pass.
- Fill duplication and partial-fill tests pass under generated sequences.
- No accounting code uses binary floats.

### Phase 3 — Architecture realignment and session drivers

**Change**

- Port durable `EventStore` and recovery from `trading/events/` into `execution/`.
- Add `SessionDriver` interface with Live, Paper, Backtest, and Replay implementations.
- Create replay-scoped session, replay clock, checkpoints, and backend seek.
- Split `BrokerAdapter` into capability ports.
- Route bracket/OCO through the application execution service.
- Make `TradingSessionState` durable and include `HALTED`, `RECONCILING`, `SETTLING`.

**Delete**

- Entire production-dead `trading/events/` package and its tests only after porting and migration validation.
- Direct broker calls from route modules.
- Frontend-as-trading replay path.

**Do not touch**

- Shared fill model, position math, fee formulas, existing broker transport retry rules.

**Validation**

- One session driver pipeline passes the same parity suite in all four modes.
- Replay orders never appear in live cache/account.
- Bracket orders have risk, idempotency, audit, and lifecycle coverage.
- Recovery tests demonstrate restart without duplicate submission.

### Phase 4 — Frontend/backend separation

**Change**

- Introduce one multiplexed stream client and normalized store.
- Reduce order engine to intent tracking and UI throttling.
- Consume server-computed PnL/marks/statuses.
- Move all order/status mapping to versioned wire mappers.
- Replace chart bar replay with explicit `ChartScrubber` or backend replay session.
- Generate control state from server session state.

**Delete**

- Browser broker-truth FSM.
- Browser PnL/risk computations.
- Redundant WebSocket wrappers.
- Indicator polling shim.
- Client OCO cancellation as a safety mechanism.

**Do not touch**

- Chart rendering primitives, profile/transform drawing, layout/persistence, visual interactions unrelated to trading authority.

**Validation**

- Frontend has no route/broker imports and no strategy/risk formulas.
- Playwright validates server-authoritative order lifecycle and replay isolation.
- WS reconnect and snapshot recovery are deterministic.

### Phase 5 — Performance, observability, and production operations

**Change**

- Partition event bus by criticality: order path, market data, diagnostics.
- Move slow scanner/analytics consumers off the order path.
- Add sequence numbers and durable event-log correlation.
- Metrics: mark age, stale-mark rejections, risk rejects by code, idempotency replays/conflicts, fill latency, uncertain submissions, reconciliation drift, queue drops, event-log lag.
- Add deterministic incident replay bundle: config hash, strategy version, tape, event log, checkpoints, broker responses with secrets removed.
- Add deployment runbooks for startup reconciliation, kill switch, stale marks, broker outage, and recovery.

**Delete**

- In-memory event deque as the primary audit source.
- Unbounded per-session maps/trackers without retention policy.
- Debug/probe scripts that exercise deleted architecture.

**Do not touch**

- Money/accounting semantics unless a new approved accounting decision is recorded and all goldens are intentionally regenerated.

**Validation**

- Load tests meet p99 order-path and quote-to-MTM budgets.
- Incident replay reproduces position/PnL/order state exactly.
- Kill-switch and broker-outage drills are signed off by operations/risk.
- Secrets and PII scanning pass.

---

## 11. Before vs after

| Concern | Current | Target |
|---|---|---|
| OMS authority | Active `execution/` plus production-dead `events/` | One execution facade plus durable event log |
| Idempotency | Browser token not forwarded; backend guard skipped without cid | Required server key, request hash, durable replay/conflict semantics |
| MTM | Fill-time unrealized zero; no live quote writer | One quote-driven MTM service with freshness and conservative mark policy |
| Risk | Mutable manager with optional providers and silent disablement | Pure policy/decision plus explicit state; fail-closed live dependencies |
| Order FSM | Domain, duplicate events FSM, browser lifecycle | Domain/execution authority plus minimal frontend intent state |
| Fill identity | Ambiguous composite fallback without venue id | Required stable occurrence identity and contradiction handling |
| Replay | UI bar scrub; tick replay shares live session context | Backend replay session, replay clock, checkpointed seek, isolated account |
| Fill timing | Next-open plus recording-only current-close branch | One declared simulation timing policy per driver |
| Brackets | Route can call broker directly | Composite application command through risk/idempotency/OMS |
| PnL | Backend partial, backtest MTM, browser floats | Server Decimal accounting and frontend projection |
| WebSockets | Multiple frontend sockets and per-route state | One authenticated multiplexed stream/store |
| Time | Mixed UTC/IST/naive conventions | UTC core plus explicit SessionGrid boundaries |
| Persistence | Optional order/idempotency persistence, competing event stack | One durable event log, projections, and recovery protocol |
| Testing | Strong existing parity/contract assets but gaps around C1/C2/replay | Release-gated correctness, property, failure, parity, and operational tests |

---

## 12. Non-negotiable engineering rules

1. **No mutation without an idempotency key.** The key is required, server-owned, durable, and bound to a normalized request hash.
2. **No live order path without fresh risk inputs.** Missing marks, positions, account, reconciliation, or persistence fail closed.
3. **Only the backend owns order truth.** The frontend may represent intent and uncertainty, never broker state.
4. **Only one service writes unrealized PnL.** Every open position has mark metadata and freshness semantics.
5. **Every fill has an occurrence identity.** Never deduplicate a real-money fill using an inherently ambiguous composite key.
6. **All modes use one pipeline.** Mode changes data source, clock, and fill adapter — not strategy, risk, accounting, or FSM rules.
7. **Replay is a separate session.** No replay UI can alter or submit into a live account.
8. **No direct broker calls from routes or strategies.** All mutations pass through application service → risk → execution port → OMS.
9. **No floating-point money decisions.** Decimal at domain/execution boundaries; float only for non-authoritative presentation/analytics.
10. **No direct wall clock in deterministic code.** Use an injected clock and event timestamps.
11. **No silent fallback for trading safety.** A warning is not a risk control; unavailable safety dependencies produce a rejection or halt.
12. **No second implementation of accounting, risk, order state, bucketing, or wire mapping.** Delete the duplicate instead.
13. **Every state transition is auditable.** Persist event id, sequence, event time, processing time, correlation id, source, and schema version.
14. **Unknown is not rejected.** An unresolved external outcome enters reconciliation; it is not guessed away.
15. **Parity is a release gate.** Any intentional difference between backtest, replay, paper, and live must be named, documented, and tested.
16. **Strategies are versioned artifacts.** Strategy id/version/parameters are recorded with every signal and order.
17. **Schema changes are versioned.** Additive changes are compatible within a major; breaking changes require migration and fixtures.
18. **Kill switch behavior is tested against the broker.** Local cancellation state is not proof of venue cancellation.
19. **Operational drills are part of correctness.** Restart, broker outage, uncertain submission, stale marks, and reconciliation drift must be practiced.
20. **Prefer deletion.** If a path is not authoritative, tested, and used, remove it rather than preserving another compatibility universe.

---

## 13. Decision log to create before implementation

Before Phase 1 code begins, record explicit decisions for:

- required idempotency-key transport (`Idempotency-Key` header, compatibility window, retention period);
- risk mark policy for each asset class and missing bid/ask behavior;
- maximum acceptable mark age per timeframe and live mode;
- treatment of a position when quote feed is stale;
- event-log transaction boundary relative to the broker mutation;
- replay isolation and whether replay can submit paper-only orders;
- bracket fallback policy when provider lacks native support;
- fill-id synthesis contract for each broker;
- EOD policy: cancel, carry, or explicit operator approval;
- regulatory/audit retention, clock synchronization, and immutable log storage.

These decisions must be approved by engineering, risk, and operations before the implementation phases are marked complete.
