# Operational minimum: dependency graph, parallel capacity, and the test reorganisation

Written 2026-09-28 against `5d245bb` (clean, 4980 passed). Source of scope is
`docs/specs/0002-production-readiness-programme/index.md`; this plan pulls the
*operational minimum* forward, ahead of the wave order, because the gate for
attended operation is stricter than the gate for a research programme.

## The operational minimum

Eight lanes, chosen by "what stops a person from safely leaving this running on
one machine", not by lane number.

| Lane | What | Depends on |
|---|---|---|
| 2h | Cash/position/fee under one lock (AC-21) | — |
| 2e | `GET /api/charts/safety` (AC-17) | — |
| 5a | Metric labels, real `p95` rows (AC-17) | — |
| 3a | WS heartbeat + frame age watchdog (AC-12) | — |
| 3b | Re-read after reconnect (AC-12) | 3a (same feed watchdog pattern) |
| 5b | Alert rules with explicit numbers (AC-17) | 5a |
| 5c | Structured logging, wired (AC-17) | 5a (shared registry) |
| 5d | Secret permissions, fail closed (AC-17) | — |

Strongly recommended, not minimum: 3f (parquet quarantine), AC-22 the public
bus bypass (3h), 5f (runbooks).

Deliberately excluded: all of Wave 4 (package consolidation: churn, no
capability, forbidden to run concurrently by the spec), 3g/3h/3i (performance),
3c/3d (data handling beyond the socket level), 5e (adopting venue positions).

## Dependency graph

```
   2h (cash atomicity) ──┐
                          ├──> [merge 1] ──> [full suite] ──> gate
   2e (safety route) ─────┤
   5a (metric labels) ────┘

   3a (feed watchdog) ──> 3b (reconnect re-read) ──> [merge 2] ──> [full suite]
                                                                    │
   5a ──> 5b (alerts) ──────────────────────────────────────────────┤
   5a ──> 5c (logging) ─────────────────────────────────────────────┤
   5d (secrets) ────────────────────────────────────────────────────┘
```

Two independent clusters: the money-path cluster (2h, 2e, 5a) and the
operational cluster (3a, 3b, 5b, 5c, 5d). Within the operational cluster 5a is
a hard predecessor of 5b and 5c. The clusters share no source file except
`runtime/startup.py`, which is why they merge sequentially and the lanes within
a cluster run in parallel worktrees.

## Parallel capacity

- 10 CPUs, 64 GB RAM, load average was 14.9 at measurement time (already above
  CPU count; something else was running).
- A full suite run is 4 to 5 minutes wall clock. Running 8 agents each running
  the full suite would oversubscribe the machine and make every run slower, not
  faster.
- Therefore: **at most 3 concurrent implementation agents**, each verified
  against the *targeted* suites for its blast radius, not the full suite. The
  full suite runs once per merge cluster, centrally, by the controller.

Each agent gets its own git worktree. The controller merges. No agent merges,
no agent runs destructive git.

## The test reorganisation (requested, and overdue)

Current distribution is the problem: 300 test files sit in the orphan
`trading/tests/`, while the modules that own the code have almost none
(`strategy` 1, `replay` 1, `runtime` 2). The unit tests do exist; they are in
the wrong place, so "run this module's tests" is not a command you can give.

Target: every test file lives under the `<module>/tests/` that owns the code it
tests. Project level keeps only cross-module integration and e2e.

Proposed mapping (each orphan area to its owning module):

| Orphan area | Files | Owner |
|---|---|---|
| `trading/tests/execution` | 93 | `execution/tests` |
| `trading/tests/runtime` | 47 | `runtime/tests` |
| `trading/tests/interface` | 37 | `interfaces/tests` |
| `trading/tests/parity` | 21 | project level (cross-module by nature) |
| `trading/tests/analytics` | 19 | `analytics/tests` |
| `trading/tests/replay` | 16 | `replay/tests` |
| `trading/tests/strategy` | 15 | `strategy/tests` |
| `trading/tests/contracts` | 13 | project level |
| `trading/tests/datalake` | 13 | `market_data/tests` |
| `trading/tests/reactive` | 8 | `reactive/tests` |
| `trading/tests/scripts` | 6 | project level |
| `trading/tests/sdk` | 6 | `runtime/tests` |
| `trading/tests/integration` | 3 | project level |
| `trading/tests/research` | 2 | `research/tests` |
| `trading/tests/application` | 1 | `application/tests` |

This is a large mechanical move and it is exactly the class of change that
must NOT run concurrently with the functional lanes: it touches every test path
that the functional agents are editing, so a collision is guaranteed, not
likely.

## Sequencing decision

The reorganisation is sequenced **after** the eight functional lanes, for one
reason: every functional lane adds and edits tests, and re-pointing 300 files
underneath moving agents would burn the whole budget on merge conflicts with no
capability gained. Once the lanes are merged and the tree is green, the move is
a single self-contained change that can be verified in one pass.

## Findings that did not execute

- Load average 14.9 on a 10-CPU machine at measurement time means the "3
  concurrent agents" ceiling is optimistic if anything else is running. If
  timings regress, drop to 2.
- `services/duckdb-analytics` and `config` are workspace members with their own
  test suites that were, until recently, not collected by a root run. They must
  keep their own paths in the reorganisation; they are not part of `trading/`.
- Part of the measured load was pytest processes belonging to an UNRELATED
  project on the same machine, not this repo. The parallel ceiling is set by
  this repo's own runs, not by the host's total load.

## Outcome, recorded 2026-09-28

All eight lanes are merged on `wave0-1-recovered`. Verification, in the order
the clusters were merged, each against the full root suite:

| Cluster | Lanes | Suite result |
|---|---|---|
| money path | 2h, 2e, 5a | 5055 passed, 0 failed |
| feed | 3a, 3b | 5091 passed, 0 failed |
| observability | 5b, 5c | 5114 passed, 0 failed |
| final (incl. 5d) | all eight | see the commit log |

Ruff ratchet 0 throughout. The ratchet caught three unsorted imports in the
new 5d test during development, which is the gate working as intended.

### What each lane actually changed, against the plan

- **2h** `CashLedger` had no lock and a fill moved money in three separate
  critical sections. Now one `apply_fill` holds proof, position booking,
  notional and fee under a single lock; both engine doors route through it.
- **2e** `GET /api/charts/safety` with severity DERIVED, not read. Four pieces
  of state it needed did not exist (no `Order` timestamp, no retained drift, no
  `session.event_store` or `session.validation_report_store` accessors); it
  degrades to nulls rather than lying, and the missing accessors are a
  follow-up.
- **5a** `del labels` removed; metrics indexed by a sorted label tuple; real
  histogram buckets so `p95`/`p99` are computable. It also corrected an
  existing test that asserted the old, invalid `summary` output.
- **3a** heartbeat params on both brokers, and the order stream finally under
  a frame-age watchdog. The order socket previously had no liveness check at
  all, which is the case that leaves a resting order believed live.
- **3b** a reconnect now re-reads orders and positions from the venue instead
  of resuming blind, and the backoff escalates to a 300s ceiling and never
  gives up. It found and fixed an `OverflowError` that killed the reconnect
  thread, and a retry-thread leak that stretched the suite from 39s to 5min.
- **5b** 22 alert rules with explicit thresholds, and a test that cross-checks
  every referenced metric against the metric names the code actually emits. The
  cross-check found that every dotted spec name is exposed underscored, so
  alerting on the spec spelling would have matched nothing.
- **5c** `setup_structured_logging` was dead code with no production call site;
  it is now called from `boot`. The existing `CorrelationId` is reused as the
  trace id rather than a second id, and it is emitted at four points from tick
  to venue. A latent crash in `StructuredFormatter` on unstamped records was
  fixed on the way.
- **5d** the token layer already wrote secrets privately; the uncovered half
  was the operator-created `.env.local`. Boot now refuses a credential file
  readable beyond its owner, and a missing file still passes so paper boots.

### The one finding worth carrying forward

The 2h concurrency test PASSED with the lock removed. CPython's GIL makes a
pure-Python check-then-debit window too narrow to hit, so the test was proving
nothing. The suite now CONTROLS the window, forcing a real yield between the
proof and the debit, so a lockless implementation double-spends deterministically
(4 accepted against a limit of 5) and the test fails.

This is the fourth instance of the pattern named at the top of this document: a
component built, documented and tested while disconnected from the thing it
was supposed to protect. A green test suite is not evidence of a working
system; it is evidence that the assertions you wrote are the ones being
exercised.
