# Task 1 Fix Report — feed recovery path review findings

Branch: `feat/tier1-3-remediation` (from implementer commit `d56ab49`).

## What I fixed

### Critical — a dead order socket is no longer recovered to READY

`FeedStaleMonitor.poll_once()` used to fire recovery on *any* DEGRADED state at
poll entry. `OrderStreamWatchdog.check()` marks the same supervisor DEGRADED, and
its `_reported` latch only clears on a frame — so a dead order socket was flipped
to READY one poll later and stayed there.

The gate now requires the DEGRADED cause the quote path owns:

```python
was_degraded            # supervisor was DEGRADED before this poll
and bool(stale)         # this poll's check_stale() returned StaleFeed events
and not order_stream_stale
and feed.active
and self._feed_recovery is not None
```

`check_stale()` is the only writer of a quote-side DEGRADED; the order watchdog
writes the same supervisor through a different door. Requiring a fresh `StaleFeed`
means an order-stream DEGRADED is never handed to the coordinator.

### Important 1 — the monitor no longer bypasses the feed's in-flight guard

The monitor called `coordinator.recover()` directly, so a poll-triggered recovery
could interleave with a reconnect or integrity-violation recovery. Recovery is now
dispatched through `MarketFeed.request_recovery()` → `_trigger_recovery()`, which
shares `_claim_recovery()`'s in-flight flag **and** runs the work on a daemon
thread. `_trigger_recovery()` now returns whether it claimed the slot.

### Important 2 — a deliberate stop is not converted into sticky HALTED

`MarketFeed.stop()`/`unsubscribe` set DEGRADED via `disconnected()`. Recovering the
now-empty feed fails closed to HALTED. The gate checks `feed.active` before
recovering, so teardown is not recovered into a terminal HALTED.

### Important 3 — recovery no longer blocks the poll thread

Because dispatch goes through `request_recovery()`, the history fetch runs on the
`feed-auto-recovery` daemon thread. The order watchdog and cadence checks that run
later in the same `poll_once()` are no longer delayed by datalake I/O.

### Important 4 — tests use real components

The rewritten `trading/tests/runtime/test_feed_recovery_path.py` uses a real
`MarketFeed` on a real `PaperBroker`, a real `ReactiveBus`, a real
`FeedSupervisor`, a real `OrderStreamWatchdog`, and a real
`ParquetHistoryProvider` over a real `ParquetStorage` temp lake. No `_FakeBroker`,
no `_Provider`, no `MagicMock` broker. The boot-wiring control calls the real
`_compose_live_feed` with a real `PaperBroker` and a real `MetricsRegistry`.

### Minor 5 — warning eliminated

The `StarletteDeprecationWarning` was emitted while `trading/tests/conftest.py`
imported `starlette.testclient`, *before* any `filterwarnings` ini entry was
active. `trading/pyproject.toml` could not silence it. The conftest now filters it
immediately ahead of the import that raises it.

### Minor 6 — docstrings updated

`session_impl.py` now describes the three recovery triggers (reconnect, integrity
violation, owned poll) sharing `MarketFeed`'s in-flight guard, rather than claiming
recovery is never automatic.

### Minor 7 — typing tightened

`feed_recovery` is annotated `FeedRecoveryCoordinator | None` via a `TYPE_CHECKING`
import, replacing `Any | None`.

### Extra — co-occurring-cause fail-open (found in self-review)

The `stale` gate alone still admitted a recovery when the quote tape was stale
**and** the order socket was dead: the quote-side `StaleFeed` authorized READY
while the order socket was silent, and the watchdog's one-shot latch would never
re-degrade it. `OrderStreamWatchdog` now exposes `is_stale()` (the single staleness
predicate, shared with `check()`), and the recovery gate refuses while the order
stream is provably stale. `check()` keeps its exact behaviour and log.

## What I tested and test results

```
.venv/bin/python -m pytest trading/tests/runtime/test_feed_recovery_path.py -v
8 passed

.venv/bin/python -m pytest trading/tests/runtime runtime/tests -q
694 passed

.venv/bin/python -m pytest trading/tests/execution/test_fail_closed_feed.py \
    trading/tests/interface/test_ws_feed_status.py \
    trading/tests/sdk/test_session_readiness.py \
    trading/tests/runtime/test_boot_cold_start_wiring.py -q
32 passed

.venv/bin/python -m ruff check <5 changed files>
All checks passed!

.venv/bin/lint-imports
Contracts: 13 kept, 0 broken.
```

The 8 tests:

1. `test_degraded_feed_triggers_recovery_on_next_poll` — poll 1 demotes, poll 2
   recovers to READY (real lake).
2. `test_failed_recovery_halts_feed` — history proves a hole with allowance 0 →
   HALTED (real lake).
3. `test_no_recovery_coordinator_leaves_degraded` — backward compatibility.
4. `test_order_stream_degraded_is_not_recovered` — **Critical control**: an
   order-stream DEGRADED stays DEGRADED; a later quote staleness recovers.
5. `test_order_dead_with_stale_tape_is_not_recovered` — **co-occurring control**.
6. `test_stopped_feed_is_not_recovered_into_halted` — **stop control**.
7. `test_request_recovery_returns_false_while_one_is_in_flight` — guard.
8. `test_monitor_trigger_is_refused_while_the_feed_guard_is_held` — **bypass
   control**.
9. `test_live_compose_wires_the_coordinator_into_the_monitor` — wiring.

### Mutation verification (in place, per the repo's worktree lesson)

Each negative control was re-run against a deliberately mutated source, then the
source was restored:

| Control | Mutation | Result |
|---|---|---|
| order-stream DEGRADED | drop the `stale` conjunct | FAILS (as required) |
| stopped feed | restore the original d56ab49 gate | FAILS (as required) |
| co-occurring cause | drop the `not order_stream_stale` conjunct | FAILS (as required) |
| in-flight guard | call `coordinator.recover()` directly | FAILS (as required) |

## Files changed

| File | Change |
|------|--------|
| `runtime/src/tradex_runtime/feed_monitor.py` | quote-owned recovery gate; `feed.active` and order-stale checks; dispatch via `request_recovery()`; typed seam; `OrderStreamWatchdog.is_stale()` |
| `runtime/src/tradex_runtime/market_feed.py` | `request_recovery()`; `_trigger_recovery()` returns claim result |
| `runtime/src/tradex_runtime/session_impl.py` | docstring reflects poll-triggered recovery |
| `trading/tests/runtime/test_feed_recovery_path.py` | rewritten with real components + negative controls |
| `trading/tests/conftest.py` | silence the starlette import warning at its source |

## Self-review findings

- **Residual ceiling (fail-closed, documented).** Requiring a fresh `StaleFeed`
  means a feed whose ticks resume *between* the demoting poll and the next poll is
  no longer auto-recovered: `check_stale()` returns nothing, so the gate stays
  shut. This is the trade-off the review's fix direction prescribes (recover only
  while the quote path reports staleness), and it is the safe direction — orders
  stay refused, never wrongly admitted. The common path (stale persists across the
  poll boundary) recovers. A cause tag on the supervisor would close this without
  reintroducing the order-socket fail-open; that is a larger change and was not
  requested.
- **Order-watchdog latch is still one-shot.** `_reported` clears only on a frame.
  The co-occurring control above closes the recovery bypass, but the underlying
  latch design (a frame is the only proof of life) is unchanged and is the
  watchdog's intended contract.
- **Verified** `_compose_live_feed` needed no change: it already wires the
  coordinator into both the monitor and the feed, so the daemon-thread routing is
  live on the production path.
- **Verified** `runtime/tests` (the whole runtime suite) is green, and
  import-linter keeps all 13 contracts — no new cross-package edge.
- **Pre-existing, unrelated:** mypy reports one `arg-type` at
  `market_feed.py:223` (unchanged line), and the working tree carries unrelated
  uncommitted edits to `sync_service.py` / `backfill_parquet.py` from another lane.
  Neither is part of this change and neither is committed here.
