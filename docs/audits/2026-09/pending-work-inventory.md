# Pending work inventory, verified against the working tree

**Date**: 2026-09-28
**Closure run complete.** Verified against `e6afb7e`: **5389 passed, 2 skipped, 10 xfailed**
in 3m38s with **no deselection**, `ruff check .` clean. `trading/tests/interface/test_ws_bars_replay.py`
is fixed, not pre-existing — see the correction below.
**Closure log at the end.** The findings below are as originally established.

> **Correction, `e6afb7e`.** The claim that the 18 replay failures were pre-existing was wrong.
> The reproduction behind it ran pytest from a pristine worktree, but the venv is an *editable*
> install whose `.pth` files hold absolute paths into the main checkout, so that run still imported
> the modified `stream.py`. This is the same editable-venv trap recorded as lesson 1 below, and the
> reproduction fell into it. A bisect that swaps source files *in place* gives the true answer:
> the failures begin at `ea5c703` (AC-15), which added `metrics=` to two `ParquetStorage`
> constructions in `stream.py`. Seven in-test fakes still took one positional argument, so the route
> raised `TypeError` before sending `replay_started`. The fakes now accept `**_kwargs`.
> A second, unrelated flake is also fixed: lane 3d's composition test pinned a real wall clock
> against a session-gated monitor and so failed every evening outside 09:15-15:30 IST.
> **38 tests were being hidden by the wrong verdict.** "5351 passed with 38 deselected" is now
> "5389 passed with nothing deselected".
>
> **Correction, `1792f4d`.** Two lanes were reported merged during the closure run and were not.
> A follow-up audit against the branches found the 3d per-symbol cadence lane had never reached
> `HEAD` at all, and the 3g default-path controls were dropped from the merge. Both are fixed here
> and re-verified. The lesson is in the closure log: a merge that reported a file list is not
> evidence the files are in the tree, and `git log <branch>..HEAD` is the check that settles it.
**Method**: seven verification agents worked the spec lane tables against the tree. Every disputed
claim was then adjudicated by direct experiment (mutation testing, live probes) rather than accepted
from a commit message or an agent summary.

**Verdict vocabulary**

- **DONE** — implemented, wired to a real call site, and a negative control catches the break.
- **PARTIAL** — some of the lane landed; either a sub-item is missing or its negative control is vacuous.
- **NOT STARTED** — no work exists toward it.
- **UNVERIFIED** — no evidence either way was obtained.

## Wave 2 — paper path and promotion gate

| Lane | Verdict | Evidence |
|---|---|---|
| 2a lifecycle | **DONE** | `strategy/core/engine.py:507-556`, 3/3 mutations caught |
| 2b strategy_id attribution | **DONE** | 3 layers: origination `engine.py:233`, fill `fill_sources.py:101`, position. P&L `trade_metrics.py:250-313` is real, not a field |
| 2c per-strategy risk budgets | **DONE** | stale-mark NC at `test_risk_budget_and_marks.py:169` caught |
| 2d selectable fill path | **DONE** | `startup.py:276` switch caught; wrong-constant NC caught |
| 2e safety route | **DONE** | `safety.py:92` derived severity, `:123` boot refusal, both caught |
| **2f order gate** | **PARTIAL** | Control exists, binding untested. See F-1 |
| 2g tape recorder | **DONE** | env gate + ctor bypass both caught, attached at `market_feed.py:120` |
| **2h cash atomicity** | **PARTIAL** | Lock is real, but the lane's negative control is vacuous. See F-2 |

## Wave 3 — feed correctness

| Lane | Verdict | Evidence |
|---|---|---|
| 3a heartbeat + watchdog | **DONE** | `ff9c576` (merge of `0cbf228`), 20 tests pass |
| 3b reconnect re-read | **DONE** | `1e617c0` (merge of `627f194`), 349-line reread suite. Fixed an `OverflowError` that killed the reconnect thread and a retry leak |
| 3c late-tick revision | **NOT STARTED** | `bar_aggregator.py:186` bare early-return; no 60 s window, no metric |
| 3d cadence liveness | **NOT STARTED** | `market_feed.py:602` is threshold-on-last-tick (30 s), not 3-min cadence, not session-aware |
| 3e parquet pushdown | **NOT STARTED** | `parquet_storage.py:286` no `filters=`; `use_dictionary=False` at `:160` |
| 3f corrupt quarantine | **NOT STARTED** | no `data/quarantine/`, no `datalake_quarantine_total`, no `ArrowInvalid` handling; 3 unguarded reads |
| 3g indicator tail cache | **PARTIAL, wrong scope** | cache is live (`stream_indicators.py:85`) but per-connection (`stream.py:598`), which is what the lane removes |
| 3h bus bypass (AC-22) | **NOT STARTED** | See F-3 |
| 3i scanner limits | **PARTIAL 1/4** | `max_bars` done (`scanner.py:41`); context, rate limit, cache absent |

## Wave 4 — package consolidation and event store

The spec's premise holds: the earlier package extraction is done (`trading` is no longer a member).
None of the rewritten consolidation lanes have been touched.

| Lane | Verdict | Evidence |
|---|---|---|
| 4a target `src/` trees | **NOT STARTED** | `pyproject.toml:138-163` still 17 members; zero of the 5 target trees exist |
| 4b test move | **NOT STARTED** | `trading/tests/` intact: 318 tracked `.py` + 147 assets; root `tests/` still 11 |
| 4c event store hardening | **NOT STARTED (0 of 5)** | See F-4 |
| 4d dead-code deletion | **~0%** | `operations/`, `persistence/`, `http_response.py`, `poc/` (20 tracked), all 6 frontend files (1,112 lines) still present |
| 4e orphan tree owners | **NOT STARTED** | `trading/scripts/` (14) coexists with root `scripts/` (13) |
| 4f `CLAUDE.md` | **UNVERIFIED** | not assessed |

## Wave 5 — operational floor

| Lane | Verdict | Evidence |
|---|---|---|
| 5a honest metrics | **DONE** | label-indexed series, 15 default buckets, `rejection_reason` rows on the live path |
| 5b alert rules | **DONE** | `d77b018` / `46c7906`. 22 alerts, all with explicit numbers; p50 0.05, p95 0.25, p99 0.75 on both latency metrics. `TradeXDatalakeQuarantine` staged behind `metric_pending: ac-15` so it is inert rather than silently green. `observability/tests/test_alert_rules.py` parses the file and refuses any expression without a numeric comparison or any metric the tree does not emit |
| 5c structured logging + trace | **DONE** | `191a21e` / `1f11b0d`. `observability/tracing.py:77` carries a correlation-derived id rather than inventing a second one |
| 5d secret permissions | **DONE** | `2515821`. Wired at `startup.py:244` in the boot path, before broker construction. A missing file passes; a world-readable one refuses boot, and it never silently chmods |
| 5e reconciliation monitor | **PARTIAL** | `safety.py` is drift **reporting** only; nothing adopts venue positions |
| 5f deploy + runbooks | **PARTIAL** | 7 runbooks exist and are substantive; probe block still missing though `/health/live` and `/health/ready` exist as ready-made targets |

## AC-4 — point-in-time universes

The guard is **armed and fails closed**, not warn-and-continue. `assert_historical_universe` raises
`PitViolation` on both halves, verified by direct probe. But:

- **F-5** — the entry half is a tautology (below).
- `nse_delistings.csv` is header-only, 0 rows.
- Universe CSVs carry a `membership_asof` column, but every row has **one** snapshot date
  (`universe_snapshot()` at `backtest_loader.py:90` requires exactly one distinct value and raises
  otherwise), so per-name membership history is structurally impossible today.
- 9 of 15 PIT guard tests acknowledge a limit (`TestLoaderReportsWhatTheCheckCouldNotEstablish`).
  Every raise-path test monkeypatches `load_delistings`, so the guard's behaviour against the
  shipped 0-row file is never tested.

## Findings that matter more than the counts

### A methodological correction, recorded because it nearly produced a false PASS

The shared venv is installed **editable** and resolves every `tradex_*` import to the **main
checkout**, not to whichever worktree the interpreter was launched from. A source mutation applied
inside a worktree is therefore silently never exercised by a test run from that worktree. This
produced a false pass during the closure work: the new 2h control appeared to pass with the lock
removed, which would have been recorded as a failed fix.

The correct procedure, now used everywhere:

```
cd <main checkout>
<venv>/bin/python -c "import inspect, <module> as m; print('MUTATED' if 'MARKER' in inspect.getsource(m) else 'NOT MUTATED')"
```

Only trust a mutation result once that prints `MUTATED`. Every mutation result in the closure log
below was confirmed this way.

### F-1 — 2f: the control that stops all order flow has no test

`startup.py:455` binds `live_orders_enabled` to the RiskManager master gate, and `risk.py:424`
refuses every submit. The control works. But mutating that binding line leaves **453 runtime tests
and 124 gate-focused tests green**. The spec's named negative control ("booting with the gate flag
false refuses every submit") does not exist: `test_boot_requires_risk_limits.py:220` writes the
private flag then calls the closure directly and never submits an order. A probe booting paper with
the flag False got `FILLED` back.

The closure the spec names, `_make_order_gate()`, is still not an enforcement point. Its only call
sites are construction, a property, and a boot-time self-check.

### F-2 — 2h: the lock is real, the test is not load-bearing

**I got this wrong earlier and a verification agent proved it.** My earlier mutation replaced
`self._lock` with a null context at construction, which broke the lock object everywhere including
inside the test's own monkeypatched copy. The targeted mutation is to keep the lock object and
neuter it *only inside `apply_fill`*. Under that mutation:

```
test_cash_concurrency_atomicity.py  5 passed
four other cash suites             23 passed
entire execution tree            781 passed
```

The cause is `test_cash_concurrency_atomicity.py:88-107`, which monkeypatches `apply_fill` with its
own re-implementation that keeps `with self._lock`. The test exercises the test's lock, never
production's. Production *is* correct (`cash_ledger.py:186` holds the lock across all four steps,
confirmed by an independent 16-thread barrier probe), but the lane's headline negative control
cannot fail. Working tree restored clean after both experiments.

### F-3 — AC-22: the bypass is real but narrower than the spec states

`PlaceOrderCommand` is still public (`bus.py:55`), still subscribed (`engine.py:307`), still
published by strategies and replay. But the bus path converges on the same `_run_pipeline` as
`submit()`, so engine-level risk, kill-switch, feed and idempotency gates **do** run. What a bus
publisher actually skips is the two **strategy-layer** gates: `PositionSizer` (`engine.py:415`) and
the protective pair (`brackets.py:115`). That is a real hole, sized to sizing and bracket bypass,
not to the whole Flow C gate order.

### F-4 — 4c: event store has none of the five required properties

1. `stream_id` is not account-scoped. `_infer_stream` returns only `"orders"` or `"system"`.
2. `schema_version` handling is the **inverse** of the requirement: `_decode` *raises* on any version
   other than 1, so a version bump invalidates all history rather than decoding through a registry.
3. No `prev_hash` chain. Zero hits repo-wide. Replay orders by `sequence` only.
4. No `Snapshot` type, no snapshot table, nothing written on `READY` or every 10,000 events.
5. The dedup index is keyed on `correlation_id` alone, with no account dimension.

### F-5 — AC-4: the entry half of the PIT guard can never fire

`backtest_loader.py:295` resolves instruments from `load_universe(universe)`, then `:381` builds the
manifest from `universe_snapshot(universe)` — the same source. The entry half compares a set with
itself, so a look-ahead symbol cannot be caught through the loader. The exit half checks a 0-row
ledger. Both halves pass vacuously, which is exactly the failure the spec's cross-workstream
contract forbids. The guard *function* is correct when called with adversarial input; the
*call site* is what neuters it.

Also: `backtest_loader.py:41` claims `point_in_time_report` is shared with
`trading/scripts/backtest_datalake.py` so the two surfaces cannot drift. That script has zero
references to the guard. The claimed sharing does not exist.

### F-6 — AC-4's blocker is solvable, and cheaper than assumed

The spec states "a vendor does not serve it for past dates." NSE publishes its own delisted-companies
list. Downloaded and parsed during this audit:

```
https://nsearchives.nseindia.com/web/sites/default/files/inline-files/List%20of%20delisted%20Companies.xlsx
sheet "delisted": 438 data rows
columns: Symbol, ISIN, Company Name, Board, Delisted Date, Type of Delisting
438 of 438 rows carry a usable date, spanning 2002-04-15 to 2024-10-17
```

That fills the `symbol,delist_date,series` schema directly. Estimated effort: 1 to 2 hours.

**One negative result that changes the priority.** Zero of today's Nifty 500 members appear on that
delisted list, by ISIN or symbol, across all four universes. Populating the ledger will not change
any current backtest. It is insurance for future snapshots, not a fix for a live bias. Index
membership *history* is the genuinely hard half, and
`github.com/BKKB20/nse-index-history` is a 1-star single-author project with self-declared OCR and
pre-2010 gaps — a bootstrap to verify, not a source to trust.

## Summary

- Wave 0, Wave 1: implemented. AC-3, AC-5, AC-6, AC-16 verified live in the tree; AC-16 boundary
  test 23 passed.
- Wave 2: 6 of 8 done. 2f and 2h need work, both for the same reason — the behaviour is right and
  the test that is supposed to prove it is not. **Neither has been fixed.**
- Wave 3: 2 of 9 done (3a, 3b, both landed since the first pass). 3c–3i untouched.
- Wave 4: 1 of 6 lanes partially addressed, none complete. ~9 days of effort, no capability.
- Wave 5: 4 of 6 done. 5a, 5b, 5c, 5d all landed since the first pass. 5e and 5f remain partial.
- AC-4: code is present, the entry half is vacuous, the data is one download away.

The recurring theme is not missing code. It is **assertions that cannot fail**: 2h's lock test, 2f's
order gate, AC-4's entry half, and 3g's per-connection cache that passes a name-based check. Four
independent instances of the same class, in the four lanes the programme treats as money-path proof.

The eight lanes that landed since the first pass of this audit do not repeat the pattern. Each
carries a negative control that fails when the control is removed, and 5b's validation test parses
the rules file and rejects any expression lacking a numeric comparison. The 3b lane found two real
bugs in existing code rather than adding to the tree blind.

One correction to the report that announced those eight lanes: it describes rewriting the 2h
concurrency test so that it controls the yield window and fails on a lockless implementation. That
rewrite is not in the tree. See F-2 below. Everything else in that report checked out.

## Closure log

Every row below was re-verified by the coordinator after merging, not accepted from an agent's
report. "Mutation" is the count of tests that FAILED when the lane's protection was removed.

| Lane | Finding | Commit | Mutation |
|---|---|---|---|
| 2h | F-2 closed: the lock now has a control that can fail | `96f40ca` | 2 fail |
| 2f | F-1 closed: the kill switch has an end-to-end test | `9f70d81` | 4 fail |
| AC-4 | F-5 closed: 438 sourced delisting rows, entry half load-bearing | `b447664` | 5 fail |
| 2e | safety route no longer degrades to null | `69afb0a` | 8 fail |
| 3f | quarantine + live alert | `ea5c703` | 17 fail |
| 5f | deploy artifacts with a probe block | `dd59354` | 4 fail |
| 3c | late-tick window, booked bars frozen | `b78b765` | 9 fail (window), 5 fail (immutability) |
| 3d | per-symbol cadence + counted halt exemption | `ae33c18` (re-merged) | 9 fail |
| 3g | process-level cache, corrected twice | `86538b0`, `b78b765`, `8b3340a` | 1 fail (caught on re-test) |
| AC-22 | `submit()` is the only path to the venue | `df52f4c` | 5 fail |
| 4c | event store version registry + `prev_hash` chain | `9bbcd61` | 15 fail |
| 3i | scanner rate limit + cache + portfolio context | `2aec13a` | 4 fail |

### Four things the parallel run taught, worth keeping

1. **The editable-venv trap is the single biggest source of false results here.** Every worktree
   test run resolves `tradex_*` to the main checkout. Three separate agents had to be told. One
   produced a false PASS because of it before the rule was established.
2. **A negative control that injects its own dependency proves nothing.** 3g's shared-cache test
   passed the cache in explicitly, so it could not detect the cache being un-shared. The fix tests
   the DEFAULT construction path, which is what production uses.
3. **A repo-wide static check must exclude non-source trees.** The AC-22 check globbed
   `*/src/**/*.py` and read `.kilo/worktrees/sour-ragdoll/`, a gitignored copy of an older tree.
4. **A merge that printed a file list is not proof the files are in the tree.** Two lanes were
   reported merged and were not: 3d was absent from `HEAD` entirely, and 3g's default-path tests
   were dropped. Both were caught only by walking the branches afterwards and running
   `git log <branch>..HEAD` plus a file-existence check per lane. Every closure claim in this
   document should be read the same way: re-derived from the tree, not from the merge transcript.
5. **"Pre-existing" must be established by bisect, not by a second worktree.** The one
   pre-existing verdict in this document was wrong for exactly the reason lesson 1 gives: the
   reproduction ran from a pristine worktree, and the editable install pointed it back at the main
   checkout's modified source. The reliable procedure is to `git checkout <commit> -- <src files>`
   *in place*, run the test, and restore — that changes what the interpreter actually imports. A
   bisect done that way over the 12 lane commits is what located `ea5c703`. A claim of
   "pre-existing" that was not obtained in place is not a claim, it is a guess, and it hides real
   regressions: here it hid 38 failing tests behind a deselection.
6. **A test with an ambient input is a test that fails on a schedule.** Lane 3d's composition test
   read the real wall clock and handed it to a monitor that skips its whole sweep outside session
   hours, so it passed every morning and failed every evening. It looked like a broken lane and was
   really a broken test. Anything whose behaviour is gated on session hours, a date, or a timezone
   must be pinned to an explicit instant in its test, never to `datetime.now()`.

### Still open after the closure run

- **3e** parquet predicate pushdown (performance only, no AC).
- **4c items 1, 4, 5**: account-scoped `stream_id`, `Snapshot` on READY, account-partitioned dedup.
  Sequenced after 2 and 3 because item 1 changes the hashed content of every row.
- **4a, 4b, 4d, 4e, 4f**: Wave 4 consolidation. Unstarted. No capability, ~9 days of effort.
- **5e**: the reconciliation monitor and venue-position adoption. Drift is reported, never adopted.
- **3i portfolio context** is implemented but nothing calls `set_portfolio()` yet, so it does not
  reach the UI.
- **`trading/tests/interface/test_ws_bars_replay.py`**: **fixed**, and it was not pre-existing. The
  18 failures were caused by AC-15's `metrics=` kwarg against seven one-argument test fakes, and
  the original "pre-existing" verdict came from a worktree reproduction the editable venv defeated.
  It now runs 38 passed in 19s and is no longer deselected.
