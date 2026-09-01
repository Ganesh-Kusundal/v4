# Code Review: 0d4454a..d79def1 (branch `refactor/execution-state`)

Range: 4 commits (1f39528 projector ordering/dup-retry/paper tests, e491c3d trip_kill_switch, 0614c3c screener bias fixes, d79def1 thread-safe EventStore), 6 files, +987/−74, implementing the event-sourcing + actor-model execution pipeline per `docs/superpowers/plans/2026-09-01-event-sourcing-actor-redesign.md`.

## Strengths

- Projector-before-data-source ordering fix (session.py:296-311) is correct and well-commented; the paper instant-fill flow is verified by tests (order returns FILLED with correct quantity on the place_order call).
- Duplicate-retry early return (session.py:283-290) correctly prevents read-model corruption and double fill tracking in the common path.
- Kill switch is fail-closed and event-sourced: KillSwitchTripped is persisted, state is rebuilt on replay (actor `_apply_event`), and non-terminal orders are swept to CANCELLED; tests cover both rejection of new orders and survival across restart.
- EventStore thread-safety rework is sound: lock held on every access path including `close()`, `check_same_thread=False`, plus a barrier-based 4-thread stress test asserting gapless sequences and unique event IDs.
- Screener bias fixes are correct: strict `< 09:45` time filters eliminate the 59-second look-ahead from open-time-stamped minute bars, and the signal 5m bucket is the complete 09:40-09:44 bucket (no partial-bar volume deflation); conventions are documented in the module docstring.
- Persist-first-then-mutate discipline is maintained in the actor; tests use real SQLite with no mocks (1893 passed).

## Issues

1. **Critical** — trading/src/tradex_trading/events/data_source.py:86-165 (with session.py:444-467): the live fill path is broken end-to-end. `BrokerDataSource` never invokes the `on_fill` callback, and its FillMatcher path (`process_update` → `CommandProcessor` → Actor) persists OrderFilled/PositionUpdated to the store but bypasses the session's projectors entirely. Repro executed: live session, place order, feed a fill via `session.fill_matcher` → `get_orders()` still shows ACK with filled_quantity 0 and `get_positions()` is empty; read models are only correct after a restart/rebuild. The `_on_data_source_fill` docstring claims live behavior that is unimplemented, and `process_broker_update` has zero callers. Suggested fix: route FillMatcher-emitted fills through the session's projector-update path (pass the `on_fill` callback into FillMatcher/BrokerDataSource, or forward `result.events` to the session), plus a live-path integration test.

2. **Important** — trading/src/tradex_trading/events/session.py:262-281: duplicate retries are not idempotent when the pre-placement risk gate fails on retry. Repro executed: `max_orders_per_minute=2`; place A, B, C, then retry A → returns `success=False`, error "Risk check failed: Rate limit exceeded: 3 orders in last minute (max 2)", `is_duplicate=False`, instead of the cached successful result. Violates the "same correlation_id returns cached result without re-applying" acceptance criterion. Suggested fix: check the processor's idempotency cache before running the risk gate (reorder risk-after-duplicate-detection).

3. **Important** — trading/src/tradex_trading/events/processor.py:38-89 + actor.py: only the EventStore was made thread-safe; `CommandProcessor._idempotency` and OrderBookActor's dicts are mutated with no synchronization, while the stated reason for the store change is that broker callbacks arrive on a WebSocket thread. Two threads interleaving `processor.process`/`actor.handle` can race the check-then-cache idempotency insert and double-apply fill deltas. The store fix is necessary but not sufficient; this blocks live wiring. Suggested fix: serialize all command processing (single command queue/actor mailbox, or a processor-level lock).

4. **Important** — trading/src/tradex_trading/events/session.py:338-364: `trip_kill_switch` is local-only. Open orders are marked CANCELLED in the event log but nothing cancels them at the broker (adapter unimplemented). In live mode orders would remain working at the broker while read models show CANCELLED. Suggested fix: document the limitation explicitly or integrate broker-side cancel before live use.

5. **Important** — trading/src/tradex_trading/events/risk_engine.py:198-216 as newly wired by session.py:434-438: the rate limit is off by one. The candidate order is not counted against the limit, so `max_orders_per_minute=N` admits N+1 orders per window. Repro executed: `max_orders_per_minute=2`; placing three same-minute orders A, B, C all succeeded, then the duplicate retry of A was rejected with "Rate limit exceeded: 3 orders in last minute (max 2)" — three orders passed a max-2 limit. Suggested fix: count the candidate as well (`count + 1 <= max`) in `check_rate_limit`. The engine predates this commit range, but the new session wiring in this range activates it.

6. **Minor** — trading/src/tradex_trading/events/session.py:184-185: `_order_timestamps` is not derived from the event log, so the rate-limit window resets on restart; it also grows unboundedly and is scanned O(n) per order. Suggested fix: rebuild from the event log on recovery and prune/cap the window.

7. **Minor** — trading/src/tradex_trading/events/session.py:265-273: risk-rejected orders leave no audit event, inconsistent with kill-switch rejections (the actor persists OrderRejected). Suggested fix: persist a rejection event for the audit trail.

8. **Minor** — trading/src/tradex_trading/events/session.py:483-499, 552-558: return-type erasure (`get_orders`/`get_positions` now return bare `list`, were `list[OrderView]`/`list[PositionView]`) and an untyped `hasattr`-based `fill_matcher` property. Suggested fix: restore typed signatures and a typed optional attribute.

9. **Minor** — trading/src/tradex_trading/events/session.py:281-315: in paper mode the place_order `CommandResult.events` contains only OrderPlaced even though OrderFilled/PositionUpdated occurred synchronously; callers inspecting events miss the fill. Suggested fix: include all events produced by the command in the result.

10. **Minor** — trading/tests/events/test_session.py:298-326: `test_get_positions_after_fill` no longer exercises `apply_fill` — the paper instant fill already created the position, so the explicit `apply_fill` is a delta-0 no-op and would pass even if `apply_fill` were broken. Suggested fix: use backtest mode or a larger cumulative quantity so the matcher path produces a real position delta.

11. **Minor** — services/duckdb-analytics/research/run_6criteria_aug31.py:142-149: b5 restricts all days to pre-09:45 buckets, so MA5/vol_ma20/hi13_prev span multiple mornings (the 13-bar high covers ~2+ mornings, not 65 contiguous minutes). Suggested fix: confirm this matches the intended criteria semantics or restrict the lookback to the current day.

12. **Minor** — services/duckdb-analytics/research/run_6criteria_aug31.py:22, 42-241: unused `import sys`; main/diag/partial SQL are three near-duplicate copies (drift risk); DAY/AS_OF hardcoded (acceptable for a one-off). Suggested fix: drop the unused import and derive the SQL variants from a single parameterized query.

13. **Minor** — trading/src/tradex_trading/events/store.py:114-124: `read_after` holds the lock while materializing all session events and filters them in Python. Suggested fix: add a SQL-level per-session sequence filter when scale matters.

## Assessment

**Needs fixes** — the Critical live-fill routing must be fixed before wiring any live broker adapter, and the duplicate-retry idempotency and command-pipeline thread-safety issues should land first. Paper/backtest/replay paths and the screener are in good shape; the full test suite (1893) passes at HEAD.
