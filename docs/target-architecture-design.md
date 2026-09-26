# TradeX v4 target architecture and refactor design

**Date:** 2026-09-24  
**Status:** Proposed  
**Migration rule:** Preserve the current shared execution and accounting path behind stable interfaces.

## 1. Purpose and principles

This is the target architecture for live trading, research, replay, interfaces, and operations. It uses a strangler migration. New modules are introduced beside current modules and callers move one vertical slice at a time. Every slice has tests, metrics, a compatibility adapter, and a rollback switch.

Principles: one execution policy; transport does not own policy; research artifacts are versioned; reconnect requires data recovery; live money fails closed; state is immutable and versioned; ports are narrow; one owner exists for each stateful concern; research is deterministic; tests follow interfaces.

## 2. Compatibility core

Preserve `tradex_domain`, `tradex_brokers`, `ExecutionEngine`, `apply_fill`, `CashLedger`, `BarAggregator`, `TradingSession`, `ReactiveBus`, and the current parity, contract, interface, and Playwright suites. No new module may create a second position or cash model. A replacement is valid only when it delegates to current pure functions or passes identical contract and parity tests.

```text
strategy or API -> PlaceOrderCommand -> reactive bus -> idempotency
    -> risk -> fill source -> order manager -> position manager
    -> cash, fees, cache, persistence, and events
```

## 3. Target organization

```text
domain/       market, instruments, execution, risk, events, protocols, identity
brokers/      base, dhan, upstox, paper, transport
trading/      application, execution, market_data, strategy, research, replay
              interfaces, runtime, persistence, observability
operations/   deploy, runbooks, dashboards
```

Dependency rules: domain has no internal imports; brokers depend on domain ports; execution does not import FastAPI, frontend, pandas, research internals, or broker SDKs; research does not import HTTP or live execution internals; interfaces contain transport mapping only; runtime is the only assembly layer; operations observes but does not change decisions.

## 4. Core ports

```python
class MarketDataPort(Protocol):
    def subscribe(self, request: MarketDataSubscription) -> Subscription: ...
    def history(self, request: HistoryRequest) -> HistorySnapshot: ...
    def health(self, instrument: InstrumentId) -> FeedHealth: ...

class ExecutionCommandPort(Protocol):
    def submit(self, command: PlaceOrderCommand) -> CommandReceipt: ...
    def modify(self, command: ModifyOrderCommand) -> CommandReceipt: ...
    def cancel(self, command: CancelOrderCommand) -> CommandReceipt: ...

class RiskPort(Protocol):
    def evaluate(self, request: RiskRequest) -> RiskDecision: ...
    def on_market_state(self, state: MarketState) -> None: ...
    def on_position(self, position: Position) -> None: ...

class StrategyArtifactStore(Protocol):
    def publish(self, artifact: StrategyArtifact) -> ArtifactReceipt: ...
    def load(self, artifact_id: ArtifactId) -> StrategyArtifact: ...
```

Ports return typed values, not provider dictionaries, and expose only what the consumer needs.

## 5. Live feed design

Ownership:

```text
BrokerAdapter: provider socket, auth, reconnect, provider resubscribe
FeedSupervisor: generation, health, stale detection, recovery windows
MarketFeed: quote and depth subscriptions, typed ticks
FeedRegistry: reference counts and capability limits
BarAggregationService: one serialized aggregator per key
WebSocketGateway: connection queues, auth, protocol, backpressure
FeedController: browser state, reconnect, resync, readiness display
```

State machine:

```text
NEW -> CONNECTING -> AUTHENTICATED -> SUBSCRIBING -> RESYNCHRONIZING
    -> READY -> DEGRADED -> RECONNECTING -> HALTED -> CLOSED
```

READY requires valid transport, acknowledged subscriptions, history from the last accepted closed bar, seeded or explicitly provisional current bucket, fresh marks, and no unresolved active-strategy gap.

Recovery: record generation and last event; degrade and block new exposure; reconnect and resubscribe; fetch and validate history; merge bars; seed current bucket only from authoritative data; resume aggregation; emit `FeedRecovered`; enter READY only after consumers acknowledge. Failure enters HALTED, never silent READY.



## 6. Execution, risk, OMS, and persistence

Order lifecycle:

```text
INTENT -> VALIDATED -> RISK_APPROVED or RISK_REJECTED
       -> IDEMPOTENCY_RESERVED -> BROKER_REQUESTED -> ACKNOWLEDGED
       -> PARTIALLY_FILLED -> FILLED -> CANCELLED or REJECTED or UNKNOWN
```

Risk order: identity, mode and replay guard, feed freshness, tradability, product, price, quantity, notional, exposure, margin and cash, daily loss, drawdown, rate limit, duplicate command, and broker capability. Rejections use stable reason codes. Unknown broker outcomes are reconciled before retry; blind money-changing retries are forbidden.

Add `OrderRepository` and `EventStore` ports. The event store is authoritative for recovery and the order table is a projection. Startup order is writer ownership, event replay, projection rebuild, broker read, reconciliation, degraded or ready decision, then order admission.

## 7. Strategy, research, and replay

A strategy is a versioned artifact containing identity, version, parameters, feature dependencies, universe constraints, risk budget, execution policy reference, and approval state. Runtime lifecycle is `CREATED -> STARTING -> RUNNING -> PAUSED -> STOPPING -> STOPPED`. Strategies provide reset, snapshot, restore, and deterministic sequencing, and emit signals only.

```text
raw data -> validated snapshot -> point in time features -> experiment
         -> candidate artifact -> shared execution simulation
```

A research manifest records data hash, universe hash, feature version, labels, splits, parameters, search budget, seed, code revision, costs, metrics, and result artifact. The current backtest remains the execution simulator and uses the same risk, fill, position, cash, and fee semantics as paper and live.

Replay is a deterministic event source. `ReplayRun` owns dataset, cursor, speed, target bar, run id, generation, simulated output, and order suppression. Replay cannot write live feed state, live OMS, or live readiness. Terminal cleanup is idempotent and restores the live snapshot.

## 8. API, frontend, and operations

FastAPI and WebSocket routes are transport adapters. They map typed application commands and queries to messages. They do not own risk, persistence, feed recovery, or strategy policy.

Frontend ownership is split into `app`, `feed`, `chart`, `orders`, `account`, `replay`, `workspace`, and `shared`. Feed readiness, chart series, replay ownership, order arming, account projection, and workspace revision each have one owner.

A live process is ready only with explicit mode and broker identity, valid credentials, healthy feed supervisor, restored OMS projection, reconciliation result, loaded risk policy, audit sink, metrics and health endpoints, and single writer ownership. Readiness fails when a required condition fails. Liveness fails only when progress is impossible. Stale data is degraded or halted, not a process crash.

Required metrics: feed age, generation, gaps, drops, resync duration, order latency, rejection reasons, unknown outcomes, slippage, exposure, drawdown, and reconciliation drift.

## 9. Migration phases

1. Baseline and safety fence.
2. Feed health and recovery.
3. Aggregation ownership and event metadata.
4. Typed execution and risk contracts.
5. OMS repository and recovery.
6. Strategy artifacts and lifecycle.
7. Research boundary and manifests.
8. Replay isolation.
9. API and frontend ownership.
10. Operations, deployment, and package extraction.

Detailed tasks, tests, exit gates, and rollback controls are in `docs/deep-refactor-plan.md`.

## 10. Definition of done

One feed supervisor owns live health; reconnect always performs data recovery; stale or unknown marks cannot open exposure; one execution policy and one accounting model serve all modes; OMS rebuild and reconciliation work; strategy artifacts and research manifests are reproducible; API and frontend contain no trading policy; package boundaries are enforced in CI; readiness and recovery states are visible and tested; every phase has a tested rollback path.
