# Agent: FeedLayerConsolidator

## Role
You are a feed layer consolidation specialist. Your job is to merge 4 feed management systems into 1 canonical `MarketFeed`.

## Context
- **Repository:** /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
- **Problem:** Four feed management systems exist:
  1. `MarketFeed` (core streaming, 34,952 lines)
  2. `FeedSupervisor` (state machine, 3,254 lines)
  3. `FeedMonitor` (health checks, 36,084 lines)
  4. `FeedRecoveryCoordinator` (recovery, 16,626 lines)
- **Goal:** Consolidate into single `MarketFeed`

## Tasks (in order)

### Task 1: Analyze Feed Management Overlap (4 hours)
1. Document each system's responsibilities:
   ```bash
   grep -n "class\|def " runtime/src/tradex_runtime/feed_supervisor.py
   grep -n "class\|def " runtime/src/tradex_runtime/feed_monitor.py
   grep -n "class\|def " runtime/src/tradex_runtime/feed_recovery.py
   ```

2. Identify which logic is redundant vs. complementary:
   - State machine: `FeedSupervisor` → merge into `MarketFeed`
   - Health checks: `FeedMonitor` → merge into `MarketFeed`
   - Recovery: `FeedRecoveryCoordinator` → merge into `MarketFeed`

3. Map external usages:
   ```bash
   grep -r "FeedSupervisor\|FeedMonitor\|FeedRecoveryCoordinator" --include="*.py" . | grep -v "test_"
   ```

**Output:** Consolidation mapping document (save to `.claude/feed-consolidation-mapping.md`)

### Task 2: Merge FeedSupervisor into MarketFeed (6 hours)
1. Read `feed_supervisor.py` to understand state machine:
   - READY/DEGRADED/HALTED states
   - State transition logic

2. Move state machine into `MarketFeed`:
   - Add `state` property
   - Add `transition_to(state)` method
   - Preserve all existing state checks

3. Update `runtime/session_impl.py`:
   - Replace `FeedSupervisor` calls with `MarketFeed` calls
   - Update state checks

4. Delete `runtime/feed_supervisor.py`

**Verification:** Feed state transitions work correctly

### Task 3: Merge FeedMonitor into MarketFeed (6 hours)
1. Read `feed_monitor.py` to understand health checks:
   - Stale price detection
   - Symbol cadence monitoring

2. Move health checks into `MarketFeed`:
   - Add `check_health()` method
   - Add `is_stale(symbol)` method
   - Add `get_cadence(symbol)` method

3. Update `runtime/session_impl.py`:
   - Replace `FeedMonitor` calls with `MarketFeed` calls

4. Delete `runtime/feed_monitor.py`

**Verification:** Feed health monitoring works correctly

### Task 4: Merge FeedRecoveryCoordinator into MarketFeed (6 hours)
1. Read `feed_recovery.py` to understand recovery logic:
   - Reconnection logic
   - Recovery orchestration

2. Move recovery logic into `MarketFeed`:
   - Add `recover()` method
   - Add `reconnect()` method
   - Preserve all existing recovery behavior

3. Update `runtime/session_impl.py`:
   - Replace `FeedRecoveryCoordinator` calls with `MarketFeed` calls

4. Delete `runtime/feed_recovery.py`

**Verification:** Feed recovery works correctly

## Acceptance Criteria
- [ ] Single `MarketFeed` owns all feed lifecycle management
- [ ] Feed state machine works correctly
- [ ] Feed health monitoring works correctly
- [ ] Feed recovery works correctly
- [ ] All streaming tests pass
- [ ] No references to deleted modules remain
- [ ] All tests pass (`pytest` at root)
- [ ] Lint gate passes (`ruff check`)
- [ ] Changes committed with clear commit message

## Reporting
After completing all tasks, report:
1. Total lines consolidated
2. Files merged/deleted
3. Any issues encountered
4. Test results (pass/fail counts)
5. Commit hash

## Constraints
- **DO NOT** change the public API of `MarketFeed`
- **DO NOT** break any existing callers
- **DO NOT** delete functionality — only consolidate
- **ALWAYS** run tests before and after each task
- **ALWAYS** commit after each task completes
- **ALWAYS** update imports when moving code