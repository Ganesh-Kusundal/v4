# Package Extraction Waves 2–N Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (preferred) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish Phase 10 package extraction — every target package is a real workspace member with code ownership, compatibility shims under `tradex_trading.*`, import gates, and verified tests. Nothing left “deferred.”

**Architecture:** Strangler fig (same as Wave 1). Move tree → rewrite `tradex_trading.<pkg>` → `tradex_<pkg>` inside the new package → leave thin re-export shims at the old paths → wire uv workspace → extend AST boundary gates → run focused then broad tests.

**Prerequisite (cycle break):** `ExecutionEngine` must not import `tradex_trading.runtime`. Replace `FeedSupervisor` type usage with a local `Protocol` (`ready: bool`). Move `runtime/metrics.py` to `tradex_observability` before extracting execution.

**Tech stack:** uv workspace, hatchling, pytest importlib mode, AST import gates.

---

## Dependency order (hard)

```text
tradex-domain, tradex-brokers          (exist)
tradex-research, tradex-operations     (Wave 1 done)
tradex-observability                   (metrics)
tradex-analytics                       (pure; domain only)
tradex-reactive                        (domain + self)
tradex-execution                       (domain, reactive, observability; optional brokers)
tradex-strategy                        (domain, analytics; scanners may need datalake)
tradex-market-data                     (feed_* + datalake paths as needed)
tradex-replay                          (execution, strategy, analytics, market-data)
tradex-interfaces                      (composition consumers — last of the leaves)
tradex-runtime                         (boot/composition; depends on the above)
tradex-persistence                     (sqlite stores extracted from execution OR thin re-export)
tradex-trading                         (composition root + shims only)
```

**Note:** `tradex-analytics` / `tradex-reactive` are required prerequisites not named in the original target list; they unblock strategy/execution without reverse deps into `tradex-trading`.

---

## Mechanical recipe (every package)

1. Create `{pkg}/pyproject.toml` + `{pkg}/src/tradex_{name}/`.
2. Move (or copy-then-delete) modules from `trading/src/tradex_trading/{old}/`.
3. Rewrite imports inside the new package.
4. Generate shim files at old paths: `from tradex_{name}.module import *` (+ `__all__` when present).
5. Add workspace member, pythonpath, trading (or peer) dependency.
6. Extend `tests/test_import_boundaries.py`.
7. `uv sync` + pytest for package tests + shim compatibility + boundaries.
8. SDD report line in `.superpowers/sdd/progress.md`.

**Do not commit** unless the user asks.

---

## Task 1: Observability + FeedReady protocol

**Files:**
- Create: `observability/pyproject.toml`, `observability/src/tradex_observability/metrics.py`, `__init__.py`, `observability/tests/test_metrics_import.py`
- Modify: `trading/.../execution/engine.py` (Protocol for feed ready)
- Modify: callers of `tradex_trading.runtime.metrics` → `tradex_observability`
- Shim: `trading/.../runtime/metrics.py` re-exports

- [ ] Move metrics; wire workspace; Protocol for feed ready; tests green

## Task 2: Analytics package

- [ ] Extract `analytics/` → `tradex_analytics`; full submodule shims; run `trading/tests/analytics` + boundaries

## Task 3: Reactive package

- [ ] Extract `reactive/` → `tradex_reactive`; shims; tests that import bus

## Task 4: Execution package

- [ ] Extract `execution/` → `tradex_execution`; deps: domain, reactive, observability; shims; run `trading/tests/execution`

## Task 5: Strategy package

- [ ] Extract `strategy/` → `tradex_strategy`; deps: domain, analytics (+ datalake only if required — prefer keeping scanner datalake import via optional/lazy or market-data); shims; `trading/tests/strategy`

## Task 6: Market-data package

- [ ] Extract feed supervisor/monitor/recovery/integrity + market_feed/bar_aggregator (+ datalake if cleanly owned); `tradex_market_data`; shims under runtime/datalake as appropriate

## Task 7: Replay package

- [ ] Extract `replay/` → `tradex_replay`; shims; `trading/tests` replay/backtest focused

## Task 8: Interfaces package

- [ ] Extract `interface/` → `tradex_interfaces`; shims; interface tests

## Task 9: Runtime + persistence

- [ ] Extract remaining `runtime/` boot/composition to `tradex_runtime`
- [ ] `tradex_persistence`: sqlite stores (move from execution or re-export)
- [ ] `tradex_trading` left as composition + shims

## Task 10: Full verification

- [ ] `uv sync`
- [ ] `pytest tests/test_import_boundaries.py research/tests operations/tests observability/tests` (and each new package tests)
- [ ] `pytest trading/tests/execution trading/tests/strategy trading/tests/runtime trading/tests/interface trading/tests/analytics -q` (broad)
- [ ] Update design status to **complete**; SDD wave reports; progress ledger

## Rollback

Delete new workspace member; restore moved tree from shim breakage by reverting the move commit (or copy back from package src). Keep Wave 1 research/operations intact.

## Exit gate

Every package in the target architecture list (plus analytics/reactive prerequisites) exists as a workspace member, has contract/import tests, and shims preserve `tradex_trading.*` imports. Progress ledger shows no deferred extraction items.
