# Reconciliation review: adversarial verification of Findings 1-7

Date: 2026-09-28
Branch: `wave0-1-recovered`
Reviewer role: adversarial (assumed the brief was wrong; each claim reproduced or pinned to source).
Brief under test: swarm shared key `recon-review-findings` (from `crocodile`).
No source or test files were modified. A throwaway reproduction script was run against the repo's `.venv`.

## Verdict table

| # | Claim | Verdict | Decisive evidence |
|---|-------|---------|-------------------|
| 1 | Reconcile seam returns `[]`; no drift reaches `monitor.last_drifts` from the daemon | **VERIFIED** | safety.py:1474 `engine.reconcile()` with zero kwargs; engine.py:1360,1367 both compare blocks gated on `is not None`; repro returns `[]` with a reconciler that finds 2 drifts |
| 2 | Unsettled guard missing on venue-only branch | **VERIFIED** | safety.py:936 guard lives only in the both-sides loop; venue-only loop safety.py:959-973 has no `unsettled` test; repro adopts venue position on X with `unsettled={"X"}` |
| 3 | `_cleared_position` is dead code | **VERIFIED** | def safety.py:1022-1030; repo-wide grep finds zero callers; clear path inlines `_flat_position(local) or _flat_position(venue)` at safety.py:946 |
| 4 | `ReconciliationResult` appended on every reconcile call (event log growth) | **VERIFIED** | engine.py:1387 unconditional `self._append(ReconciliationResult(...))`; `_append` engine.py:329 writes to the event store; monitor polls every 30s (reconciliation_monitor.py:244-249); safety.py:594-595 derives `last_checked_at` from the newest `ReconciliationResult` |
| 5 | Double `_position_instrument_id` call + `_unsettled()` fail-open | **VERIFIED** | safety.py:1455-1456 calls `_position_instrument_id(p)` twice per position in the comprehension; safety.py:1432-1434 `except Exception: ... return frozenset()` disables the guard when the order read raises |
| 6 | Adoption apply has no multi-key atomicity and no coordination with the fill path | **PARTIAL** | Core risk confirmed: `_apply_venue_adoption` safety.py:1334-1335 writes one `set_position` at a time, and `TradingCache.set_position` (trading_cache.py:96-99) takes only the dict-level `_positions_lock`, never the per-instrument `position_lock` the fill path holds across read-modify-write (position_accountant.py:46-52, 53-65). Mechanism detail wrong in the brief: `_positions_lock` is a single global dict lock, not "per-key". The real hole is worse than stated: adoption can overwrite a concurrent fill's RMW because it ignores `position_lock` entirely |
| 7 | Test gap: factory asserted on the paper branch only | **VERIFIED** | test_live_bind_policy.py:208-236 (factory asserted, env set PAPER/paper at 217-219); test_live_bind_policy.py:177-205 covers backtest/replay with no factory assertion (201-204); the live-branch test (161-174) only checks the loopback refusal and never reaches boot. serve_app does pass the factory on all 3 branches (fastapi_app.py:520, 526, 532) but only the paper branch is pinned |

## Finding 1 detail: can ANY drift reach `monitor.last_drifts` from the daemon path?

No.

The daemon chain is `ReconciliationMonitor._run` -> `poll_once` -> `self._reconcile()` (reconciliation_monitor.py:237, 251-267) -> `self._reconcile_fn()` which is the seam `_reconcile` from `build_reconciliation_monitor_seams` (safety.py:1470-1477). That seam calls `tuple(engine.reconcile())` (safety.py:1474) with no arguments.

`ExecutionEngine.reconcile` (engine.py:1351-1388) is keyword-only: `broker_orders`, `broker_positions`, `local_positions`. The positions comparison is inside `if broker_positions is not None:` (engine.py:1360) and the orders comparison inside `if broker_orders is not None:` (engine.py:1367). Called with no kwargs, both blocks are skipped and it returns an empty `drifts` list, always.

Reproduction (stub reconciler that finds `POSITION_DRIFT` + `ORDER_DRIFT` on real compare):

```
F1:  no-arg reconcile() -> []                       # reconciler would find 2 drifts
F1b: with kwargs       -> ['POSITION_DRIFT', 'ORDER_DRIFT']
```

The seam result lands in `self._last_drifts` (reconciliation_monitor.py:259-266) exposed as `last_drifts` (196-201). Since the only producer on the daemon path is the no-arg seam, `monitor.last_drifts` is permanently `()` regardless of actual drift.

Two related paths are NOT broken and were checked to keep the claim honest:

- startup reconciliation (`startup.py` `_run_startup_reconciliation`, live-only, plus the post-reconnect hook) calls `engine.reconcile(broker_orders=..., broker_positions=...)` with correct kwargs and records via `session.record_reconciliation`. That feeds `session.last_reconciliation`, not `monitor.last_drifts`.
- the safety surface's `_rerun_reconciliation` (safety.py:603-610) also passes the snapshots correctly.

So the defect is specifically the monitor/daemon seam, exactly as the brief says.

## Finding 2 detail: the exact counterexample

Setup:

- a local order on instrument X in status `NEW` (also PENDING/ACK/SUBMITTED/PARTIALLY_FILLED/UNKNOWN), so `unsettled_local_instruments` returns `{"X"}` (safety.py:810-819, 848-856).
- the local book (cache) has no position on X: either the BUY was just submitted (no fill booked yet) or the fill has not been written. `local_by_id` does not contain X.
- the venue `get_positions()` snapshot is older than the order (or reflects a manual/old trade) and reports a nonzero position on X.

What the plan does with X today: `derive_venue_adoption` (safety.py:867-984) takes the venue-only branch, `for iid in sorted(set(venue_by_id) - set(local_by_id))` (safety.py:959). That loop checks only quantity readability (962), zero (965), and position constructibility (969). There is no `if iid in unsettled` anywhere in the loop (the only unsettled test is safety.py:936, in the both-sides loop). So X goes straight into `positions` and `_apply_venue_adoption` (safety.py:1334-1335) writes the venue's stale position to the book.

Reproduction (local=[], venue=[X qty 5], unsettled={"X"}):

```
F2: positions bucket: {'X': Position(... qty 5 ...)}
F2: skipped bucket:  {}
F2: X skipped as None (expected 'unsettled_fill' if guard applied)
```

When the NEW order then fills locally, the booked fill is merged on top of (or races, see Finding 6) a position adopted from a stale snapshot. The docstring at safety.py:822-846 promises "An unsettled fill is never adopted over" (module-level promise) but the venue-only branch violates it. The brief is right, and the docstring is wrong.

## Finding 7 detail: which branches assert the factory

- `test_serve_app_passes_the_reconciliation_factory` (test_live_bind_policy.py:208-236): env `TRADEX_SERVE_BROKER=PAPER`, `TRADEX_SERVE_MODE=paper` (217-219), asserts `boot.call_args.kwargs.get("reconciliation_monitor_factory") is build_reconciliation_monitor` (233-235). This exercises serve_app's paper branch only. Its docstring claims "This pins the factory on every spawn-built session", which is false: one of the three `boot()` calls in `serve_app` is pinned.
- `test_serve_app_builds_non_live_dhan_session` (177-205, parametrized backtest/replay): asserts `config.broker_id`, `config.mode`, `create_app` call (201-204). No factory assertion.
- `test_serve_app_rejects_live_non_loopback_before_session` (161-174): live branch never reaches `boot()`, so no factory assertion is possible there. No other test in the file exercises the live-branch `boot()`.

Result: the live boot branch (fastapi_app.py:526 area) and the non-live branches (520, 532) pass the factory in source today but only the paper branch (520) is regression-pinned.

## Test suites

Command: `.venv/bin/python -m pytest trading/tests/runtime/test_reconciliation_wiring.py trading/tests/runtime/test_reconciliation_monitor.py trading/tests/interface/test_safety_adoption.py trading/tests/interface/test_live_bind_policy.py -q`

| Suite | Result |
|-------|--------|
| trading/tests/runtime/test_reconciliation_wiring.py | PASS |
| trading/tests/runtime/test_reconciliation_monitor.py | PASS |
| trading/tests/interface/test_safety_adoption.py | PASS |
| trading/tests/interface/test_live_bind_policy.py | PASS |

Combined: **138 passed in 1.05s, 0 failed**. Note the suites pass while Findings 1, 2 and 7 stand: nothing pins the no-arg seam to real drift output, nothing exercises the unsettled guard on the venue-only branch, and the live-branch factory is unpinned. Green here is not evidence against the findings.

## Notes on minor points in the brief

- Finding 5 double call: confirmed literally at safety.py:1455-1456 (`_position_instrument_id(p)` in both the element and the condition). Complexity only, no behavior change.
- Finding 5 fail-open: confirmed at safety.py:1432-1434. The read is separately guarded (a failed venue read refuses the adoption at safety.py:1253-1306), but the unsettled guard itself degrades to "protect nothing" exactly when the local order book is unreadable.
- Finding 6 wording: `TradingCache._positions_lock` is a single dict-level lock (trading_cache.py:93-99), not per-key. Per-instrument locks exist separately as `position_lock()` (trading_cache.py:76-89) and are used by the fill path (position_accountant.py:46-52) and mark-to-market (mark_to_market.py:91). `set_position` and `update_position` share the same dict lock, so per-write atomicity holds, but adoption writes never take `position_lock`, so they can clobber a fill's in-flight read-modify-write on the same instrument. Multi-instrument apply also interleaves with concurrent readers (`all_positions` returns an in-apply snapshot).
