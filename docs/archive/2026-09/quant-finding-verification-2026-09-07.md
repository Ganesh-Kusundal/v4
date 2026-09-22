# Quant-Finding Verification — 2026-09-07

**Review lens:** principal quant engineer
**Repository state checked:** branch `s0-phase1-followon`, shared working tree
**Validation:** source tracing plus focused test inventory; the pre-existing suite was reported green at **2885 passed, 2 skipped**.

## Executive verdict

The prior remediation plan was directionally good but not accurate enough to execute unchanged. It treated three items as open in the wrong way:

1. fill identity is **partially** hardened, not fully absent;
2. frontend idempotency forwarding is already fixed and tested, while submit hash binding remains open;
3. the duplicate `events/` package is already deleted in the working tree, while current event-log integration/recovery remains incomplete.

The safest revised order is:

1. close the **submit/cancel request-hash binding**;
2. close the **provider-neutral live fill identity contract**;
3. perform a **narrow deterministic-clock audit** with explicit mode semantics;
4. audit and wire the existing durability primitives, or explicitly defer recovery — do not re-port deleted code speculatively.

## Finding-by-finding evidence

### F1 — live fill identity: CONFIRMED, PARTIALLY MITIGATED

**Evidence:**

- `trading/src/tradex_trading/sdk/live_fill_bridge.py` contains `TradeBookFillIdResolver`.
- Dhan exposes `DhanBroker.trade_book()` and its trade rows use `orderId`/`tradeId`.
- Upstox exposes `UpstoxBroker.get_trade_book()`, but with a different method name and provider row shape (`trade_id` is present in the existing client tests).
- `trading/src/tradex_trading/runtime/startup.py` only probes `getattr(broker, "trade_book", None)`, so the resolver is constructed for Dhan but not Upstox.
- When the resolver cannot fetch a row or cannot find a trade id, it returns `None`; `LiveFillBridge` then creates a fill with no `fill_id`, and `ExecutionEngine` uses `(order_id, side, quantity, price)`.
- Existing tests intentionally prove both behaviors: distinct IDs solve equal-lot partials, while no resolver under-counts them.

**Accuracy:** the ambiguity is real and remains a live-money risk. The prior plan was wrong to imply that simply adding a resolver closes it. It does not close the outage/lag case or the Upstox wiring gap.

**Correct action:** define one adapter/bridge contract that yields a non-empty fill identity for every live fill. Use venue trade IDs where available; otherwise generate an adapter-local monotonic identity tied to the broker order and observed fill occurrence. A live identity failure must be observable and fail closed or halt/reconcile — never silently downgrade to ambiguous dedup.

**Important design constraint:** the bridge currently receives cumulative updates and emits deltas. A generated identity must identify the *delta occurrence* and remain stable across a republished cumulative update. A naïve counter called on every update is unsafe unless the bridge records the cumulative boundary/identity mapping.

### F2 — deterministic time: PARTIALLY CONFIRMED, SEVERITY REDUCED

**Evidence:**

- `ReactiveStrategyEngine` stamps `OrderRequest.reference_timestamp` from candle/quote events.
- `BacktestEngine` passes timestamps into the recording-only bridge and uses `FakeClock` for its simulated timeline.
- `FillModel.fill_timestamp()` still falls back to `datetime.now(UTC)` when no reference timestamp is supplied.
- `LiveFillBridge` intentionally timestamps real live fills with wall clock; this is correct for live event receipt time, although a broker execution timestamp would be better when available.
- `RiskManager.check()` still uses wall clock when `now` is omitted, including `_fresh_marks_available`, rate limiting, and session-day baselines.
- `ScannerEngine` has a wall-clock fallback for an omitted history window.
- Domain default factories for `Fill`, `Quote`, and events are boundary conveniences; they are not by themselves proof of a replay bug.

**Accuracy:** the broad statement “the pipeline is nondeterministic” is too strong. Deterministic paths are substantially timestamped already. The precise finding is that the APIs allow callers to omit the timestamp and silently fall back to wall clock, so determinism is not enforced at the boundary.

**Correct action:** first classify every hit as (a) market/event boundary, (b) live receipt clock, (c) user convenience default, or (d) deterministic pipeline logic. Require an explicit timestamp/clock only for category (d), and add tests for backtest/replay. Do not blanket-replace all `datetime.now(UTC)` calls.

### F3 — idempotency: FRONTEND CLOSED; SUBMIT HASH BINDING OPEN

**Evidence:**

- `frontend/src/trade-feed.ts` forwards `clientToken` as `Idempotency-Key` for `placeOrder()` and `place()`.
- `frontend/src/trade/integration.test.ts` asserts that forwarding and also asserts a generated key when the caller supplies none.
- `routes/orders.py` requires the header and passes it as `OrderRequest.correlation_id`.
- `ExecutionEngine.modify()` binds `_request_fingerprint(request)` when reserving its key.
- `ExecutionEngine._run_pipeline()` calls `check_and_reserve(cid)` without `_request_fingerprint(request)` for normal submit, affecting both synchronous and reactive submit paths.
- `ExecutionEngine.cancel()` also reserves its own key without a request fingerprint.
- Therefore duplicate same-key replay works for submit, but same-key/different-payload conflict protection is not active for normal submit/cancel mutations. The guard implementation supports the protection; the call sites do not consistently use it.

**Accuracy:** the previous plan’s “frontend + E2E idempotency proof open” is stale. The highest-value implementation is a small call-site correction plus regression tests, not a frontend forwarding change.

**Correct action:** bind a canonical operation-specific fingerprint for every mutation. The fingerprint must include the operation and target order id, not only mutable request fields; otherwise a cancel/modify key could collide across different targets or operations. Preserve replay semantics and map mismatch to 409.

### F4 — duplicate events stack / durability: STALE DESCRIPTION, REAL DURABILITY GAP

**Evidence:**

- `trading/src/tradex_trading/events/` is absent from the current filesystem and appears as deleted in `git status`.
- `trading/tests/events/` is also deleted in the working tree.
- `trading/scripts/probe_review_fixes.py` remains, but it is now an AST boundary diagnostic and does not import or execute the deleted stack.
- `trading/src/tradex_trading/reactive/event_log.py` already defines `SQLEventLog`.
- `trading/src/tradex_trading/reactive/thread_safe_bus.py` can attach an event log when explicitly constructed with `event_log=...`.
- `runtime/startup.py` does not pass `event_log` when constructing its bus and instead wires `SQLiteIdempotencyGuard` plus `SQLiteOrderStore` when persistence is configured.
- No current `execution/recovery.py` or equivalent recovery-projector flow was found.

**Accuracy:** “port the event log then delete `trading/events/`” is stale for this working tree. The valid question is whether the existing `SQLEventLog` is sufficient and whether it is wired to a real recovery path. It currently serializes payloads best-effort and is not, by itself, a domain-event recovery mechanism.

**Correct action:** freeze deletion as already performed in the working tree; audit the existing event log’s schema, event-type fidelity, atomicity, lifecycle wiring, and recovery semantics. Either wire a tested recovery path or explicitly document SQLite order-store + broker reconciliation as the current recovery contract. Do not invent a second OMS or port code that no longer exists in the checkout.

## Revised priority

| Priority | Work | Why |
|---|---|---|
| P0 | Live fill identity must not silently downgrade | Silent lost fills corrupt position/P&L |
| P0 | Request-hash binding for submit/cancel | Same key with altered payload can bypass conflict protection |
| P1 | Deterministic timestamp enforcement in deterministic modes | Reproducibility and parity evidence |
| P1 | Durability/recovery contract audit | Current order/idempotency persistence is not equivalent to event recovery |
| P2 | Replay-scoped session/driver | Safety semantics, but route gate is already an interim defense |
| P2 | Transition-table cleanup and frontend intent simplification | Valuable hygiene, not the first money-risk fix |

## Plan corrections required

- Remove “frontend forwarding open” from the plan; mark it verified closed.
- Add `request_hash` to the normal submit and cancel reservation paths.
- Make the fingerprint operation/target-specific and test collisions across operation types.
- Replace “always synthesize an ID” with “identity provider contract plus stable cumulative-delta mapping”; a generated counter must be stable across republished updates.
- Add Upstox trade-book integration with its actual method and row keys.
- Replace the Phase-B port/delete sequence with “audit and wire current `SQLEventLog` or explicitly defer recovery”; do not re-port deleted `trading/events/` code.
- Keep the clock work narrow: enforce timestamps in deterministic modes, retain live receipt wall clock, and justify boundary defaults individually.

## Non-negotiable acceptance gates

1. Two genuine equal-size, equal-price partial fills from each live provider both update the position; a republished cumulative update does not.
2. A live fill with no stable identity is rejected/halts/reconciles according to an explicit policy; it never silently uses the ambiguous engine fallback in live mode.
3. Same idempotency key + same operation/target/payload replays; same key + changed payload/target/operation returns 409 and makes no venue call.
4. Backtest/replay results are invariant to wall-clock time when given the same data and configuration.
5. Recovery behavior is documented and tested from the actual persisted artifacts; no claim of event-sourced recovery is made until domain-event reconstruction works.

*This verification supersedes the stale status statements in the 2026-09-07 remediation drafts.*
