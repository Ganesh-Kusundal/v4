# TradeX v4 deep refactor plan

**Date:** 2026-09-24  
**Status:** Proposed  
**Design:** `docs/target-architecture-design.md`  
**Execution rule:** Complete one phase at a time. Keep the old path available until the new path passes its exit gate.

## Phase 0, baseline and safety fence

**Goal:** Make the current system measurable and reproducible before changing ownership.

**Work:** Record the current dirty tree without resetting it. Map paper, backtest, replay, and live flows. Record exact Python, frontend, Playwright, parity, lint, type, and build commands. Add architecture import checks. Add metrics for feed health, queue drops, order outcomes, risk decisions, and reconciliation drift. Keep the current loopback live bind and development authentication policy explicit.

**Tests:** Baseline smoke tests for paper and replay. Existing execution, position math, interface, and browser tests. Add a negative test proving live mode fails closed when readiness is not authoritative.

**Exit gate:** Known green baseline or a documented list of pre existing failures. Every later phase has a configuration rollback switch.

**Rollback:** No runtime behavior change.

## Phase 1, feed health and recovery

**Goal:** Make feed readiness mean data readiness, not only socket readiness.

**Work:** Add `FeedHealth`, `ConnectionGeneration`, `EventPosition`, `RecoveryResult`, and `FeedSupervisor`. Move stale detection out of optional polling into a session owned monitor. Add state transition events. Add authoritative history recovery after the last accepted closed bar. Add current bucket seeding rules. Add duplicate, out of order, and gap counters. Keep `MarketFeed` as the broker adapter until the new supervisor is proven.

**Tests:** Provider disconnect, reconnect, stale timeout, recovery success, recovery failure, current bucket present, current bucket absent, duplicate tick, out of order tick, large timestamp gap, session close, and replay isolation.

**Exit gate:** `READY` is impossible with an unresolved gap or stale mark. Risk blocks new live exposure while feed health is not ready. Metrics expose generation, age, recovery duration, gaps, and drops.

**Rollback:** `feed_supervisor_enabled=false` routes the existing WebSocket bridge to `MarketFeed` and `BarAggregator`.

## Phase 2, aggregation ownership and event metadata

**Goal:** Give bar construction one owner and make every frame auditable.

**Work:** Add `BarAggregationService` keyed by instrument, timeframe, source, and run id. Serialize updates per key. Add event id, feed id, connection generation, event timestamp, receive timestamp, sequence number, and quality flags. Close buckets on timer and boundary. Dispose state on unsubscribe. Preserve current chart wire fields.

**Tests:** Boundary close, timer close, IST session behavior, seeded current bar, duplicate quote, out of order quote, replay versus live parity, unsubscribe cleanup, and slow consumer backpressure.

**Exit gate:** Existing chart payloads remain compatible. No replay frame can enter a live aggregator.

**Rollback:** Keep the current per connection `BarAggregator` behind the legacy adapter.

## Phase 3, typed command and event envelopes

**Goal:** Make trading state transitions versioned and auditable.

**Work:** Introduce command and event envelopes around `PlaceOrderCommand`, modify, cancel, risk decisions, broker acknowledgement, fill, rejection, cancellation, and unknown outcome. Include command id, correlation id, strategy id and version, policy version, code revision, and timestamps. Keep `ExecutionEngine` as the initial handler.

**Tests:** Existing parity suite must pass without changed numeric outputs. Add envelope round trips, duplicate command behavior, unknown outcome, partial fill, cancel race, and event ordering tests.

**Exit gate:** Every order transition has a stable event identity and reason code. Backtest, replay, paper, and live still share `apply_fill` and `CashLedger`.

**Rollback:** Translate new envelopes to current domain events at the engine boundary.

## Phase 4, OMS repository and recovery

**Goal:** Make local state rebuildable and broker reconciliation explicit.

**Work:** Add `OrderRepository` and `EventStore` ports. Mirror current SQLite state into a versioned projection. Persist commands, risk decisions, broker requests, acknowledgements, fills, cancel attempts, unknown outcomes, and reconciliation results. Rebuild projections on startup before accepting orders. Compare broker open orders and positions before enabling live trading.

**Tests:** Crash after command, crash after broker request, crash after partial fill, duplicate event replay, unknown submission, broker drift, recovery to degraded, and recovery to ready.

**Exit gate:** A process can restart, rebuild local state, identify drift, and fail closed until drift is resolved. No direct SQLite access remains in API routes.

**Rollback:** Use the existing SQLite store as the read source while the new projection is shadow written.

## Phase 5, risk and command application layer

**Goal:** Remove policy from transports and make risk decisions observable.

**Work:** Add application use cases for submit, modify, cancel, mark, and reconcile. Move replay mode, feed freshness, product, quantity, price, exposure, margin, daily loss, drawdown, rate, and capability checks behind the `RiskPort`. Add stable reason codes and policy version to every decision.

**Tests:** Risk decision table, stale mark rejection, replay rejection, unknown mark rejection, per strategy budget, account cap, daily loss, drawdown, rate limit, and broker capability failure.

**Exit gate:** API, WebSocket, strategy, and CLI paths produce the same typed risk decisions for the same command.

**Rollback:** Existing `ExecutionEngine` remains the command processor while new use cases call it.



## Phase 6, strategy artifacts and lifecycle

**Goal:** Make strategies reproducible and safely resettable.

**Work:** Add `StrategyArtifact`, `StrategyRegistry`, `StrategyRuntime`, reset, snapshot, restore, and deterministic seed. Keep current extension strategies behind adapters. Make strategy parameters, feature dependencies, universe constraints, risk budget, and approval state explicit. Strategies emit signals only.

**Tests:** Reset between runs, snapshot restore, deterministic replay, versioned parameters, feature dependency validation, strategy stop behavior, and mode parity.

**Exit gate:** The same artifact and event stream produce identical signals and P&L in all modes.

**Rollback:** Registry configuration selects the current auto discovered strategy instances.

## Phase 7, research boundary and experiment manifests

**Goal:** Separate fast research from faithful execution simulation.

**Work:** Move data lake, feature, universe, experiment, and evaluation ownership into research modules. Add dataset manifest, universe snapshot, feature as of semantics, experiment manifest, walk forward splits, purged splits, cost breakdown, and capacity report. Research emits versioned strategy artifacts. The current backtest remains the shared execution simulator.

**Tests:** No lookahead feature tests, point in time universe tests, snapshot hash tests, deterministic seed tests, cost sensitivity, parameter perturbation, walk forward, and artifact approval.

**Exit gate:** A result can be reproduced from manifests alone. Research cannot import API, broker SDK, SQLite, or live execution internals.

**Rollback:** Research writes artifacts to existing parquet and strategy paths while runtime reads through the legacy registry.

## Phase 8, replay isolation

**Goal:** Make replay a safe deterministic event source.

**Work:** Move replay run state, cursor, speed, target bar, generation, guard, and cleanup behind `ReplayRun`. Ensure replay frames are scoped by run id and source. Keep live feed, chart snapshot, live anchors, and live readiness outside replay ownership. Make all terminal cleanup paths idempotent.

**Tests:** Start, pause, resume, speed, step, seek, stop, failure, cancellation, repeated terminal commands, live restore, order suppression, and multi client ownership.

**Exit gate:** No replay state changes live OMS or live feed state. Cleanup always restores the prior live snapshot and clears the order guard.

**Rollback:** Use the existing replay route and guard until the new run owner passes all terminal path tests.

## Phase 9, API and frontend ownership

**Goal:** Keep transport and UI modules free of trading policy.

**Work:** Split frontend into app, feed, chart, orders, account, replay, workspace, and shared owners. Add typed API and WebSocket protocol clients. Move route logic behind application queries and commands. Add authenticated production identity, CSRF protection, authenticated WebSockets, and audit identity in the security workstream.

**Tests:** Playwright startup, readiness, feed recovery, stale order blocking, replay isolation, account updates, workspace conflict, error recovery, and accessible critical actions. API contract tests for every route and WebSocket message.

**Exit gate:** The UI can recover from disconnect and stale data without enabling trading. Routes contain transport mapping, not risk or persistence policy.

**Rollback:** Serve the previous frontend bundle and retain compatibility routes.

## Phase 10, observability, deployment, and extraction

**Goal:** Operate the system and extract packages only when boundaries are real.

**Work:** Add dashboards and alerts for feed age, gaps, drops, resync duration, order latency, rejection reasons, unknown outcomes, slippage, exposure, drawdown, and reconciliation. Add runbooks. Add deployment readiness and liveness checks. Extract deep modules into separate packages only after import checks and independent tests pass.

**Tests:** Deployment smoke, readiness failure, liveness survival, stale feed alert, broker outage drill, recovery drill, and package contract tests.

**Exit gate:** Operators can detect, diagnose, halt, recover, and audit a live session. Each extracted package has a public interface, contract tests, and rollback release.

**Rollback:** Revert the package distribution change while keeping internal module boundaries and compatibility adapters.

## Required work in every phase

1. Add or update tests before behavior changes.
2. Keep the old path available.
3. Add metrics before optimization.
4. Record migration version and rollback switch.
5. Run domain, broker, execution, parity, interface, frontend, and browser gates relevant to the phase.
6. Do not reset, stash, or overwrite the current uncommitted remediation work.
7. Do not promote a strategy or enable live trading from a partially verified phase.

## Final release checklist

- `uv sync --frozen` succeeds.
- Ruff passes.
- Domain and broker Mypy pass.
- Trading Mypy follows the approved decreasing error budget.
- Python unit, property, contract, integration, parity, and recovery tests pass.
- Frontend typecheck and build pass.
- Playwright safety and recovery tests pass.
- Paper, backtest, replay, and live mode matrices pass.
- Live non loopback binding follows the production authentication policy.
- Feed recovery restores data before readiness.
- Unknown broker outcomes cannot be blindly retried.
- OMS rebuild and broker reconciliation pass.
- Strategy and research artifacts identify their data, code, policy, and parameters.
- Rollback procedure is documented and tested.

