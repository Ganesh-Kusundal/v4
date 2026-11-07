# Review Remediation Plan 2 — Remaining Findings

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Close remaining findings from module/top-down reviews: observability concurrency, interfaces auth/concurrency, application input validation, reactive gating/memory, analytics warmup, replay duplicate engines.

**Architecture:** Targeted per-module fixes; module-level tests only.

**Tech Stack:** Python 3.12+, pytest.

## Global Constraints

- No full suite; module-level tests only.
- Preserve public interfaces unless noted.

## Review Focus

- Metrics unsynchronized access.
- Unauthenticated /metrics and /auth/login.
- Application unguarded Decimal.
- Reactive unbounded gating state.

---

### Task 1: Observability metrics locking
**Files:** `observability/src/tradex_observability/metrics.py`
- Add lock around `count_for`/`min_for`/`max_for`/`series_snapshot`/`render_prometheus`.
- Fix `get()` return type consistency; fix unlabelled render format; fix `sorted(families)` TypeError by custom key.
- Tests: `observability/tests/`.

### Task 2: Interfaces auth + concurrency
**Files:** `interfaces/src/tradex_interfaces/...`
- Add rate limiting to `/auth/login`.
- Guard `/metrics` (auth or bind restriction).
- Synchronize `app.state.replay_guard` lazy init.
- Fix `_drop_replay_frames` re-enqueue to not raise.
- Fix `cli.py` paper sessions start/stop.
- Fix `workspace_store.put()` revision handling.
- Tests: `interfaces/tests/`.

### Task 3: Application input validation
**Files:** `application/src/tradex_application/orders.py`
- Guard `Decimal(str(...))` with try/except → ValueError.
- Normalize whitespace-only instrument_id.
- Resolve ProductType.from_wire(None) contract.
- Tests: `application/tests/`, `trading/tests/interface/`.

### Task 4: Reactive gating memory + thread safety
**Files:** `reactive/src/tradex_reactive/bus.py`
- Clear per-subscriber gating state on unsubscribe/dispose.
- Dispose `_safe_observable` subscribers.
- Fix overflow backlog handling.
- Tests: `reactive/tests/`.

### Task 5: Analytics warmup + typo
**Files:** `analytics/src/tradex_analytics/`
- Fix `engine.py` warmup padding.
- Fix cache key unhashable params.
- Fix `_round_trips` sort None.
- Rename `MIN_DEFATED_SHARPE` → `MIN_DEFLATED_SHARPE` (update tests).
- Tests: `analytics/tests/`.

### Task 6: Replay duplicate engines
**Files:** `replay/src/tradex_replay/`
- Consolidate BacktestResult/BacktestEngine definitions.
- Fix monte_carlo global seed.
- Fix grid_search error swallowing.
- Tests: `replay/tests/`.

## Execution Handoff

Plan saved. Review? Execution approach: subagent-driven or native? I recommend subagent-driven. Does plan capture what you want?
