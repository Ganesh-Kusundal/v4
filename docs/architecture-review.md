# TradeX v4 architecture and testing review

**Date:** 2026-09-24  
**Scope:** Current working tree on `chore/cleanup-overhaul`  
**Review type:** Architecture, separation of concerns, and quantitative system quality

## Executive conclusion

TradeX v4 has a sound foundation for an algorithmic trading platform. The project is not a collection of broker scripts. It has a real domain model, a broker boundary, a composition root, a reactive execution spine, a shared accounting model, risk controls, persistence, backtesting, replay, an HTTP interface, and a browser application.

The highest quality decision in the current code is the shared execution model. Backtest, paper, replay, and live flows are intended to publish the same commands and consume the same execution, position, fee, and risk components. That is the right direction for a quant system. It reduces the risk that research results describe an engine that production does not use.

The next improvement is not a rewrite. It is to make the boundaries more explicit, remove weak typing at the application edges, and establish a research quality layer around the existing trading engine. The main risks are concentrated in the broad `tradex_trading` package, permissive object interfaces, tolerated static type errors, incomplete data identity rules, and the absence of an explicit research registry and experiment manifest.

## 1. What the project does today

### Package structure

The repository currently uses three Python packages:

```text
domain      shared market, execution, value, event, protocol, and capability model
brokers     provider adapters, authentication, transport, resilience, and streams
trading     runtime composition, execution engine, OMS, strategy, backtest, replay, data lake, API, SDK
```

The dependency direction is good:

```text
domain <- brokers <- trading
```

`domain` has no internal package dependency. `brokers` depends on `domain`. `trading` depends on both. The README and package manifests agree on this direction. This is the correct shape for broker agnosticism and future research adapters.

### Runtime control flow

The runtime composition root is `trading/src/tradex_trading/runtime/startup.py`. It selects the broker, creates the reactive bus, chooses a fill source, configures persistence and risk, creates the session, and fails closed when boot fails. This is a good separation between assembly and behavior.

The normal order flow is intended to be:

```text
strategy or API
    -> PlaceOrderCommand
    -> reactive bus
    -> idempotency
    -> risk
    -> fill source
    -> order manager
    -> position manager
    -> fee, cache, persistence, and events
```

The order pipeline is in `trading/src/tradex_trading/execution/engine.py`. The engine combines orchestration with a few stateful concerns such as brokerage accounting, fill deduplication, correlation tracking, reconciliation, and metrics. It is safe to retain during the current remediation, but it should be split when a new execution capability is added.

The strategy engine is in `trading/src/tradex_trading/strategy/core/engine.py`. It converts strategy output into commands and supports a `next_open` fill reference. The backtest engine in `trading/src/tradex_trading/replay/backtest.py` drives the same reactive pipeline. The parity tests and shared position math are important because they test this intended equivalence.

### Interface and frontend

FastAPI is an adapter over the session. The frontend is a TypeScript and Vite application with Playwright end to end tests. The current working tree contains substantial frontend and interface changes, including replay ownership, feed recovery, account actions, and startup behavior. These changes are not included in the architectural rewrite recommendation. They should be reviewed as a separate delivery stream.

## 2. Strengths that should be preserved

### 2.1 Shared domain model

The use of frozen domain objects, Decimal money and quantity values, explicit instruments, typed events, and protocols gives the system strong semantic boundaries. This prevents broker response dictionaries and HTTP request dictionaries from becoming an accidental global model.

### 2.2 Broker adapters and capability failure

Dhan, Upstox, and Paper are behind a broker adapter boundary. Capability defaults are closed, which is safer than assuming that every venue supports the same order types, products, quotes, or streaming behavior.

### 2.3 Fail closed live boot and risk gates

The project contains protections that are often missing in trading systems:

- a live writer lock;
- explicit live confirmation;
- a kill switch;
- daily loss and drawdown limits;
- per strategy risk budgets;
- fresh mark requirements for live exposure checks;
- idempotency guards;
- uncertain submission handling;
- reconciliation;
- persistence support.

These are meaningful safety properties. They should become versioned runtime contracts with dedicated scenario tests, not just implementation details.

### 2.4 One accounting model

`tradex_domain.position_math.apply_fill` is the shared pure accounting model. The position manager delegates to it, and the backtest and reactive paths use the same fill semantics. This is a strong separation between accounting policy and runtime orchestration.

### 2.5 Test and CI breadth

The repository currently has approximately 198 trading test files, 36 broker test files, 15 domain test files, and a small root test suite. The CI design includes:

- Ruff;
- Mypy for domain and brokers;
- full Python tests with coverage;
- parity and contract gates;
- a shared position math gate;
- frontend typecheck and build;
- Playwright browser tests;
- pinned chart dependency handling.

The breadth is good. The test command could not be completed in the current shell because the local environment did not expose the project tools, so this review does not claim a fresh green test result.

## 3. Separation of concerns findings

### 3.1 The trading package is still a monolith

`tradex_trading` contains application services, execution, OMS, risk, strategy runtime, backtest, replay, data lake, analytics, API, runtime boot, metrics, and SDK orchestration. The internal directories help, but package dependency direction alone does not enforce the intended layer boundaries.

Recommended direction:

```text
trading_domain       pure policies and state transitions
trading_application  use cases and commands
trading_execution    OMS, risk, fill, reconciliation, persistence
trading_strategy     strategy contracts and runtime adapters
trading_research     data, features, experiments, metrics, walk forward
trading_interfaces   FastAPI, WebSocket, CLI, frontend protocol models
trading_runtime      composition and process lifecycle
```

Introduce explicit internal ports before moving files. Prevent research modules from importing FastAPI, broker SDKs, or SQLite. Prevent API routes from importing research code.

### 3.2 Too many object typed seams

`TradingSession` accepts multiple `object | None` dependencies, including scanner, strategy, stream, backtest, fill bridge, market feed, scheduler, mark to market, metrics, and writer lock. `startup.py` also uses `Any` for important injected services.

This is an architectural observability problem. Replace broad seams incrementally with small Protocols such as `StrategyRuntime`, `ScannerRuntime`, `StreamRuntime`, `BacktestProvider`, `FillBridge`, `MarketFeed`, `Clock`, and `MetricsSink`. Each protocol should expose only the operations required by its consumer.

### 3.3 The strategy interface is informal and stateful

The reference strategies implement `on_bar`, `on_quote`, `on_depth`, `on_fill`, and `on_event`, but the strategy state contract is not as strongly typed as the domain model. The SMA examples convert Decimal close values to Python float and keep internal history in the strategy object.

For live strategies this is workable, but a quant platform needs an explicit contract for reset, snapshot, typed event input, signal timestamp, horizon, expiry, feature dependencies, deterministic random seed, and no direct broker or database access.

Use Decimal or fixed precision where decisions affect money. Float may be allowed inside vectorized research features, but conversion and rounding policy must be explicit.

### 3.4 Backtest and research are not yet separate concepts

The backtest engine is strong for execution parity, but it is also being used as the research engine. A production quant system needs two paths:

```text
research pipeline
    raw data -> validated dataset -> features -> experiment -> candidate strategy

execution simulation
    approved strategy + market events -> same execution policy as live
```

Research must run many parameter sets quickly. Simulation must be faithful to fills, fees, slippage, liquidity, latency, and market sessions. Do not optimize research by weakening simulation. Define a formal promotion process from research result to a versioned strategy artifact.

### 3.5 Persistence is present, but durability is not yet complete

SQLite order persistence and idempotency support are valuable, but the current store is a snapshot style persistence seam. A solid trading system needs explicit event and state durability for accepted commands, broker requests, acknowledgements, fills, partial fills, cancellation and modification attempts, reconciliation, positions, cash, risk decisions, strategy decisions, and process and data versions.

The system should rebuild local state after a crash and then reconcile against broker truth. The current design has the right ingredients, but the event schema and recovery contract need to be specified before increasing persistence scope.

### 3.6 Authentication remains a safety workstream

The README documents API key protection for write routes while read endpoints are public. The existing remediation document correctly treats the page injection model as development only. A live platform should eventually add user identity, authorization, short lived sessions or signed tokens, CSRF protection for browser mutations, authenticated WebSockets, audit identity on every command, secret rotation, revocation, and a non loopback deployment policy.

## 4. Quant system requirements

### 4.1 Point in time data

Every market data row needs a stable identity and provenance. Recommended dimensions include `instrument_id`, `venue`, `asset_class`, `timeframe`, `event_timestamp`, `receive_timestamp`, `session_date`, `sequence_number`, `source`, `ingestion_version`, and `quality_flags`.

Features must be calculated using only data available at the decision timestamp. Extend the existing gap classification and data quality work into a general dataset manifest and point in time feature contract.

### 4.2 Instrument identity

The project guide records the problem of one trading symbol referring to multiple securities. A production system must not use the symbol alone as an identity. Use a canonical instrument identity plus venue identifiers: `canonical_instrument_id`, `venue`, `exchange_segment`, `provider_symbol`, `provider_security_id`, `series`, `expiry`, `strike`, and `option_type`.

Persist mapping history. A symbol mapping must never silently overwrite a historical meaning.

### 4.3 Survivorship and universe bias

Universe membership must be point in time. Store effective from and effective to dates, source, and revision history. Backtests should support delisted and inactive instruments when data is available. The backtest manifest should capture exact membership used for each run.

### 4.4 Corporate actions and market calendars

Corporate actions need event identity and application timestamp, not only an ex date. Extend the current support with tests for splits, dividends, bonuses, symbol changes, and contract adjustments. The data contract should state whether prices are raw or adjusted and where adjustment happens.

### 4.5 Costs and fill realism

A credible result models brokerage, statutory fees, exchange charges, stamp duty and GST where applicable, spread, slippage by liquidity bucket, market impact, partial fills, queue position where available, latency, rejected submissions, circuit limits, session restrictions, square off rules, and product rules.

Every simulation should write a cost breakdown, not only a total return.

### 4.6 Evaluation and statistical validity

The project has walk forward and grid search scripts. A mature quant layer should record train, validation, and test periods; purged and embargoed splits where labels overlap; parameter count and search budget; random seeds; universe and data snapshot hashes; feature and strategy versions; code revision; result artifact; benchmark and factor comparison; turnover; capacity; exposure; concentration; maximum drawdown; recovery time; hit rate; profit factor; tail loss; and sensitivity to fees, delay, missing bars, and parameter perturbation.

A result containing only total return, Sharpe, and drawdown is a smoke test, not strategy approval.

### 4.7 Live safety and observability

Every order should have a trace from signal to decision to command to broker request to fill. Correlation IDs, event types, metrics, and the kill switch are good foundations. Add strategy and dataset version on every signal, immutable decision reason codes, risk snapshots, broker request and response correlation, clock skew and feed latency metrics, slippage and fill rate by strategy and instrument, exposure and drawdown alerts, reconciliation drift alerts, audit export, and explicit degraded and halted states.

## 5. Recommended target architecture

```text
                    +----------------------+
                    | research and data     |
                    | datasets, features,  |
                    | experiments, reports  |
                    +----------+-----------+
                               |
                      versioned strategy artifact
                               |
                    +----------v-----------+
                    | application runtime  |
                    | commands, sessions,  |
                    | use cases, scheduler  |
                    +-----+-----------+---+
                          |           |
              +-----------v--+     +--v-----------+
              | market data  |     | execution    |
              | live/history |     | OMS, risk,   |
              | validation   |     | fills, recon |
              +-----------+--+     +------+-------+
                          |                  |
                    +-----v------------------v-----+
                    | broker and venue adapters   |
                    +----------------------------+

```

The application runtime depends on ports. Research produces versioned artifacts. Execution consumes artifacts and cannot import research or HTTP code. Broker adapters know provider details. The API translates transport requests into application commands and never owns trading policy.


## 6. Test strategy

Use a pyramid with explicit contracts at every boundary.

### Unit tests

Test pure behavior without I/O: value objects and instrument identity; position math; cash ledger; fee calculation; risk decisions; idempotency fingerprints; feature calculations; corporate action adjustment; strategy state reset and snapshot; serialization; and event versioning.

### Property tests

The repository already declares Hypothesis. Use it for high risk invariants:

- no duplicate fill is applied twice;
- cash and positions remain consistent after arbitrary fill sequences;
- risk limits are never exceeded after valid state transitions;
- order idempotency is stable for identical requests;
- serialization round trips preserve domain meaning;
- backtest and live execution produce identical state for the same event stream;
- no future event can influence a feature value.

### Contract tests

Keep separate suites for each broker adapter, the paper broker and adapter contract, fill source, market data provider, clock, persistence, HTTP and WebSocket schemas, and broker capability behavior.

### Integration tests

Cover signal to fill to position to cash; partial fills; reject and retry; unknown submission followed by reconciliation; live feed disconnect and recovery; process restart and state recovery; API write authorization; replay and live state isolation; and single writer enforcement.

### Simulation and research tests

These are separate from ordinary unit tests. Each approved research run should be replayable and have a small golden dataset. Add event replay, deterministic seed, no lookahead, universe membership, market calendar, corporate action, cost sensitivity, walk forward split, parameter perturbation, benchmark, and factor comparison tests.

### Browser and operational tests

Playwright should test behavior, not only rendering. Cover order controls disabled when readiness is false, replay unable to mutate live order state, reconnect resynchronization, stale and provisional bars, typed account errors, production API and WebSocket authentication, and keyboard and screen reader workflows for critical actions.

## 7. Prioritized roadmap

### Priority 0, release safety

1. Complete the existing remediation plan without resetting the working tree.
2. Make live non loopback binding fail closed until production authentication exists.
3. Add crash recovery and reconciliation tests for persistence.
4. Turn the tolerated trading Mypy baseline into a tracked, decreasing error budget. Do not call a system type safe while CI continues on error.
5. Record exact test, lint, build, and browser commands in the release checklist.

### Priority 1, boundary hardening

1. Replace `Any` and `object | None` application seams with narrow Protocols.
2. Define a versioned event envelope and command envelope.
3. Split research and execution simulation packages without changing shared execution behavior.
4. Add dependency checks that fail if domain imports broker or trading code, or if research imports API or broker SDK code.
5. Make strategy lifecycle explicit with reset, snapshot, version, and deterministic seed support.

### Priority 2, quant research foundation

1. Define a dataset manifest and immutable snapshot hash.
2. Define point in time instrument and universe identity.
3. Add feature computation with as of semantics and no lookahead tests.
4. Add an experiment manifest capturing data, features, strategy, parameters, costs, and code revision.
5. Add walk forward, purged split, sensitivity, and capacity reports.
6. Promote a strategy only through a versioned approval artifact.

### Priority 3, production operations

1. Add process safe state ownership for multi process operation.
2. Add authentication and authorization identities to commands and audit events.
3. Add alerting for feed health, risk, execution quality, and reconciliation.
4. Add disaster recovery and data replay drills.
5. Add runbooks for stale feeds, unknown submissions, risk halts, and broker outages.

## 8. Acceptance criteria for the next architecture slice

The next slice should be a measurable boundary improvement, not a large file move. It is complete when:

- `TradingSession` and startup inject narrow typed Protocols;
- domain, broker, research, execution, and interface dependency checks run in CI;
- every command and event has a version and correlation identity;
- a strategy can reset and reproduce the same signals from the same snapshot;
- point in time feature tests prove no future data is used;
- a backtest writes a run manifest and cost breakdown;
- live and simulation event streams pass the same execution parity fixture;
- Mypy on trading is blocking, or an approved error budget is shrinking and enforced;
- a crash recovery test restores OMS state and reconciles broker truth;
- the release checklist proves frontend build, Playwright, Python tests, parity tests, and live safety checks.


## 9. Live feed and live data review

The live feed path is currently:

```text
broker WebSocket
    -> broker adapter reconnect layer
    -> MarketFeed
    -> reactive bus
    -> FastAPI WebSocket bridge
    -> per connection BarAggregator
    -> frontend BarSocket
    -> chart and strategy consumers
```

This separation is directionally correct. `MarketFeed` owns broker stream subscriptions. `FeedRegistry` shares subscriptions across browser clients. The FastAPI route owns browser connection state and bounded queues. `BarAggregator` owns the conversion from quotes to bars. The frontend owns reconnect and resubscription from the browser point of view.

### Existing strengths

- The broker adapter owns automatic reconnect and live subscription replay. This is the correct layer for provider connection recovery.
- `MarketFeed` uses stable callback objects, avoiding duplicate handler registration when subscriptions are added repeatedly.
- The feed uses an `RLock` around shared instrument and depth state.
- `FeedRegistry` reference counts instruments across multiple WebSocket clients.
- Each browser connection has bounded tick and control queues. Slow consumers cannot create unlimited server memory growth.
- The WebSocket authenticates before accepting the connection when an API key is configured.
- The frontend uses one WebSocket with reference counted subscriptions and reconnects with jittered exponential backoff.
- New live bar subscriptions can carry the last closed bar and current forming bucket as seeds.
- The first live frame after a history seed is marked provisional when the current bucket was not supplied.
- Replay frames carry `source`, `run_id`, and separate provisional state.
- Order submission is guarded while replay is active.

### Live feed risks and required improvements

#### 1. Stale feed detection is poll driven

`MarketFeed.check_stale()` exists and publishes `StaleFeed`, but its own documentation says it must be called periodically. A stale feed must not depend on an optional caller remembering to poll it.

Recommended design:

- create one feed health monitor owned by the session;
- run it at a bounded interval;
- transition each instrument through explicit states such as `starting`, `live`, `stale`, and `halted`;
- publish state transitions, not only repeated stale events;
- clear stale state only after a valid tick and successful history reconciliation;
- feed health into the risk gate;
- block new live orders when the execution mark is stale or unavailable;
- expose feed health through readiness, WebSocket status, metrics, and audit events.

A stale feed must affect both chart readiness and trading readiness. A chart that is merely old is a display problem. A stale execution mark is a risk problem.

#### 2. Reconnect is not the same as data recovery

The backend reconnect layer can reopen the broker socket and replay subscriptions, but that does not prove that ticks were not lost while disconnected. After reconnect, the system needs a recovery window:

1. mark the feed degraded immediately;
2. record the last accepted event timestamp and sequence;
3. reconnect and resubscribe;
4. fetch authoritative history from the last accepted closed bar;
5. merge and validate recovered bars;
6. resume live aggregation only after reconciliation;
7. emit a recovery complete event with the repaired gap.

The frontend already has `onResync` support. The backend contract should make that callback meaningful by guaranteeing that a resync request has a corresponding history and state recovery result.

#### 3. Feed health should be explicit in the client state machine

The UI should not infer readiness only from whether a WebSocket is open. Use an explicit state machine:

```text
DISCONNECTED -> CONNECTING -> AUTHENTICATING -> SUBSCRIBING
    -> RESYNCHRONIZING -> READY -> DEGRADED -> RECONNECTING -> HALTED
```

A socket can be open while data is stale or the current bar is unseeded. Those states must not be presented as `READY`.

The browser should display connection state, last accepted tick time, last closed bar time, current bar provisional state, recovery progress, dropped frames, and whether trading is blocked.

#### 4. Bar aggregation needs one owner

`BarAggregator` correctly handles bucket boundaries, IST bucketing, forming versus closed bars, source tagging, and current bucket seeding. It is not thread safe by itself and relies on the caller to serialize access. That is acceptable, but the contract should be enforced by one `BarAggregationService` that owns aggregators by key:

```text
(instrument_id, timeframe, source, run_id) -> BarAggregator
```

That service should serialize updates, reject stale timestamps, detect out of order events, close buckets on a timer as well as on the next tick, and dispose state on unsubscribe. A large timestamp jump should trigger recovery rather than silently creating a gap.



#### 5. Sequence and duplication metadata

The public frame does not currently expose a broker event sequence or source event identity. Add metadata such as `feed_id`, `connection_generation`, `event_id`, `sequence_number`, `event_timestamp`, `receive_timestamp`, `source`, and `quality_flags`.

This makes duplicate detection, gap detection, and audit possible. It also allows the backend to distinguish a repeated quote from a genuinely new quote with the same price and volume.

#### 6. Frontend reconnect tests are too shallow

`frontend/e2e/feed-recovery.spec.ts` currently checks that a feed status matching `ready`, `stale`, or `reconnecting` is visible. That proves rendering, not recovery.

The browser suite should simulate WebSocket close, reconnect with subscription replay, delayed and failed history responses, current bucket seed present and missing, stale feed disabling order controls, replay blocking order controls, instrument and timeframe changes during reconnect, shared browser subscriptions, queue overflow telemetry, and deliberate unsubscribe without reconnect.

#### 7. Replay and live feeds need separate state ownership

The current `ReplayGuard` correctly blocks order mutations while replay is active, and replay frames carry a run identifier. The feed state should also be explicitly partitioned:

```text
live feed owner
replay feed owner
chart series owner
active run owner
```

A replay run must not mutate the live aggregator, live anchors, live feed health, or live order readiness. Terminal replay cleanup must be idempotent and must restore the previous live snapshot and feed state.

### Recommended live feed target

```text
Broker adapter
    owns provider socket, authentication, reconnect, provider subscription replay

FeedSupervisor
    owns connection generation, health state, stale detection, recovery windows

MarketFeed
    owns provider quote and depth subscriptions, emits typed tick events

FeedRegistry
    owns browser reference counts and capability limits

BarAggregationService
    owns aggregators, ordering, bucket close, seeding, and recovery

WebSocket gateway
    owns per client queues, authentication, protocol translation, and backpressure

Frontend FeedController
    owns explicit client state, retry, resync, stale display, and trading readiness
```

The important principle is that reconnect is only transport recovery. Trading readiness requires data recovery. A system is not ready merely because a WebSocket is open.

## 10. Live feed acceptance criteria

The live feed is ready for production trading only when all of these are true:

- a broker disconnect changes readiness to degraded or reconnecting;
- a reconnect fetches and validates history after the last accepted closed bar;
- missing or incomplete current bucket data remains provisional;
- a stale feed cannot authorize a new live order;
- a recovered feed emits a recovery complete event with gap information;
- duplicate and out of order ticks are counted and handled deterministically;
- browser subscriptions are restored after reconnect;
- client side history requests are cancellable and bounded;
- WebSocket queues cannot grow without bound;
- replay cannot mutate live feed state or live readiness;
- replay cleanup is idempotent;
- browser tests prove all of these transitions;
- operational metrics expose connection generation, last tick age, resync duration, gaps, drops, and recovery failures.

## 11. Final assessment

The project has moved beyond a prototype and contains the right ingredients for a serious trading system. The most valuable existing choice is the shared execution and accounting path. Preserve it.

The most important improvement is to separate research truth from runtime orchestration while keeping both connected through versioned contracts. The second most important improvement is to turn currently tolerated uncertainty, especially broad object seams and non blocking type checking, into explicit enforced boundaries.

The recommended sequence is incremental. First harden contracts and safety. Then add the research quality layer. Then split packages only where dependency tests prove the split creates value. This preserves the current working tree and gives the team a path toward a solid, extensible, and auditable quant platform.


