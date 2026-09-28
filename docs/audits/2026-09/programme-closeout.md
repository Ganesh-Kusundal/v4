# Programme closeout: production-readiness waves 3/4/5

Pinned at `267b620` plus the reconciliation wiring commit that follows it.

This is the record of what the six lanes changed, what they proved, and what is
still open. It exists so the next person does not have to re-derive any of it
from the commit log.

## What was true at the start

The programme ran against a tree where five lanes of prior work had already
landed. The finding that shaped everything after it: **38 tests were being
hidden behind a "pre-existing failures" verdict that was wrong**. The editable
install is load-bearing — 17 `_editable_impl_tradex_*.pth` files carry absolute
paths into the main checkout — so a run from a different working directory
imports a stale shadow and fails for reasons that have nothing to do with the
code. That trap produced a false "pre-existing" verdict, and the same trap is
standing lesson #1 in `CLAUDE.md`: import from the checkout you are standing in,
or you are testing a different tree.

## The six lanes

| Lane | Commit | What changed | Control |
|---|---|---|---|
| 3i | `9456480` | Portfolio routes serve the live OMS book | 3 mutation controls, each failed 6/7 |
| 4f + 4d | `8e2680f` | 12 `CLAUDE.md` errors corrected; dead-code analysis | zero proven-dead Python |
| 3e | `38fd436` | Parquet predicate pushdown into the read path | monkey-patched `_read_partition` counter |
| 4c | `05e4384` | Account-scoped `stream_id`, snapshots, per-account dedup | 15 mutation controls, all failed |
| 5e | `267b620` | Reconciliation monitor that *adopts* venue positions | 21 mutation controls, all failed |
| wiring | (this commit) | The 5e monitor attached to the session lifecycle | 4 mutation controls, all failed |

### 3i — the portfolio routes were reading a dict nothing writes

`/holdings` read `PaperBroker.get_holdings()`, whose dict the fill path never
writes (`PaperFillSource.position_projection_owned = False`,
`fill_sources.py:206`). The route was serving an empty book to a UI that
believed it was showing positions. Now `routes/deps.py` sources
`get_session_portfolio()` from `TradingCache.all_positions()`, and 7 tests boot
a real `TradingSession.paper()` to prove the book is the live one.

### 4f — the documentation was actively misleading

Every `tradex_trading.*` path in `CLAUDE.md` was dead — retired in `b941da2`,
importing it raises `ModuleNotFoundError`. `_clipped_tail` and `_merge_candles`
never existed; the real function is `_stitch_windows`. `session.market` is
`session.broker`. A reader following the doc would land on code that is not
there.

### 4d — there is no dead Python to delete

Only 2 of 240 modules have zero importers, and both are `-m` entry points. The
dead code that does exist is 4 TypeScript modules in `frontend/src`. **Deletion
was gated to analysis-only** per the programme's constraint; it needs human
approval.

### 3e — pushdown is real and measured

`fragment.to_table(filter=...)` at `parquet_storage.py:354` pushes the predicate
into the reader instead of materialising and filtering in Python. Measured:
312,456 → 266,584 rows materialised, byte-identical frames.

### 4c — account scoping without breaking old rows

`stream_id` is now `f"{broker_id}:{account_id}:{stream}"` with
`SCHEMA_VERSION=2` and a registered v1 decoder. Chain verification is keyed on
schema version, so **old rows still verify** — the hard constraint, tested in
durability. Fill dedup partitions per account as `fill_dedup_{account}`.

### 5e — the monitor adopts instead of just reporting

Before this lane, `grep -rn 'def adopt'` returned nothing. `safety.py` reported
drift and nothing ever acted on it. `adopt_venue_positions()` now mutates the
book via `cache.set_position`, and is safe by construction: a failed, partial,
or stale read is refused *before* any position is touched.

## The verdict on Wave 4 consolidation: defer

`lane-4a-feasibility.md` records the detail. The proposed 17→5 package merge is
a **rewrite, not a mechanical move**: 2,639 import statements across 719 files,
94% of tracked `.py`. The spec's stated motivation — duplicate module names — is
false, except for **one collision the merge would create**: `engine.py` at top
level in both `analytics` (AnalyticsEngine, 394 lines) and `execution`
(ExecutionEngine, 1,617 lines). Two classes, one name, no overlap.

**4a, 4b, and 4e are deferred.** Not declined — deferred, because the cost is
measured and the benefit is not.

## What is still open

These were reported by the lanes, not fixed. Each is one focused change.

| Item | Lane | Why it matters |
|---|---|---|
| `recovery.py` does not fold from `latest_snapshot` | 4c | A restart replays the whole log instead of replaying from the last snapshot |
| `FillDedup` owns the restart boundary | 4c | The dedup set is rebuilt on restart; the boundary belongs with the store, not the guard |
| `set_portfolio` at `scanner.py:272` has no production caller | 3i | The scanner's portfolio context is documented but never wired |
| Write-path `row_group_size` for intra-month pushdown | 3e | Row groups are month-granular, so an intra-month filter still reads the whole month |

None of these is load-bearing for production correctness. Each is a focused,
well-scoped improvement.

## The composition root, and why it is not in `boot()`

The monitor is attached to the session lifecycle, but something has to
*construct* it. That cannot be `boot()`: the two halves of the daemon sit on
opposite sides of the package boundary, because `build_reconciliation_monitor_seams`
is in `tradex_interfaces` and `ReconciliationMonitor` is in `tradex_runtime`,
which may not import it.

The legal composition point is `cmd_serve` in `tradex_interfaces/cli.py` — the
production entrypoint, and the package that can see both. It passes a factory
closure into `boot()` rather than assigning the monitor after the fact, because
`boot()` calls `session.start()` before it returns: a monitor assigned post-hoc
would be constructed and never started. The factory receives the real broker,
cache, and engine, so it builds against the session that is actually running,
not a shadow the caller had to reconstruct.

Verified end to end: `cmd_serve`'s exact boot path builds a real
`ReconciliationMonitor`, it is running when the session reaches READY, and it
is stopped cleanly on `session.stop()`.

## Gates

| Gate | Result |
|---|---|
| Full suite, all lanes together | 5600+ passed, 0 failed |
| ruff, whole repo | clean |
| mypy, touched files | 2 errors, both pre-existing at baseline `e6afb7e` on identical lines |
| Import boundaries | passed; `runtime` still may not import `tradex_interfaces` |
| End-to-end `cmd_serve` boot | monitor running at READY, stopped on `session.stop()` |
