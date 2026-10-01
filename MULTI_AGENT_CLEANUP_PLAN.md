# Multi-Agent Execution Plan: TradeX v4 Pre-Deployment Cleanup

**Created:** 2026-10-01  
**Based on:** PREDEPLOY_SYSTEM_REVIEW.md  
**Goal:** Eliminate dead code, consolidate duplicates, achieve deployment readiness in 8-13 days

---

## Executive Summary

This plan decomposes the pre-deployment cleanup into **parallel work streams** that can be executed by a multi-agent team. Each stream is independently verifiable and has clear acceptance criteria.

**Team Structure:**
- **Orchestrator** (main agent) — Coordinates work streams, resolves conflicts, integrates results
- **4 Work Stream Agents** — Execute cleanup tasks in parallel
- **1 QA Agent** — Verifies each phase before proceeding

---

## Work Stream Decomposition

### **Stream 1: Dead Code Elimination** (2-3 days)
**Agent:** DeadCodeHunter  
**Goal:** Remove all provably dead code with zero behavioral change

#### Tasks:
1. **Delete dead directories** (Day 1, 2 hours)
   - `poc/` (30 files, 15,000 lines)
   - `apps/` (1 file, 500 lines)
   - `graphify-out/` (generated artifacts)
   
   **Verification:** `grep -r "from poc\|import poc" --include="*.py"` returns nothing

2. **Delete one-time migration scripts** (Day 1, 1 hour)
   - `trading/scripts/backfill_2025.py`
   - `trading/scripts/backtest_datalake.py`
   - `trading/scripts/sync_today.py`
   - `trading/scripts/topup_gaps.py`
   
   **Verification:** Scripts are not referenced in any active code or documentation

3. **Delete deprecated token manager** (Day 2, 4 hours)
   - Remove `DurableTokenManager` from `brokers/common/token_lifecycle.py`
   - Remove legacy v2 token file reading
   - Migrate any remaining callers to `PortTokenManager`
   
   **Verification:** `grep -r "DurableTokenManager" --include="*.py"` returns nothing

4. **Delete legacy compatibility layers** (Day 2, 2 hours)
   - Remove legacy rate limit re-exports from `brokers/common/resilience.py`
   - Remove legacy v2 field handling
   
   **Verification:** All tests pass, no legacy imports remain

5. **Delete ParquetBacktestLoader** (Day 3, 6 hours)
   - Migrate `trading/scripts/backtest_datalake.py` callers to `MarketWindowLoader`
   - Migrate `strategy/extensions/strategies/multi_symbol_sma_cross.py`
   - Delete `market_data/backtest_loader.py`
   
   **Verification:** All backtest paths work through `MarketWindowLoader`

**Acceptance Criteria:**
- [ ] 25,000+ lines deleted
- [ ] All tests pass (`pytest` at root)
- [ ] No dead imports remain (`vulture` or `grep` verification)
- [ ] Lint gate passes (`ruff check`)

---

### **Stream 2: Data Layer Consolidation** (3-4 days)
**Agent:** DataLayerConsolidator  
**Goal:** Merge 3 data loading paths into 1 canonical `MarketWindowLoader`

#### Tasks:
1. **Analyze ParquetMarketProvider** (Day 1, 4 hours)
   - Document all public methods
   - Identify which methods are used externally
   - Map to `MarketWindowLoader` equivalents
   
   **Output:** Migration mapping document

2. **Merge ParquetMarketProvider into MarketWindowLoader** (Day 2-3, 8 hours)
   - Move `history()` method into `MarketWindowLoader`
   - Move `candles()` method into `MarketWindowLoader`
   - Update all imports in `interfaces/routes/chart.py`
   - Update all imports in `strategy/extensions/strategies/multi_symbol_sma_cross.py`
   - Update all imports in `runtime/session_impl.py`
   
   **Verification:** All chart history and backtest paths work correctly

3. **Delete ParquetMarketProvider** (Day 4, 2 hours)
   - Delete `market_data/market_provider.py`
   - Update `market_data/__init__.py`
   
   **Verification:** `grep -r "ParquetMarketProvider" --include="*.py"` returns nothing

4. **Delete simple_sync.py** (Day 4, 2 hours)
   - Verify `backfill_parquet.py` covers all functionality
   - Delete `market_data/simple_sync.py`
   - Update `market_data/__init__.py`
   
   **Verification:** All backfill scripts work correctly

**Acceptance Criteria:**
- [ ] Single `MarketWindowLoader` is the only data loading entry point
- [ ] All chart history tests pass
- [ ] All backtest tests pass
- [ ] All backfill scripts work
- [ ] No references to deleted modules remain

---

### **Stream 3: Feed Layer Consolidation** (3-4 days)
**Agent:** FeedLayerConsolidator  
**Goal:** Merge 4 feed management systems into 1 `MarketFeed`

#### Tasks:
1. **Analyze feed management overlap** (Day 1, 4 hours)
   - Document `FeedSupervisor` state machine
   - Document `FeedMonitor` health checks
   - Document `FeedRecoveryCoordinator` recovery logic
   - Identify which logic is redundant vs. complementary
   
   **Output:** Consolidation mapping document

2. **Merge FeedSupervisor into MarketFeed** (Day 2, 6 hours)
   - Move state machine (READY/DEGRADED/HALTED) into `MarketFeed`
   - Update `runtime/session_impl.py` to use new API
   - Delete `runtime/feed_supervisor.py`
   
   **Verification:** Feed state transitions work correctly

3. **Merge FeedMonitor into MarketFeed** (Day 3, 6 hours)
   - Move stale price detection into `MarketFeed`
   - Move symbol cadence monitoring into `MarketFeed`
   - Update `runtime/session_impl.py`
   - Delete `runtime/feed_monitor.py`
   
   **Verification:** Feed health monitoring works correctly

4. **Merge FeedRecoveryCoordinator into MarketFeed** (Day 4, 6 hours)
   - Move reconnection logic into `MarketFeed`
   - Move recovery orchestration into `MarketFeed`
   - Update `runtime/session_impl.py`
   - Delete `runtime/feed_recovery.py`
   
   **Verification:** Feed recovery works correctly

**Acceptance Criteria:**
- [ ] Single `MarketFeed` owns all feed lifecycle management
- [ ] Feed state machine works correctly
- [ ] Feed health monitoring works correctly
- [ ] Feed recovery works correctly
- [ ] All streaming tests pass
- [ ] No references to deleted modules remain

---

### **Stream 4: Order Layer Consolidation** (2-3 days)
**Agent:** OrderLayerConsolidator  
**Goal:** Replace `SQLiteOrderStore` with event projection from `SQLiteEventStore`

#### Tasks:
1. **Analyze SQLiteOrderStore usage** (Day 1, 4 hours)
   - Document all public methods
   - Identify which methods are used externally
   - Map to event projection equivalents
   
   **Output:** Migration mapping document

2. **Implement order projection from events** (Day 2, 6 hours)
   - Add `get_order(order_id)` to `SQLiteEventStore`
   - Add `get_orders(status)` to `SQLiteEventStore`
   - Add `get_orders(instrument)` to `SQLiteEventStore`
   - Ensure projection is efficient (index events by order_id)
   
   **Verification:** All order queries return correct results

3. **Migrate callers to event projection** (Day 3, 4 hours)
   - Update `runtime/startup.py`
   - Update `execution/engine.py`
   - Update `interfaces/routes/orders.py`
   
   **Verification:** All order operations work correctly

4. **Delete SQLiteOrderStore** (Day 3, 2 hours)
   - Delete `execution/sqlite_store.py`
   - Keep `SQLiteIdempotencyGuard` (move to separate file if needed)
   
   **Verification:** `grep -r "SQLiteOrderStore" --include="*.py"` returns nothing

**Acceptance Criteria:**
- [ ] Single `SQLiteEventStore` is the source of truth for order state
- [ ] Order projection is efficient (< 100ms for 1000 orders)
- [ ] All order tests pass
- [ ] All order API endpoints work
- [ ] No references to deleted module remain

---

### **Stream 5: Test Coverage & Integration** (2-3 days, parallel with Streams 1-4)
**Agent:** TestCoverageEngineer  
**Goal:** Add missing tests for critical paths

#### Tasks:
1. **Add broker fallback tests** (Day 1, 4 hours)
   - Test `MarketWindowLoader` falls back to broker when datalake is empty
   - Test `MarketWindowLoader` uses datalake when available
   - Test `MarketWindowLoader` merges datalake + broker data correctly
   
   **File:** `market_data/tests/test_window_loader_fallback.py`

2. **Add feed recovery integration tests** (Day 1-2, 6 hours)
   - Test feed disconnection detection
   - Test feed reconnection logic
   - Test feed state transitions (READY → DEGRADED → HALTED → READY)
   
   **File:** `runtime/tests/test_feed_recovery.py`

3. **Add event store tamper detection tests** (Day 2, 4 hours)
   - Test hash chain verification detects modified rows
   - Test hash chain verification detects deleted rows
   - Test hash chain verification detects reordered rows
   
   **File:** `execution/tests/test_event_store_tamper.py`

4. **Add end-to-end backtest test** (Day 2-3, 6 hours)
   - Test full backtest flow: Datalake → Loader → Engine → Report
   - Test with real datalake data (use `seed_e2e_datalake.py`)
   
   **File:** `replay/tests/test_e2e_backtest.py`

5. **Add end-to-end live order test** (Day 3, 6 hours)
   - Test full order flow: API → Engine → Paper Broker → Event Store
   - Test order lifecycle: PLACED → FILLED → SETTLED
   
   **File:** `execution/tests/test_e2e_live_order.py`

**Acceptance Criteria:**
- [ ] All new tests pass
- [ ] Test coverage > 80% for critical paths
- [ ] Integration tests cover end-to-end flows
- [ ] No silent failures (all errors are asserted)

---

## Parallel Execution Schedule

### Phase 1: Dead Code Elimination (Days 1-3)
```
Day 1: Stream 1 (DeadCodeHunter) — Delete dead directories and scripts
Day 2: Stream 1 (DeadCodeHunter) — Delete deprecated token manager
Day 3: Stream 1 (DeadCodeHunter) — Delete ParquetBacktestLoader
```

### Phase 2: Consolidation (Days 4-10, parallel)
```
Day 4-7:  Stream 2 (DataLayerConsolidator) — Consolidate data layer
Day 4-7:  Stream 3 (FeedLayerConsolidator) — Consolidate feed layer
Day 4-6:  Stream 4 (OrderLayerConsolidator) — Consolidate order layer
Day 4-6:  Stream 5 (TestCoverageEngineer) — Add missing tests
```

### Phase 3: Integration & Verification (Days 11-13)
```
Day 11: Orchestrator — Integrate all streams, resolve conflicts
Day 12: Orchestrator — Run full test suite, fix failures
Day 13: Orchestrator — Final verification, deployment readiness check
```

---

## Multi-Agent Coordination

### Orchestrator Responsibilities:
1. **Kick off work streams** in parallel using Workflow tool
2. **Monitor progress** via task notifications
3. **Resolve conflicts** when streams touch the same files
4. **Integrate results** after each stream completes
5. **Run verification gates** between phases
6. **Make go/no-go decision** at the end

### Work Stream Agent Responsibilities:
1. **Execute tasks** according to the plan
2. **Verify acceptance criteria** before marking complete
3. **Report blockers** to Orchestrator immediately
4. **Commit changes** with clear commit messages
5. **Update plan** if tasks take longer than expected

### QA Agent Responsibilities:
1. **Review each stream's output** for correctness
2. **Run test suites** after each stream completes
3. **Verify no regressions** were introduced
4. **Sign off** on each phase before proceeding

---

## Risk Mitigation

### Risk 1: Merge Conflicts Between Streams
**Mitigation:** 
- Streams 2, 3, 4 touch different packages (market_data, runtime, execution)
- If conflicts arise, Orchestrator resolves them sequentially

### Risk 2: Breaking Changes During Consolidation
**Mitigation:**
- Each stream runs tests before marking complete
- QA agent verifies no regressions
- Orchestrator runs full test suite after each phase

### Risk 3: Incomplete Migration
**Mitigation:**
- Each task has explicit acceptance criteria
- `grep` verification ensures no dead imports remain
- Test coverage ensures behavior is preserved

### Risk 4: Scope Creep
**Mitigation:**
- Plan is time-boxed (8-13 days)
- Each stream has clear deliverables
- Orchestrator can descope non-critical tasks

---

## Success Metrics

### Quantitative:
- [ ] 25,000+ lines of code deleted
- [ ] 50+ files deleted
- [ ] 6 duplicate modules consolidated to 3
- [ ] Test coverage > 80% for critical paths
- [ ] All tests pass (0 failures)
- [ ] Lint gate passes (0 errors)
- [ ] Complexity gate passes (no new complex functions)

### Qualitative:
- [ ] Single canonical path for each concern (data, feed, order)
- [ ] No shotgun surgery (adding a feature requires < 3 file edits)
- [ ] Clear ownership (each concept has one authoritative owner)
- [ ] Deployable (can be built and deployed to staging)
- [ ] Maintainable (new developer can understand the system in < 1 day)

---

## Tools & Skills

### Workflow Orchestration:
- **Workflow tool** — Parallel execution of work streams
- **Agent tool** — Spawn specialized agents for each stream
- **Monitor tool** — Track progress and detect blockers

### Verification Tools:
- **pytest** — Run test suites
- **ruff** — Lint gate
- **vulture** — Dead code detection
- **grep** — Import verification
- **git** — Version control and diff review

### Skills:
- **ponytail-debt** — Track deliberate shortcuts during cleanup
- **ponytail-audit** — Audit each stream's output
- **ponytail-review** — Review PRs and merge requests

---

## Communication Plan

### Daily Standup (Orchestrator):
- What did each stream complete yesterday?
- What will each stream work on today?
- Are there any blockers?

### Phase Gates (Orchestrator + QA):
- End of Phase 1: Verify dead code elimination
- End of Phase 2: Verify consolidation complete
- End of Phase 3: Verify deployment readiness

### Escalation Path:
1. Work stream agent → Orchestrator (for blockers)
2. Orchestrator → User (for scope changes or go/no-go decisions)

---

## Appendix: File-Level Task Breakdown

### Stream 1: Dead Code Elimination
```
DELETE:
  poc/                                      # 30 files
  apps/                                     # 1 file
  graphify-out/                             # Generated
  trading/scripts/backfill_2025.py
  trading/scripts/backtest_datalake.py
  trading/scripts/sync_today.py
  trading/scripts/topup_gaps.py

MIGRATE THEN DELETE:
  brokers/common/token_lifecycle.py          # Remove DurableTokenManager
  brokers/common/resilience.py               # Remove legacy re-exports
  market_data/backtest_loader.py            # Migrate to MarketWindowLoader
```

### Stream 2: Data Layer Consolidation
```
MERGE:
  market_data/market_provider.py → market_data/window_loader.py

DELETE:
  market_data/market_provider.py
  market_data/simple_sync.py

UPDATE IMPORTS:
  interfaces/routes/chart.py
  strategy/extensions/strategies/multi_symbol_sma_cross.py
  runtime/session_impl.py
  market_data/__init__.py
```

### Stream 3: Feed Layer Consolidation
```
MERGE:
  runtime/feed_supervisor.py → runtime/market_feed.py
  runtime/feed_monitor.py → runtime/market_feed.py
  runtime/feed_recovery.py → runtime/market_feed.py

DELETE:
  runtime/feed_supervisor.py
  runtime/feed_monitor.py
  runtime/feed_recovery.py

UPDATE IMPORTS:
  runtime/session_impl.py
  runtime/startup.py
```

### Stream 4: Order Layer Consolidation
```
IMPLEMENT:
  execution/sqlite_event_store.py            # Add projection methods

MIGRATE:
  runtime/startup.py                        # Use event projection
  execution/engine.py                       # Use event projection
  interfaces/routes/orders.py               # Use event projection

DELETE:
  execution/sqlite_store.py                 # Keep SQLiteIdempotencyGuard
```

### Stream 5: Test Coverage
```
ADD:
  market_data/tests/test_window_loader_fallback.py
  runtime/tests/test_feed_recovery.py
  execution/tests/test_event_store_tamper.py
  replay/tests/test_e2e_backtest.py
  execution/tests/test_e2e_live_order.py
```

---

## Sign-Off

**Orchestrator:** Main Agent  
**Date:** 2026-10-01  
**Status:** Ready for execution  
**Next Step:** Kick off Stream 1 (Dead Code Elimination)