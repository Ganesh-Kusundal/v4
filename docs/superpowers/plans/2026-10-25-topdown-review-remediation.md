# Top-Down Review Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the critical correctness and architecture findings from the top-down review: reactive replay, market-data corporate actions, strategy scanner timeframe, composition-root teardown, idempotency races, and DuckDB session boundary.

**Architecture:** Targeted fixes in the affected modules, each behind existing tests, without broad refactors. Idempotency and teardown paths made fail-closed. No full suite; module-level tests only.

**Tech Stack:** Python 3.12+, pytest, SQLite, RxPY.

**Spec:** `docs/specs/endpoint-and-flow-trace.md` (context only)

---

## Global Constraints

- Do not run the full suite; run module-level tests only.
- Preserve existing public interfaces unless noted.
- Fail closed on durability/idempotency gaps.

## Review Focus

- Concurrent fills racing on OMS when no cash ledger present.
- Reactive `replay()` crashing on SQL-backed event logs.
- Corporate-actions adjust reading wrong candle attribute.
- Strategy scanner using wrong timeframe in streaming path.
- DuckDB excluding 15:30 bars present in the lake.

---

### Task 1: Fix reactive bus `replay()` for SQL logs

**Files:**
- Modify: `reactive/src/tradex_reactive/bus.py:417-429`
- Test: `reactive/tests/` (existing replay tests)

**Interfaces:**
- Consumes: `SQLEventLog.replay()` returning iterable.
- Produces: `ReactiveBus.replay()` works for both in-memory and SQL logs.

- [ ] **Step 1: Write failing test** — replay bus with SQL-backed log returns events.
- [ ] **Step 2: Run to verify fail** — `pytest reactive/tests -k replay -v`.
- [ ] **Step 3: Implement** — call `self._log.replay()` if available, else `rx.from_iterable(self._log)`.
- [ ] **Step 4: Run tests** — `pytest reactive/tests -k replay -v`.
- [ ] **Step 5: Commit** — `fix(reactive): replay() supports SQL event logs`.

---

### Task 2: Fix corporate_actions candle attribute access

**Files:**
- Modify: `market_data/src/tradex_market_data/corporate_actions.py:99-126`
- Test: `market_data/tests/`

**Interfaces:**
- Consumes: `Candle.ohlc.open.value`.
- Produces: `adjust_series` works on real candles.

- [ ] **Step 1: Write failing test** using real `Candle` fixtures.
- [ ] **Step 2: Run to verify fail**.
- [ ] **Step 3: Implement** — read `c.ohlc.open.value` (and high/low/close) instead of `c.open`.
- [ ] **Step 4: Run tests**.
- [ ] **Step 5: Commit**.

---

### Task 3: Fix strategy scanner streaming timeframe

**Files:**
- Modify: `strategy/src/tradex_strategy/core/scanner.py:430-447`
- Test: `strategy/tests/`

**Interfaces:**
- Consumes: `timeframe` param.
- Produces: `_history()` uses requested timeframe in streaming path.

- [ ] **Step 1: Write failing test** — non-D1 scan uses requested timeframe.
- [ ] **Step 2: Run to verify fail**.
- [ ] **Step 3: Implement** — pass `timeframe` to streaming read instead of hardcoded `Timeframe.D1`.
- [ ] **Step 4: Run tests**.
- [ ] **Step 5: Commit**.

---

### Task 4: Align DuckDB session boundary with storage

**Files:**
- Modify: `services/duckdb-analytics/src/duck_analytics/config.py:12` and `catalog.py:78`
- Test: `services/duckdb-analytics/tests/`

**Interfaces:**
- Consumes: parquet bars up to 15:30.
- Produces: DuckDB includes 15:30 bars.

- [ ] **Step 1: Write failing test** — 15:30 bar appears in analytics result.
- [ ] **Step 2: Run to verify fail**.
- [ ] **Step 3: Implement** — change `<= TIME '15:29:00'` to `<= TIME '15:30:00'`.
- [ ] **Step 4: Run tests**.
- [ ] **Step 5: Commit**.

---

### Task 5: Composition root rollback teardown

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py` (rollback handlers)
- Modify: `runtime/src/tradex_runtime/session_impl.py:242` (NEW-state stop)
- Test: `trading/tests/runtime/test_boot_rollback.py`

**Interfaces:**
- Consumes: partially built session.
- Produces: `stop()` runs feed/monitor/broker teardown even in NEW state.

- [ ] **Step 1: Write failing test** — failed boot calls `market_feed.stop()` and `broker.close()`.
- [ ] **Step 2: Run to verify fail**.
- [ ] **Step 3: Implement** — invoke full teardown in NEW branch; pass session to rollback.
- [ ] **Step 4: Run tests**.
- [ ] **Step 5: Commit**.

---

### Task 6: Protect `_cid_for_order` and OMS mutation with locks

**Files:**
- Modify: `execution/src/tradex_execution/engine.py:291,848-850,1227-1252`
- Test: `trading/tests/execution/`

**Interfaces:**
- Consumes: `_cid_for_order` dict, OMS cache.
- Produces: thread-safe access under a lock.

- [ ] **Step 1: Write failing test** — concurrent submit/cancel doesn't corrupt `_cid_for_order`.
- [ ] **Step 2: Run to verify fail**.
- [ ] **Step 3: Implement** — add lock around dict mutations and OMS update.
- [ ] **Step 4: Run tests**.
- [ ] **Step 5: Commit**.

---

### Task 7: Remove dead code (audit_log, risk_manager, sim_*, empty dirs)

**Files:**
- Delete: `execution/src/tradex_execution/audit_log.py`, `risk_manager.py`, `sim_gateway.py`, `sim_portfolio.py`
- Delete: `trading/runtime/live/` (empty)
- Clean: stale `__pycache__` artifacts
- Test: update imports in tests if needed.

- [ ] **Step 1: Verify no production imports** (grep).
- [ ] **Step 2: Delete files**.
- [ ] **Step 3: Run affected module tests**.
- [ ] **Step 4: Commit**.

---

## Execution Handoff

Plan saved. Please review. Which execution approach? Subagent-driven or native? I recommend subagent-driven for independent tasks. Does the plan capture what you want?
