# Review Remediation Plan

**Date:** 2026-09-30
**Source:** Full project review (architecture, code quality, tests, git/docs)
**Branch:** `wave0-1-recovered`

---

## Summary

The refactoring objective is functionally complete: 4,308 tests pass, ruff clean, layering enforced, zero parity achieved. The remaining work is **hygiene, coverage gaps, and documentation** — not new refactoring.

---

## Phase 1: Commit Current Work (P0)

**Why:** 78 uncommitted changes is a liability. The work is done and tested.

### Tasks
- [ ] 1.1 Review staged diff for accidental inclusions (`git diff --cached --stat`)
- [ ] 1.2 Commit staged changes with descriptive message
- [ ] 1.3 Commit unstaged modifications (second commit)
- [ ] 1.4 Commit untracked source files (cash_manager, fee_processor, indicator_stream, market_builder, plan docs, conftest, test files)
- [ ] 1.5 Verify `git status` is clean

**Risk:** Low. All tests pass. Stash `stash@{0}` (drift-for-causality, 87 files) can be dropped after commit.

---

## Phase 2: Recover Orphan Tests (P0)

**Why:** 112K lines of negative-control tests exist only in `/tmp/orphan_inflight_tests/`. Two describe defects already fixed (verified: F-40 sizing parity and BarAggregator wiring are both in `startup.py`). The tests are stale but valuable — they match an established convention (42 existing "NEGATIVE CONTROL" files).

### Tasks
- [ ] 2.1 Copy `/tmp/orphan_inflight_tests/conftest.py` → `trading/tests/conftest.py` (merge with existing)
- [ ] 2.2 Copy `test_bar_aggregator_wiring.py` → `trading/tests/runtime/test_bar_aggregator_wiring.py`
- [ ] 2.3 Copy `test_paper_realism_gates.py` → `trading/tests/parity/test_paper_realism_gates.py`
- [ ] 2.4 Copy `test_recovery_snapshot_fold.py` → `trading/tests/execution/test_recovery_snapshot_fold.py`
- [ ] 2.5 Copy `test_sizing_parity.py` → `trading/tests/parity/test_sizing_parity.py`
- [ ] 2.6 Update each test to match current code (remove claims about fixed defects, adjust line references)
- [ ] 2.7 Run each test file to verify it passes against current code
- [ ] 2.8 Delete `/tmp/orphan_inflight_tests/` after successful integration

**Risk:** Medium. Tests may reference old APIs or line numbers. Need to verify each claim.

---

## Phase 3: Add Unit Tests for Untested Packages (P1)

**Why:** `strategy/`, `runtime/`, `replay/`, and `shared/` have no real tests — 16,000+ lines covered only incidentally.

### Tasks
- [ ] 3.1 `replay/tests/` — unit tests for `backtest.py` (1,234 lines): signal generation, fill simulation, bracket handling, split re-basing
- [ ] 3.2 `replay/tests/` — unit tests for `walk_forward.py` (826 lines): window generation, walk-forward iteration
- [ ] 3.3 `runtime/tests/` — unit tests for `bar_aggregator.py` (1,042 lines): bar formation, session boundaries, timestamp handling
- [ ] 3.4 `runtime/tests/` — unit tests for `feed_monitor.py` (813 lines): health checks, alert thresholds
- [ ] 3.5 `strategy/tests/` — unit tests for `sizing.py` (687 lines): position sizing, risk fraction, max order value
- [ ] 3.6 `strategy/tests/` — unit tests for `engine.py` (658 lines): signal processing, order submission
- [ ] 3.7 `shared/tests/` — unit tests for `rate_limiting.py` (332 lines): rate limit windows, token bucket
- [ ] 3.8 `shared/tests/` — unit tests for `market_builder.py` (66 lines): candle construction from DataFrame

**Risk:** Low. Pure unit tests, no integration dependencies.

---

## Phase 4: Move Credential/Runtime State Out of Source Tree (P1)

**Why:** `runtime/dhan/token_state.json`, `runtime/upstox/token_state.json` (mode 0600), `runtime/live/session.db`, `runtime/workspace.sqlite`, and 8 instrument-master JSON snapshots are untracked files in the package source directory.

### Tasks
- [ ] 4.1 Move `runtime/dhan/` and `runtime/upstox/` token state → `trading/data/runtime_state/` (gitignored)
- [ ] 4.2 Move `runtime/live/session.db` → `trading/data/runtime_state/`
- [ ] 4.3 Move `runtime/workspace.sqlite` → `trading/data/runtime_state/`
- [ ] 4.4 Move instrument-master snapshots → `trading/data/instrument_masters/`
- [ ] 4.5 Update any code that references these paths (check `runtime/paths.py` or similar)
- [ ] 4.6 Add `trading/data/runtime_state/` to `.gitignore` if not already covered
- [ ] 4.7 Move `runtime/audit_master_parity.py` into `runtime/src/` or `trading/scripts/`

**Risk:** Medium. Path changes may break runtime discovery. Need to verify boot still works.

---

## Phase 5: Fix Swallowed Exceptions (P2)

**Why:** 7 `except Exception: pass` sites with no logging. Broker clients and replay engine silently swallow errors.

### Tasks
- [ ] 5.1 `brokers/src/tradex_brokers/upstox/client.py:284` — add `log.warning(..., exc_info=True)`
- [ ] 5.2 `brokers/src/tradex_brokers/dhan/client.py:481` — add `log.warning(..., exc_info=True)`
- [ ] 5.3 `replay/src/tradex_replay/backtest.py:1038` — add `log.warning(..., exc_info=True)`
- [ ] 5.4 `execution/src/tradex_execution/risk.py:235` — add `log.debug(..., exc_info=True)` (already fails closed to None)
- [ ] 5.5 `interfaces/src/tradex_interfaces/routes/stream.py:1395` — add `log.warning(..., exc_info=True)`
- [ ] 5.6 Leave `session_impl.py:665` and `live_fill_bridge.py:486` as-is (justified teardown paths)

**Risk:** Low. Adding logging doesn't change control flow.

---

## Phase 6: Update Documentation (P2)

**Why:** ARCHITECTURE.md predates the entire current refactoring. Plan docs show tasks unchecked despite commits existing.

### Tasks
- [ ] 6.1 Update `docs/ARCHITECTURE.md` — add BarSource, tradex_shared, CashManager, FeeProcessor, IndicatorStream, market_builder
- [ ] 6.2 Update `docs/superpowers/plans/2026-09-30-extract-shared-resilience.md` — check off completed tasks
- [ ] 6.3 Update `docs/superpowers/plans/2026-09-30-zero-parity-bar-source.md` — check off completed tasks
- [ ] 6.4 Update `docs/superpowers/plans/2026-09-29-architectural-refactoring.md` — check off completed waves
- [ ] 6.5 Update `CLAUDE.md` — add tradex_shared to Architecture Quick Reference, mention BarSource
- [ ] 6.6 Update `CLAUDE.md` — add CashManager, FeeProcessor, IndicatorStream to module list

**Risk:** Low. Documentation only.

---

## Phase 7: Break Up stream.py:49 (P3)

**Why:** 282 cyclomatic complexity, 832 statements — 4x the next-worst function in the codebase.

### Tasks
- [ ] 7.1 Read `interfaces/src/tradex_interfaces/routes/stream.py:49` and map its 832 statements
- [ ] 7.2 Identify logical chunks (message dispatch, replay control, bar subscription, indicator stream, etc.)
- [ ] 7.3 Extract each chunk into a helper function or class method
- [ ] 7.4 Ensure all 38 tests in `test_ws_bars_replay.py` still pass
- [ ] 7.5 Run full interface test suite

**Risk:** High. This is the most complex function in the codebase. Needs careful incremental extraction.

---

## Phase 8: Add Async Test Infrastructure (P3)

**Why:** Zero async tests despite real asyncio code in `runtime/` and `interfaces/`.

### Tasks
- [ ] 8.1 Add `pytest-asyncio` to dev dependencies
- [ ] 8.2 Add `asyncio_mode = "auto"` to `pyproject.toml` or mark tests individually
- [ ] 8.3 Write 2-3 async smoke tests for `runtime/queueing.py`
- [ ] 8.4 Write 2-3 async smoke tests for `interfaces/routes/stream.py` (if feasible with TestClient)

**Risk:** Medium. Async tests can be flaky. Start with a few stable ones.

---

## Execution Order

```
Phase 1 (commit)  →  Phase 2 (orphan tests)  →  Phase 3 (unit tests)
                                                        ↓
Phase 6 (docs)  ←  Phase 5 (exceptions)  ←  Phase 4 (credentials)
                                                        ↓
                                              Phase 7 (stream.py)
                                                        ↓
                                              Phase 8 (async tests)
```

Phases 1–2 are P0 (do first). Phases 3–4 are P1. Phases 5–6 are P2. Phases 7–8 are P3.

---

## Success Criteria

- [ ] `git status` clean after Phase 1
- [ ] All 5 orphan test files integrated and passing
- [ ] `replay/`, `runtime/`, `strategy/`, `shared/` each have ≥10 unit tests
- [ ] No credential files in `runtime/` source tree
- [ ] 0 `except Exception: pass` without logging
- [ ] ARCHITECTURE.md mentions all new packages and modules
- [ ] `stream.py:49` complexity reduced below 100
- [ ] At least 5 async tests passing