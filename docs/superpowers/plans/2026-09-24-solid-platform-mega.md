# Solid Platform Mega Remediation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Parallelism:** Within a wave, tasks marked `PARALLEL` may run as concurrent subagents. Waves are sequential: A → B → C (except C2/C3 may start after A if they only touch execution/runtime tests).

**Goal:** Make live trading fail closed, make the package DAG acyclic, and make backtest/recon claims honest under CI.

**Architecture:** Three waves — (A) ExecutionEngine + boot Ready contract, (B) `tradex_config` + break interfaces/runtime → trading cycles + CI package coverage, (C) bracket parity decision + recon golden + crash-restart test. Keep single OMS spine (`apply_fill`, `ExecutionEngine`).

**Tech Stack:** Python 3.12, uv workspace, pytest, existing `KillSwitch` / `RiskManager` / `ReconciliationEngine` / `SQLiteEventStore`, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-24-solid-platform-mega-design.md`

## Global Constraints

- No git commit unless the human explicitly asks (overrides writing-plans “Commit” steps — skip those steps).
- Patch and test package authority (`tradex_execution`, `tradex_runtime`, …), never shim-only.
- No second position or cash model.
- Paper mode may exempt feed READY; live mode may not.
- Fail closed beats silent continue.
- TDD: failing test first for each behavior change.

## File map

| Path | Role |
|------|------|
| `execution/src/tradex_execution/engine.py` | Fail-closed pipeline, `_append`, reactive rejects |
| `execution/src/tradex_execution/risk.py` | Unchanged API; always wired in live |
| `runtime/src/tradex_runtime/startup.py` | Live Ready gates, recon unavailable → refuse start |
| `trading/src/tradex_trading/config/*` → `config/src/tradex_config/` | Wave B extract |
| `interfaces/src/tradex_interfaces/**` | Drop `tradex_trading` imports |
| `tests/test_import_boundaries.py` | Enforce new DAG |
| `.github/workflows/quality-gate.yml` | Cov/mypy extracted packages |
| `.github/workflows/parity.yml` | Recon golden |
| `replay/src/tradex_replay/backtest.py` | Bracket simulation or strip |
| `trading/tests/execution/`, `trading/tests/runtime/`, `trading/tests/parity/` | Behavioral tests |

---

## Wave A — Fail-closed money

### Task A1: Live unbound risk rejects (PARALLEL with A2)

**Files:**
- Modify: `execution/src/tradex_execution/engine.py`
- Test: `trading/tests/execution/test_fail_closed_risk.py` (create)

**Interfaces:**
- Consumes: `ExecutionEngine(..., risk_manager=None)`, `_run_pipeline` / `_process_request`
- Produces: when `require_risk=True` (or live mode flag) and `_risk is None` → `OrderRejected` with reason `RISK_UNBOUND`; never submit to fill source

- [ ] **Step 1: Write failing test**

```python
# trading/tests/execution/test_fail_closed_risk.py
from decimal import Decimal
from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Quantity, Price
from tradex_execution.engine import ExecutionEngine
# build bus + SimulatedFillSource as in test_execution_engine.py

def test_unbound_risk_rejects_when_required(bus, fill_source):
    engine = ExecutionEngine(bus, fill_source, risk_manager=None, require_risk=True)
    # submit OrderRequest for NSE:RELIANCE qty 1
    # assert OrderRejected published with reason containing RISK_UNBOUND
    # assert fill_source never called / no fill
```

- [ ] **Step 2: Run test — expect FAIL** (`require_risk` missing or still admits)

```bash
.venv/bin/python -m pytest trading/tests/execution/test_fail_closed_risk.py -q --timeout=15
```

- [ ] **Step 3: Implement** — add `require_risk: bool = False` to `ExecutionEngine.__init__`. In `_run_pipeline` before risk check: if `require_risk and self._risk is None`: reject with stable reason, return. Default False preserves paper/unit callers; startup sets True for live.

- [ ] **Step 4: Tests pass**

- [ ] **Step 5: Skip commit** unless human asks

---

### Task A2: Feed not READY emits OrderRejected (PARALLEL with A1)

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (`_process_request` / bus filter)
- Test: `trading/tests/execution/test_fail_closed_feed.py` (create)

**Interfaces:**
- Consumes: `FeedReady.ready`
- Produces: not-ready → `OrderRejected` reason `FEED_NOT_READY` (reactive path included; no silent None)

- [ ] **Step 1: Failing test** — supervisor `ready=False`, publish `OrderRequest` on bus, assert `OrderRejected` received within timeout; assert kill-switch filter does not drop without event when feed gate fails.

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement** — replace silent return paths with `_reject(request, reason=...)`. Keep kill-switch filter but ensure imperative `submit` and reactive path both emit rejects. When `feed_supervisor` is None and `require_feed=True`, reject `FEED_UNBOUND`.

- [ ] **Step 4: Pass**

- [ ] **Step 5: Skip commit**

---

### Task A3: Event append failure fails closed in live

**Files:**
- Modify: `execution/src/tradex_execution/engine.py` (`_append`)
- Test: `trading/tests/execution/test_fail_closed_event_store.py` (create)

**Interfaces:**
- Consumes: `EventStore.append`
- Produces: on failure when `require_durable_events=True` → trip kill switch + publish `ErrorOccurred` (or `OrderSubmissionUnknownError` if mid-submit)

- [ ] **Step 1: Failing test** — fake store that raises; engine with `require_durable_events=True`; trigger `_append` via successful paper fill path; assert kill switch active.

- [ ] **Step 2–4: Implement minimal: wrap `_append` to re-raise path that trips kill switch when flag set; keep swallow when flag False (backtest).**

- [ ] **Step 5: Skip commit**

---

### Task A4: Boot wires fail-closed flags + refuses READY (depends A1–A3)

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py`
- Test: `trading/tests/runtime/test_live_boot_fail_closed.py` (create)
- Modify if needed: `trading/tests/sdk/test_session_readiness.py` (update expectations)

**Interfaces:**
- Consumes: `cfg.mode == "live"`, persistence path, RiskManager construction
- Produces: live boot constructs `ExecutionEngine(..., require_risk=True, require_feed=True, require_durable_events=True)`; missing event store or risk → do not `session.start()`; log critical

- [ ] **Step 1: Failing test** — boot live with persistence disabled / no event store → session not READY / start skipped.

- [ ] **Step 2–4: Wire flags in `_build_engine` (or equivalent around line 349–368). If live and `event_store is None`, set `reconcile_tripped`-style refuse start.

- [ ] **Step 5: Skip commit**

---

### Task A5: Recon broker-unavailable fails closed (PARALLEL after A4 starts)

**Files:**
- Modify: `runtime/src/tradex_runtime/startup.py` (`_run_startup_reconciliation`)
- Test: `trading/tests/runtime/test_startup_reconcile_unavailable.py` (create or extend)

**Interfaces:**
- Today: both `book` and `positions` None → `return False` (continue).  
- Target live: both unavailable → treat as refuse READY (return True / trip kill switch with reason `RECON_UNAVAILABLE`).

- [ ] **Step 1: Failing test** — broker methods raise; assert start refused.

- [ ] **Step 2–4: Change early return when both None in live path to trip + return True.**

- [ ] **Step 5: Skip commit**

---

### Task A6: Wave A verification slice

- [ ] Run:

```bash
.venv/bin/python -m pytest \
  trading/tests/execution/test_fail_closed_risk.py \
  trading/tests/execution/test_fail_closed_feed.py \
  trading/tests/execution/test_fail_closed_event_store.py \
  trading/tests/runtime/test_live_boot_fail_closed.py \
  trading/tests/runtime/test_startup_reconcile_unavailable.py \
  trading/tests/sdk/test_session_readiness.py \
  -q --timeout=30
```

Expected: all pass.

---

## Wave B — Package DAG

### Task B1: Extract `tradex_config`

**Files:**
- Create: `config/pyproject.toml`, `config/src/tradex_config/{__init__,schema,env}.py` (move from `trading/src/tradex_trading/config/`)
- Modify: root `pyproject.toml` workspace members
- Modify: `runtime/pyproject.toml`, `trading/pyproject.toml`, `interfaces/pyproject.toml` deps
- Shim: leave `tradex_trading.config` as importlib re-export
- Test: `config/tests/test_package_import.py`, extend `tests/test_import_boundaries.py`

- [ ] **Step 1: Move modules; add workspace member; `uv sync`**
- [ ] **Step 2: Point runtime `from tradex_config.schema import AppConfig`**
- [ ] **Step 3: Boundary test forbids `tradex_runtime` importing `tradex_trading`**
- [ ] **Step 4: `pytest tests/test_import_boundaries.py config/tests -q`**
- [ ] **Step 5: Skip commit**

---

### Task B2: Break interfaces → tradex_trading (depends B1)

**Files:**
- Modify: `interfaces/src/tradex_interfaces/fastapi_app.py`, `routes/chart.py`, other routes importing `tradex_trading.sdk` / `config`
- Prefer: depend on `tradex_runtime.boot`, `tradex_config`, `tradex_application`, `tradex_strategy` directly
- Chart loader: `importlib.import_module(f"tradex_strategy.extensions.strategies.{module_name}")`

- [ ] **Step 1: Grep `tradex_trading` under `interfaces/src`; replace each import**
- [ ] **Step 2: Remove `tradex-trading` from interfaces deps if present; add needed packages**
- [ ] **Step 3: `pytest trading/tests/interface -q --timeout=30` (patch targets stay on `tradex_interfaces`)**
- [ ] **Step 4: Skip commit**

---

### Task B3: CI package coverage (PARALLEL with B4 after B1)

**Files:**
- Modify: `.github/workflows/quality-gate.yml`

- [ ] **Step 1: Add mypy steps for `execution`, `analytics`, `strategy`, `interfaces`, `runtime` (fail hard)**
- [ ] **Step 2: Extend pytest `--cov=` to those packages**
- [ ] **Step 3: Document that `trading` mypy may remain continue-on-error until shims shrink**

---

### Task B4: Retarget analytics goldens to `tradex_analytics` (PARALLEL with B3)

**Files:**
- Modify: `trading/tests/analytics/test_golden_parity*.py` imports → `tradex_analytics`
- Prefer single golden root: `analytics/src/tradex_analytics/goldens/` or keep tests path but import package

- [ ] **Step 1: Change imports; run `pytest trading/tests/analytics/test_golden_parity.py -q --timeout=60`**
- [ ] **Step 2: Skip commit**

---

### Task B5: Wave B verification

```bash
.venv/bin/python -m pytest tests/test_import_boundaries.py trading/tests/interface -q --timeout=60
```

---

## Wave C — Bracket + recon parity

### Task C1: Bracket parity — simulate SL/TP in backtest (recommended Option C1)

**Files:**
- Modify: `replay/src/tradex_replay/backtest.py`
- Modify: `strategy/src/tradex_strategy/core/brackets.py` (reuse geometry helpers)
- Test: `trading/tests/parity/test_bracket_backtest_parity.py` (create)

**Decision locked:** Option C1 — simulate protective exits on subsequent bars using stop/target from signal metadata; if incomplete pair, no protective exit (same as live degrade).

- [ ] **Step 1: Failing test** — strategy emits entry + stop/target; synthetic bars pierce stop; assert exit fill at stop (or documented slippage rule matching `SimulatedFillSource`).
- [ ] **Step 2–4: Implement bar-loop check after entry; publish exit through same engine submit path.
- [ ] **Step 5: Skip commit**

---

### Task C2: Recon golden in parity CI (PARALLEL with C1 after Wave A)

**Files:**
- Create: `trading/tests/parity/goldens/recon_startup_drift.json` (broker orders/positions + local cache snapshot + expected severities)
- Create: `trading/tests/parity/test_recon_golden.py`
- Modify: `.github/workflows/parity.yml` to include the test

- [ ] **Step 1: Fixture from existing `test_reconciliation` cases**
- [ ] **Step 2: Assert drift keys + kill switch trip for CRITICAL**
- [ ] **Step 3: Wire CI**

---

### Task C3: Crash-restart recovery test (PARALLEL with C2)

**Files:**
- Test: `trading/tests/runtime/test_event_store_crash_restart.py` (create)
- Uses: `SQLiteEventStore`, `SessionRecovery`, temp path

- [ ] **Step 1: Place order + fill with durable store; drop engine; new engine + recover; assert order/position present**
- [ ] **Step 2–4: Implement test only if recovery already works; if gap in recovery, fix `recovery.py` minimally**

---

### Task C4: Final verification

```bash
.venv/bin/python -m pytest \
  trading/tests/execution/test_fail_closed_*.py \
  trading/tests/runtime/test_live_boot_fail_closed.py \
  trading/tests/runtime/test_startup_reconcile_unavailable.py \
  trading/tests/runtime/test_event_store_crash_restart.py \
  trading/tests/parity/test_bracket_backtest_parity.py \
  trading/tests/parity/test_recon_golden.py \
  tests/test_import_boundaries.py \
  trading/tests/interface \
  -q --timeout=60
```

---

## Subagent dispatch map

| Phase | Parallel agents | Barrier |
|-------|-----------------|--------|
| Wave A start | A1, A2 | Then A3 |
| Wave A boot | A4 then A5 | Then A6 |
| Wave B start | B1 | Then B2 \|\| B3 \|\| B4 |
| Wave B end | B5 | |
| Wave C | C1 \|\| C2 \|\| C3 | Then C4 |

After each task: task-reviewer subagent (spec + quality). After C4: broad branch review. No commit unless asked.

## Spec coverage checklist

| Spec requirement | Task |
|------------------|------|
| Unbound risk rejects | A1 |
| Feed not READY / unbound | A2 |
| Event append fail closed | A3 |
| Live boot wires flags + refuse READY | A4 |
| Recon unavailable refuse | A5 |
| tradex_config extract | B1 |
| interfaces without trading | B2 |
| CI package cov/mypy | B3 |
| Golden package imports | B4 |
| Bracket simulate | C1 |
| Recon golden CI | C2 |
| Crash-restart | C3 |
