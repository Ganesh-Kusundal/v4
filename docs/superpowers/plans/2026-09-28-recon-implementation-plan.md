# Reconciliation/Adoption Subsystem — Implementation Plan

Date: 2026-09-28 · Branch: `wave0-1-recovered`
Inputs: `2026-09-28-recon-verification.md` (6/7 findings VERIFIED, 1 PARTIAL),
`2026-09-28-recon-quant-risk.md` (risk ranked, oscillation failure mode),
`2026-09-28-recon-adoption-redesign.md` (target design, flows, diagrams).

Goal: make lane 5e's daemon actually report drift, make adoption safe against
in-flight fills on **every** branch, and close the fill/adoption lost-update
race — then pin all of it with failing-tests-first so none of it regresses.

## Global constraints

- TDD: failing test first for every behavior change. No fix without a test that
  fails before it and passes after.
- `tradex_runtime` may NOT import `tradex_interfaces`
  (`tests/test_import_boundaries.py` pins it). Joins stay in `tradex_interfaces`.
- One venue read per poll wherever possible: `get_positions()` is rate-limited.
- Fail closed beats silent continue on the money path (repo doctrine, and the
  `_unsettled()` fail-open is the violation).
- Gates per phase: targeted suites for the blast radius, then on merge:
  `.venv/bin/python -m pytest domain/tests brokers/tests trading/tests` (root
  conftest supplies pythonpath), `ruff check .`, mypy on touched files,
  `tests/test_import_boundaries.py`.
- Baseline: 5604 passed at `5ec4b8a`. Any phase that cannot hold that number
  green (except by *adding* tests) stops and re-plans.

## Phase ordering and dependencies

```
P0-F6 (lock race) ──┐
P0-F2 (unsettled) ──┼──> P0-F1 (reconcile seam) ──> P1-F7 (tests) ──> P1-F3 (dead code)
P0-Q1 (realized)  ──┘                                  │
                                                        v
                                    P2-F5 (seams hygiene) ──> P2-Q4 (tolerance)
                                                                   │
                                                                   v
                                                    P3-F8 (severity) + P3-Q5 (gauge)
```

P0 items are independent of each other and can run as parallel worktrees
(max 2, per the operational-minimum plan's ceiling). P0-F1 comes after
P0-F6/P0-F2 deliberately: fixing the seam makes the monitor *act* on drift, so
the mutation paths must be safe first.

---

## P0-F0 — already landed (record only)

`5ec4b8a` gives `serve_app` the reconciliation factory on all three branches;
`a22d707` records the boot-site audit. Nothing to do. The `test_live_bind_policy`
docstring overclaim ("pins the factory on every spawn-built session") is fixed
in P1-F7, not here.

---

## P0-F6 — fill/adoption lost-update race (WORST HOLE — do first)

**Why first:** quant risk ranks it #2 by bound but #1 by exploitability: it is
reachable on every poll against a live fill, and it produces a **period-2
oscillation**, not a bounded error (quant §4, Cycle 1). Two-line cause:
`_apply_venue_adoption` (safety.py:1319-1340) writes via
`TradingCache.set_position` (trading_cache.py:96-99) which takes only the
dict-level `_positions_lock`, never the per-instrument `position_lock()`
(trading_cache.py:76-89) the fill path holds across read-modify-write
(position_accountant.py:46-65).

**Failing tests first** (`trading/tests/interface/test_safety_adoption.py`):

1. `test_apply_adopts_under_the_per_instrument_position_lock` — a fake cache
   records which locks `set_position`/`set_positions` is called under; assert
   the apply path holds `position_lock(iid)` for every written instrument.
2. `test_apply_batch_writes_all_or_nothing` — a cache whose Nth write raises
   leaves the book unchanged (today: N-1 rows written).
3. `test_apply_takes_instrument_locks_in_sorted_key_order` — deadlock-freedom
   against the fill path's single-lock RMW (record lock acquisition order).

**Fix:**

- `TradingCache.set_positions(Sequence[Position])` (trading_cache.py): acquire
  each `position_lock(key)` in **sorted key order**, then `_positions_lock`,
  swap all rows, release. Sorted order is what makes concurrent multi-instrument
  applies and fill RMWs deadlock-free.
- `_apply_venue_adoption` (safety.py) calls `set_positions((*positions,
  *cleared))`. Fallback: if the cache lacks the method, keep the per-row loop
  but `log.warning("adoption apply is NOT atomic: cache lacks set_positions")` —
  visible, never silent.
- Mark-to-market (mark_to_market.py:91) already holds `position_lock`; no
  change needed there — verify with a test that adoption apply and a concurrent
  mark cannot interleave on one instrument.

**Gate:** `test_safety_adoption.py`, `trading/tests/execution/` cache + accountant
suites, `trading/tests/interface/test_safety_adoption.py` race test run 50x in a
loop (flaky-race detector) before merge.

**Rollback:** single commit; revert restores the per-row loop (the fallback path
still exists).

---

## P0-F2 — unsettled guard missing on the venue-only branch

**Why:** `derive_venue_adoption` (safety.py:959-973) adopts venue-only rows with
**no** `unsettled` check; the both-sides branch has it at :936. Counterexample
(verified by repro): local order `NEW` on X, no local position row, stale venue
snapshot reports X qty 5 → X lands in `plan.positions` and is written. The
docstring's "An unsettled fill is never adopted over" (safety.py:822-846) is
false on this branch. Worst case per quant §4 D: the whole stale position is
restated over a live fill, unbounded in size.

**Failing tests first** (`test_safety_adoption.py`):

1. `test_venue_only_row_on_an_unsettled_instrument_is_skipped` —
   `local=[], venue=[X qty 5], unsettled={"X"}` ⇒ `plan.skipped == {"X":
   SKIP_UNSETTLED_FILL}`, `plan.positions == {}`.
2. `test_venue_only_settled_row_is_still_adopted` — same without the guard set
   ⇒ adopted (no over-blocking).
3. `test_unsettled_guard_fails_closed_when_orders_are_unreadable` — an engine
   whose `all_orders()` raises ⇒ the adoption is **refused** (distinct refusal
   code `unsettled_guard_unavailable`), not applied unguarded. Today
   `_unsettled()` (safety.py:1422-1434) returns `frozenset()` = guard silently
   off (quant §3.3 amplifier: the window becomes "no guard at all").

**Fix:**

- In the venue-only loop, before `_adopted_position(None, venue)`:
  `if iid in unsettled: skipped[iid] = SKIP_UNSETTLED_FILL; continue` —
  symmetric with :936.
- `_unsettled()` returns `None` on failure (tri-state: set = known, None =
  unknown). `_adopt` treats `None` as refusal
  `AdoptionOutcome(refusal="unsettled_guard_unavailable", applied=False)`.
  Rationale for fail-closed: this is the money path; the guard's failure mode
  today is exactly the corruption lane 5e exists to prevent.

**Gate:** `test_safety_adoption.py`, `test_reconciliation_monitor.py`,
`test_reconciliation_wiring.py`.

**Rollback:** revert the one commit; the docstring change rides with it (the
promise becomes true or is qualified — never in between).

---

## P0-Q1 — adoption destroys the local realized-P&L ledger

**Why (quant §1):** `_adopted_position` (safety.py:1031-1070) writes the venue's
`realized_pnl` (raw, un-q2-ed, **default 0 when the venue omits it**) and
un-`q2`-ed `avg_price`. Worked example: 10 shrs, local avg 100.00 vs venue
99.95, mark 102 ⇒ total P&L jumps by `R_v + F + 0.50` where F is the fee ledger
`on_fee` booked (position_accountant.py:81-96). When the venue omits realized
(the common Dhan/Upstox case), **the fee deduction is silently destroyed**.
`trade_metrics` derives from the fill stream (trade_metrics.py:135) and
`positions_pnl_by_strategy` reads `Position.total_pnl` — after adoption the two
ledgers disagree structurally.

**Failing tests first** (`test_safety_adoption.py`):

1. `test_adopt_preserves_the_local_realized_pnl` — venue omits realized ⇒ the
   adopted position carries the local `realized_pnl`, not 0.
2. `test_adopt_quantizes_the_venue_avg_price_to_q2` — adopted basis is on the
   same q2 grid the fill path books (kills quant §4 Cycle 2's flip-flop
   amplitude).
3. `test_adopted_position_agrees_with_fill_log_total_pnl` — round-trip through
   `apply_fill` + adoption + `trade_metrics.round_trip_trades` vs
   `positions_pnl_by_strategy`: Δ == 0 for a venue whose figures match.

**Fix:**

- `_adopted_position`: carry `realized_pnl` from `local` when the venue omits
  it (adopt the venue's only when it actually reports one — and q2 it); q2 the
  adopted `avg_price`. Document the deliberate asymmetry in the docstring.
- Decision to make explicitly at implementation time (record in the commit):
  when the venue *does* report realized, whose figure wins. Recommendation:
  venue wins only when `local.realized` is fee-only (no closes yet); otherwise
  keep local and log the discrepancy as drift — that keeps one ledger (the fill
  log) authoritative for `trade_metrics`.

**Gate:** `test_safety_adoption.py`, `trading/tests/analytics/` trade_metrics
suite.

**Rollback:** one commit; the old "venue object as-is" behavior is the revert.

---

## P0-F1 — the reconcile seam reports no drift (confirmed defect)

**Why:** `build_reconciliation_monitor_seams._reconcile` (safety.py:1470-1477)
calls `engine.reconcile()` **with no arguments**; `ExecutionEngine.reconcile`
(engine.py:1351-1388) gates both compare blocks on `is not None`, so it always
returns `[]`. Verified by execution: `[]` despite 2 real drifts.
`monitor.last_drifts` is permanently empty; the module docstring's "reports
drift exactly as before" is false.

**Failing tests first:**

1. `test_seams_reconcile_reports_position_drift` (`test_safety_adoption.py`) —
   book 3 vs venue 7 on X ⇒ `seams.reconcile()` returns the X drift item.
2. `test_monitor_last_drifts_reflects_the_book` (`test_reconciliation_monitor.py`)
   — one `poll_once` on a drifted book ⇒ `monitor.last_drifts` non-empty.
3. `test_seam_reads_the_venue_once_per_poll` — the reconcile and adopt seams
   share **one** `get_positions()` call per poll (count on a fake broker == 1).

**Fix:**

- `_reconcile` closes over the same `read_venue_positions` result the adopt seam
  uses: restructure `build_reconciliation_monitor_seams` so `_adopt` performs the
  single venue read and hands the sealed `VenueRead` to a drift-only comparison;
  `_reconcile` runs first against the *previous* poll's read + current book, or
  the two are collapsed into one seam the monitor calls (preferred: change
  `ReconciliationMonitorSeams` to one callable `poll() -> (drifts, outcome)` and
  keep `reconcile`/`adopt` as compatibility shims — the monitor's API change is
  contained in `reconciliation_monitor.py`, which `runtime` owns outright).
- Orders drift: include `compare_orders` via `broker.get_orderbook()` **only if**
  the endpoint cost is acceptable; startup reconciliation already compares
  orders, and the seam should not be silently narrower. Decide in the commit;
  default yes (order drift is the UNKNOWN-order population the safety surface
  exists to show).
- `ReconciliationResult` append (engine.py:1387): once the seam works, this
  appends every 30s = 2880 rows/day. Fix here: append **on drift only** (a
  zero-drift poll adds no information; `last_checked_at` then means "last time
  something was found", so ALSO set `session.record_reconciliation(drifts)` from
  the seam and let `reconciliation_checked` carry the "checked, found nothing"
  answer — the type already distinguishes `None` from `()`).

**Gate:** `test_reconciliation_monitor.py`, `test_reconciliation_wiring.py`,
`test_safety_adoption.py`, `test_safety_wiring.py`, durability suite (the event
log's growth behavior is pinned in `trading/tests/persistence/` if such a pin
exists — add one: "a zero-drift reconcile appends no event").

**Rollback:** the seam change is one commit; the `ReconciliationMonitorSeams`
shims let a revert keep the old two-callable shape.

---

## P1-F7 — serve_app factory test gap (and a false docstring)

**Why:** the factory is pinned on the **paper** branch only
(test_live_bind_policy.py:208-236). `test_serve_app_builds_non_live_dhan_session`
(177-205, backtest/replay) asserts nothing about it; the live branch never
reaches `boot` in tests. The test's docstring "pins the factory on every
spawn-built session" is false — the verifier's audit.

**Failing tests first:**

1. Parametrize `test_serve_app_passes_the_reconciliation_factory` over
   `("paper", "backtest", "replay", "live")` with the env vars that reach each
   branch's `boot` (for `live`, a loopback host + API key so the guards pass and
   `boot` is actually called — mock `boot` as the existing tests do).
2. Fix the docstring to name what is pinned.

**Fix:** test-only + docstring. If the live branch cannot reach `boot` with the
current guards in a test, that itself is Finding-worthy — record it in
`programme-closeout.md`'s open table rather than force it.

**Gate:** `test_live_bind_policy.py` full file.

---

## P1-F3 — delete dead `_cleared_position`

safety.py:1022-1030, zero callers (the clear path inlines
`_flat_position(local) or _flat_position(venue)` at :946). Verify with
`grep -rn "_cleared_position"` (expect only the def), delete, run the full
`test_safety_adoption.py` + import-boundary gate. One-line rollback.

## P2-F5 — seams hygiene

- `_adopt`'s `known_instruments` comprehension calls `_position_instrument_id(p)`
  twice per position (safety.py:1455-1456). Bind once (walrus or loop).
- With P0-F2's tri-state, delete the old fail-open comment and its test-shaped
  cover. Small, test-covered by P0-F2's tests.

## P2-Q4 — tolerance to notional basis

Quant §2: `_ADOPTION_TOLERANCE = 0.01`/share hides `0.01 × qty` — 1,000 INR at
100k shares — and defines the ping-pong threshold of quant §4 Cycle 2. Fix:
tolerance = `max(0.01, 1.00 INR / qty)` (a 1-rupee notional floor with the
per-share floor kept for odd lots), tested at 1 / 1,000 / 100,000-share
positions. P0-Q1's q2 of the adopted basis shrinks Cycle 2 below this line in
most cases; this closes the rest.

## P3-F8 — severity divergence (align or document)

`engine.reconcile` emits `reconciliation_drift_total.<severity>.<kind>` with the
**stored** `DriftItem.severity` while AC-17 derives severity from facts and the
stored values contradict it (safety.py:5-15: stored MEDIUM exists; status
mismatch is LOW there, HIGH here). Either (a) derive in the engine too (single
rule — moves `derive_severity` to a package both can see; `tradex_execution` is
below both, so the rule can move down), or (b) document the divergence in
`deploy/prometheus-rules.yml` and the runbook. Recommend (a) long-term, (b) now.

## P3-Q5 — gauge semantics

Quant §5: `venue_adoption_last_adopted` freezes on refusal and cannot separate
"monitor dead / all polls refused / nothing to do". Add
`venue_adoption_last_success_timestamp_seconds` (set every applied poll); alert
on `time() - gauge > 2 × RECONCILIATION_INTERVAL_SECONDS` (60s). Optionally
split `last_planned_total` vs `last_applied_total`; the gap is the un-actioned
drift an operator must see. Alert rule + a monitor test pinning the new gauge.

---

## Merge sequence and gates

| Merge | Contents | Gate |
|---|---|---|
| M1 | P0-F6 + P0-F2 + P0-Q1 (mutation-safety cluster) | targeted suites + 50x race loop |
| M2 | P0-F1 (drift reporting) | targeted suites + persistence pin |
| M3 | P1-F7 + P1-F3 + P2-F5 | targeted suites |
| M4 | P2-Q4 + P3-F8(b) + P3-Q5 | full root suite + prometheus rule lint |

Each merge: full root suite `5604 + new tests` passed, ruff clean, mypy on
touched files (2 pre-existing errors at baseline `e6afb7e` are the allowance),
import boundaries pass. No agent merges; controller merges, per house rules.

## What this plan deliberately does NOT do

- The closeout's open items (recovery snapshot folding, `FillDedup` boundary,
  `scanner.set_portfolio`, `row_group_size`) — separate, non-blocking.
- Wave 4 consolidation (4a/4b/4e deferred by measured cost in
  `lane-4a-feasibility.md`).
- Auth gaps from the 2026-09-27 review (S2/S5) — different subsystem, higher
  priority than everything here if live exposure is possible; flagged, not
  folded in.
