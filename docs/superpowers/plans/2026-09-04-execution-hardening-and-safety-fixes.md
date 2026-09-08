# Execution Hardening & Safety Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (Recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-09-04
**Source findings:** `docs/reviews/architecture-flow-verification-2026-09-04.md` (N1–N8) — an independent source-verified review of the working tree on branch `refactor/execution-state`.
**Companion docs:** `docs/design/tradex-target-architecture-and-refactoring-plan-2026-09-04.md` (long-term target), `docs/reviews/principal-architecture-review-2026-09-04.md`.

**Goal:** Land the review's findings as small, tested, behavior-preserving fixes in the **active `execution/` spine** — the real-money safety defects first (live cancel never reaches the venue, idempotency gaps on modify/cancel, kill-switch cancel ordering), then the P2 hardening items, then the known cleanups. No architecture rewrite; the target-architecture phases (delete `trading/events/`, session drivers, wire envelope, frontend separation) remain gated follow-on work referenced in §6.

**Scope guardrails (non-negotiable):**
- **Do NOT modify or import `trading/events/`.** It is the production-dead duplicate stack slated for deletion in a later phase. All fixes live in `trading/src/tradex_trading/execution/`, `interface/routes/`, `sdk/`, and `frontend/src/`.
- **Do NOT change money math, fee formulas, fill-model timing, or broker payload mappings.** If a task seems to require it, stop and record a design note instead.
- **Parity is a release gate:** every task's tests must pass alongside the existing parity goldens (`trading/tests/parity/`).

## Global Constraints

- **TDD for every task** — failing test first, then implementation
- **No mocking of broker boundaries where a fake transport already exists** — reuse `BrokerFillSource` + adapter seams; tests use real engine/cache/guard
- **Fail-closed** — venue-failure on cancel/modify must never leave the OMS claiming a state the venue does not confirm
- **Venue-first for mutations with external side effects** — OMS projection follows venue acknowledgement (mirrors the existing bracket path)
- **Idempotency keys are server-owned** — reserved before external mutation, replayed for duplicates, released on rejection
- **Decimal only for money** — no floats in any new accounting/risk code
- **UTC-aware timestamps** in all new code; injected `Clock`/`now` where determinism matters
- **One execution spine** — no new mutation entry points; routes/strategies call the engine facade
- **Commit per task** — small, self-contained commits with the repo's `feat/fix/test` message style

---

## Phase 0: Baseline Freeze

### Task 0: Baseline verification

**Files:** none (verification only)

**Steps:**

- [ ] **Step 1:** Record green baseline. Run:
  ```bash
  .venv/bin/python -m pytest trading/tests/execution trading/tests/interface -q
  .venv/bin/python -m pytest trading/tests/runtime trading/tests/parity trading/tests/sdk -q
  .venv/bin/python -m pytest domain/tests -q
  .venv/bin/python -m pytest brokers/tests/common/test_broker_contracts.py -q
  ```
  Expected: all green (WS-resilience collection errors from missing optional extras are pre-existing and out of scope).

- [ ] **Step 2:** `git status` — confirm the working tree is the uncommitted `refactor/execution-state` state documented in the deep-dive review. Note any files touched by other agents before starting (shared checkout).

- [ ] **Step 3:** Record the baseline in `.superpowers/sdd/progress.md` (new ledger section for this plan; do not overwrite the blocked prior plan's history).

---

## Phase 1: Real-money safety (P0/P1)

### Task 1: Live cancel must reach the venue (N1 — P0)

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`ExecutionEngine.cancel`)
- Modify: `trading/tests/execution/test_cancel_releases_idempotency.py` or new `trading/tests/execution/test_live_cancel_reaches_venue.py`

**Interfaces:**
- Consumes: `FillSource.cancel(order_id)` (already exists on `BrokerFillSource` → `broker.cancel_order`)
- Produces: `engine.cancel()` that dispatches the venue first for **plain orders**, exactly as it already does for brackets via `cancel_super_order`

**Contract (tests must assert):**
1. `engine.cancel(oid)` on a **plain** order with a `BrokerFillSource` whose broker records calls: `broker.cancel_order` invoked **exactly once**, BEFORE the OMS cache shows CANCELLED.
2. Venue cancel raising `OrderRejectedError`: OMS order **stays ACK** (not CANCELLED), `OrderCancelled` is **not** published, error propagates to the caller.
3. Bracket orders still dispatch `cancel_super_order` exactly once (existing tests must stay green — regression guard).
4. Paper/simulated sources (no venue): behavior unchanged — OMS CANCELLED, `OrderCancelled` published, cid released (existing `test_cancel_releases_idempotency.py` guards this).
5. The route `DELETE /orders/{id}` (orders.py `cancel_order`) surfaces the venue failure as a typed 4xx/5xx (not a silent "cancelled").

**Steps:**

- [ ] **Step 1:** Write the failing tests (contract 1–5 above) against `ExecutionEngine` with a recording fake broker behind `BrokerFillSource`.
- [ ] **Step 2:** Run `pytest trading/tests/execution/test_live_cancel_reaches_venue.py -v` — expected FAIL (venue never called; OMS flips first).
- [ ] **Step 3:** Implement: in `engine.cancel`, for non-bracket orders with a callable `self._fill.cancel`, call it (venue-first) before `transition_to(CANCELLED)`; on exception, leave OMS untouched and re-raise. Keep bracket path unchanged.
- [ ] **Step 4:** Run the new tests + `test_cancel_releases_idempotency.py` + `test_bracket_engine_pipeline.py` — expected PASS.
- [ ] **Step 5:** Commit: `fix(execution): cancel plain orders at the venue before flipping OMS state`.

### Task 2: Kill-switch cancel ordering (N4 — P1)

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`trip_kill_switch`)
- Modify: `trading/tests/execution/test_engine_killswitch_dedup.py` or new `trading/tests/execution/test_kill_switch_venue_first.py`

**Interfaces:**
- Consumes: `engine.cancel` (Task 1, now venue-first for plain orders too)
- Produces: `trip_kill_switch()` that returns per-order failures with the OMS left in the pre-cancel state for every order whose venue cancel failed

**Contract (tests must assert):**
1. For each open order, the venue cancel is attempted before the OMS transitions; a failing venue cancel for order X leaves X's OMS status unchanged (ACK), while other orders still cancel.
2. The returned `failures` list contains X's order_id; the kill switch remains tripped (new submissions rejected).
3. Brackets are still cancelled exactly once via `cancel_super_order` (no double venue call — existing dedup behavior).

**Steps:**

- [ ] **Step 1:** Write failing tests: recording broker that fails cancel for one specific order; assert its OMS status is preserved and it appears in `failures`.
- [ ] **Step 2:** Run — expected FAIL (current code flips OMS first, then reports).
- [ ] **Step 3:** Implement: iterate open orders, call `engine.cancel` per order (venue-first from Task 1), catch per-order exceptions into `failures` **without** transitioning that order's OMS state. Remove the now-redundant `self._fill.cancel` double-call for plain orders (Task 1 made `engine.cancel` do it).
- [ ] **Step 4:** Run kill-switch + cancel suites — expected PASS.
- [ ] **Step 5:** Commit: `fix(execution): kill switch cancels venue-first and preserves OMS state on failure`.

### Task 3: Modify/cancel idempotency keys — reserve, replay, conflict (N2 — P1)

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`modify`, `cancel`)
- Modify: `trading/tests/execution/test_modify_risk.py`, `trading/tests/execution/test_cancel_releases_idempotency.py` (extend)

**Interfaces:**
- Consumes: existing `IdempotencyGuard` (`check_and_reserve` / `record_result` / `release`) and `request.correlation_id`
- Produces: modify/cancel with the same reservation semantics as submit

**Design decisions (already made — implement as specified):**
- `engine.modify(oid, request)`: if `request.correlation_id` is present and a guard is bound — reserve before the risk re-check; a completed key replays the **original modified Order** (return it, no venue call); a reserved key raises the deterministic in-flight error (see Task 4); on success `record_result(cid, modified_order)`; on risk/venue failure `release(cid)`.
- `engine.cancel(oid, correlation_id=None)`: accept an optional cid (route passes it); reserve → venue-first cancel (Task 1) → OMS transition → `record_result`; on failure `release`.
- **Request-hash binding:** extend `SQLiteIdempotencyGuard`/`MemoryIdempotencyGuard` with an optional `request_hash` recorded at reserve time; replay returns `IdempotencyDuplicate` only when the hash matches, else raises `IdempotencyKeyReuseMismatch` (map to 409 in the route). Backward compatible: `check_and_reserve(cid)` without hash keeps current behavior.

**Contract (tests must assert):**
1. Duplicate modify (same cid, same payload): venue modify called once, second call replays the first modified Order, no second venue call.
2. Same cid with a **different** payload: rejected with `IdempotencyKeyReuseMismatch` (route → 409), venue untouched.
3. Duplicate cancel (same cid): venue cancel called once; second returns the cancelled Order.
4. Risk-denied modify releases the cid (immediately reusable).
5. SQLite guard: completed modify/cancel keys replay across restart.

**Steps:**

- [ ] **Step 1:** Write failing tests (contract 1–5).
- [ ] **Step 2:** Run — expected FAIL (guard never consulted on modify/cancel).
- [ ] **Step 3:** Implement guard integration in `engine.modify`/`engine.cancel` + hash binding in both guards; route `PUT/DELETE /orders/{id}` passes `correlation_id` into `engine.modify`/`engine.cancel`.
- [ ] **Step 4:** Run modify/cancel/idempotency/sqlite suites + parity goldens — expected PASS.
- [ ] **Step 5:** Commit: `feat(execution): idempotency reservation and request-hash binding for modify and cancel`.

---

## Phase 2: Hardening (P2)

### Task 4: Deterministic in-flight duplicate response (N5 — P2)

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`MemoryIdempotencyGuard`, `_run_pipeline`)
- Modify: `trading/src/tradex_trading/execution/sqlite_store.py` (`SQLiteIdempotencyGuard`)
- Modify: `trading/src/tradex_trading/interface/routes/orders.py`
- New: `trading/tests/execution/test_inflight_duplicate.py`

**Contract (tests must assert):**
1. A second submit with a reserved-but-incomplete cid returns a deterministic `OrderReceipt(status=…, message="idempotency_in_flight")` — no `RuntimeError`, no 500.
2. The route maps it to `409 Conflict` with `detail="idempotency key is being processed"`.
3. The in-flight reservation is **not** released (the original request owns it); once the original completes, the same cid replays the original result.
4. Concurrent submits (two threads, same cid) produce exactly one venue call.

**Steps:**

- [ ] **Step 1:** Write failing tests (contract 1–4, thread-based for 4).
- [ ] **Step 2:** Run — expected FAIL (RuntimeError → 500).
- [ ] **Step 3:** Implement: guards return a typed `IdempotencyInflight` marker instead of raising; `_run_pipeline` converts it to the receipt; route catches it → 409.
- [ ] **Step 4:** Run execution + interface suites — expected PASS.
- [ ] **Step 5:** Commit: `fix(execution): return deterministic 409 for in-flight idempotency duplicates`.

### Task 5: Replay-mode order gate (N3 — P2, pragmatic interim)

**Files:**
- Modify: `trading/src/tradex_trading/interface/routes/orders.py` (place/modify/cancel preflight)
- Modify: `trading/src/tradex_trading/sdk/session.py` (expose `mode` already present; add `orders_allowed` helper)
- New: `trading/tests/interface/test_replay_order_gate.py`

**Contract (tests must assert):**
1. Session in `replay` mode (or with an active replay task per connection): `POST/PUT/DELETE /orders*` → 422/403 with `detail="orders are disabled during replay"`.
2. `paper`/`live`/`backtest` modes: unchanged (existing route tests stay green).
3. The gate is server-side only — the frontend pill is not trusted.

**Steps:**

- [ ] **Step 1:** Write failing tests.
- [ ] **Step 2:** Run — expected FAIL (orders currently submit during replay).
- [ ] **Step 3:** Implement the mode preflight in the three mutation routes.
- [ ] **Step 4:** Run interface + session suites — expected PASS.
- [ ] **Step 5:** Commit: `fix(interface): reject order mutations while session is in replay mode`.

*Note:* this is the interim guard. The full replay-scoped session driver remains a gated follow-on phase (§6).

### Task 6: Frontend chart-interface token stability (N6 — P2)

**Files:**
- Modify: `frontend/src/trade-feed.ts` (`placeOrder` on `TradexTradeFeed`)
- Modify: `frontend/src/trade/integration.test.ts`

**Contract (tests must assert):**
1. `TradexTradeFeed.placeOrder()` accepts an optional `clientToken` in the payload and forwards it as `Idempotency-Key` when present.
2. Without a token, it generates one (current behavior).
3. The `OrderEngine` path is unchanged (already token-stable).

**Steps:**

- [ ] **Step 1:** Write failing tests.
- [ ] **Step 2:** Run `npm test` (or the repo's frontend test command) — expected FAIL.
- [ ] **Step 3:** Implement token passthrough.
- [ ] **Step 4:** Run frontend tests + `npm run typecheck` (or repo equivalent) — expected PASS.
- [ ] **Step 5:** Commit: `fix(frontend): forward client token through chart placeOrder for idempotency`.

### Task 7: Conservative SELL notional gate (N7 — P2)

**Files:**
- Modify: `trading/src/tradex_trading/execution/engine.py` (`RiskManager._mark_for_trade` / `_incoming_exposure`)
- Modify: `trading/tests/execution/test_engine_risk_gaps.py` (extend)

**Contract (tests must assert):**
1. SELL with a limit price above the bid mark: notional gate uses the **bid-side mark** (or max(request price, bid)) so short exposure is never understated; a limit far below market no longer collapses the notional check.
2. BUY keeps request-price-first behavior (limit above market is already conservative).
3. No mark available: `reject_unknown_market_value=True` still denies; paper/backtest defaults unchanged.

**Steps:**

- [ ] **Step 1:** Write failing tests.
- [ ] **Step 2:** Run — expected FAIL (SELL limit below market understates notional).
- [ ] **Step 3:** Implement side-aware mark selection mirroring `MarkToMarketService.select_mark` (BUY→ask/request, SELL→bid/request).
- [ ] **Step 4:** Run risk suites + parity goldens — expected PASS.
- [ ] **Step 5:** Commit: `fix(risk): mark SELL notional conservatively with the bid side`.

---

## Phase 3: Cleanups (P3)

### Task 8: Known cleanups (N8)

**Files:**
- Modify: `trading/src/tradex_trading/interface/routes/orders.py` — remove the duplicate `_require_idempotency_key` call in `modify_order` (F4)
- Modify: `brokers/src/tradex_brokers/paper/adapter.py` — keep, but add a contract test pinning `_update_position` output equality with `execution.position_math.apply_fill` (parity test, not refactor); note in docstring that domain extraction is tracked
- Modify: `trading/src/tradex_trading/runtime/startup.py` — anchor the writer-lock path to the repo root (F6): resolve `runtime/live` relative to `Path(__file__)` root, same pattern as the datalake-root fix (commit `c040245`)
- New: `trading/tests/runtime/test_writer_lock_cwd.py`

**Steps:**

- [ ] **Step 1:** Write/extend tests: writer lock created under repo-root `runtime/live/` regardless of cwd; paper-vs-engine position math parity.
- [ ] **Step 2:** Run — expected FAIL for the cwd case.
- [ ] **Step 3:** Implement the three small changes.
- [ ] **Step 4:** Run runtime + interface + paper suites — expected PASS.
- [ ] **Step 5:** Commit: `fix(runtime): anchor writer lock to repo root; drop duplicate key validation`.

---

## Phase 4: Gated follow-on (NOT in this plan's scope)

From `docs/design/tradex-target-architecture-and-refactoring-plan-2026-09-04.md` — do not start without a new plan and a fresh baseline:

1. **Delete `trading/events/`** (entire package + `trading/scripts/probe_review_fixes.py`) after porting its durable `EventStore` concepts into `execution/` (Phase 3 of the target plan).
2. **Replay-scoped session driver** — full C4 fix replacing the Task 5 interim gate.
3. **Versioned wire envelope** and one multiplexed frontend stream client (Phases 4 of the target plan).
4. **`fill_id` required at adapter boundary** — remove the composite fallback (C3 completion).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-04-execution-hardening-and-safety-fixes.md`. Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**