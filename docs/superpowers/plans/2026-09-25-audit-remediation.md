# Execution Plan — Audit Remediation (Superpowers + Ponytail)

**Date:** 2026-09-25
**Audit:** `docs/ARCHITECTURAL-AUDIT-2026-09-25.md`
**Method:** superpowers `subagent-driven-development` + `dispatching-parallel-agents`, ponytail `full`
**Branch:** `chore/cleanup-overhaul` (in place, **no commits without asking**)

---

## Pre-flight review (superpowers step 1) — two blockers, stated up front

**Blocker 1 — the tree is dirty in a way that breaks agent isolation.**
222 modified, 91 deleted, 121 untracked. Critically, whole extracted packages are
*untracked* (`analytics/`, `interfaces/`, `market_data/`, `execution/…`). Two
consequences:
- `git checkout`/`stash`/`diff HEAD` would not describe the real working state.
- If an agent runs `git add -A`, it sweeps up **all** 432 pre-existing changes under
  its name, and per-task review diffs become meaningless.

→ **Mitigation, decided not to ask:** agents **edit files in place and do not run any
git command that mutates state** (`add`/`commit`/`checkout`/`stash`/`restore`).
Verification is by test run, not diff. Reviewers read the *files*, not a git diff.
This is the ponytail rung-2 answer: the existing guard suite is the verification
mechanism that already exists; don't build a new one for this.

**Blocker 2 — `test_import_boundaries.py` is already modified** and is the file W2
must extend. Uncommitted work in a file another agent must edit = lost-update risk.
→ **Mitigation:** W2 is dispatched **alone in wave 1**, before the parallel wave. It
is also the plan's designated regression net, so it must land first regardless.

Neither blocker is fatal. Both are worked around without inventing infrastructure.

---

## Ponytail applied to the plan itself

The audit proposed 24 tasks. Most fail the ladder. Cuts made:

| Cut | Why |
|---|---|
| REF-3 (delete dead dirs), REF-12 (credential transport), REF-13 (origin policy), REF-22 (protocol types), REF-23/24 (mypy+coverage) | Nothing is broken. Rung 1: does this need to exist? Defer until someone is bitten. |
| REF-10/11 (generated wire contract) | Real problem (`product` dropped), but the fix is *one field in one function*, not a codegen pipeline. Rung 6. REF-11 collapses to a 3-line fix. |
| REF-16/17 (WS lifecycle, adapter templating) | Genuine duplication, but 2 brokers × 6 modules is the cost of supporting 2 brokers. Rung 1. Not now. |
| REF-19/20/21 (strangle `trading/`, delete shims) | 429 files, many untracked, weeks of work, **zero defects fixed**. Rung 1 + risk of catastrophic regression. Explicitly deferred. |
| REF-14/15 (cycles, manifests) | Real, but fixing the cycle *correctly* is a design task, not a mechanical one. Deferred with a note. |
| REF-8 (fee dedup) | Already retracted as a non-defect. **Dropped entirely.** |

**What survives is 4 units, 17 files, all of which fix either a live bug or a
verified one-line divergence.** That is the honest minimum.

---

## Wave 1 — sequential (the regression net) — ✅ COMPLETE, VERIFIED

### W2 · Close the enforcement gaps — REF-2 — ✅ DONE
`_SRC_DIRS` widened 3 → 17 roots. `KNOWN_CYCLES` allowlist (4 rings) + 3 new tests
(`test_no_unknown_import_cycles_between_packages`, `test_known_import_cycles_all_still_exist`,
`test_import_cycle_scan_roots_are_not_empty`). `test_import_boundaries.py` 587 → 769 lines.

**Verification performed:**
- Guard suite: 23 → **26 passed**
- Full collection: 3856 → **3871** (+15)
- **Mutation-tested the guard**: injected `analytics↔execution` and `config↔domain` cycles;
  both correctly flagged as uncovered. The guard is **not vacuous** — a new cycle fails the build.
- The longer allowlist rings correctly cover the shorter ones via
  `_cycle_contains` + `members <= set(ring)`, so one entry cannot excuse an unrelated cycle.

**New finding the widened scan surfaced (1 violation, now fixed):**
`runtime/src/tradex_runtime/startup.py:739` assigned `session._strategy_registry` — a
foreign private write. Only consumer was a test reading it back.
Fix: added a `strategy_registry` constructor param + public `strategy_registry()` accessor
to `TradingSession` (following the file's existing `[REF-5]` convention), passed it from
`startup.py`, and updated 9 test references. No allowlist entry needed.
Verified: 8/8 lifecycle tests pass, guard suite 26 passed, 525 passed across
`runtime/ sdk/ contracts/`.

---

## Wave 2 — parallel (4 agents, provably disjoint file sets) — ✅ COMPLETE, VERIFIED

Disjointness verified mechanically: **0 collisions across 17 files.** All 4 ran
concurrently with a "no git mutations" constraint.

| Agent | Fix | Verified by coordinator |
|---|---|---|
| W1 | 2 backfill scripts (live `ValueError`) | Guard **intact** (`len(brokers)!=1` untouched); both scripts now build one fetcher per broker: `{name: broker}` |
| W3 | timeframe + calendar constants | All 3 private maps deleted; grep for dict literals returns nothing; 5 new adoption tests |
| W4 | IST identity (12 sites, 7 files) | New `domain/timezones.py`; 13 new product/route tests green; no re-derivation remains |
| W5 | dropped `product` field | `_PRODUCT_BY_WIRE` closed map, **422 on unknown**; 13 new tests |

**Final state:** **3889 passed, 3 skipped, 0 failed** · guards **26 passed** ·
collection 3856 → 3891.

### Coordinator correction — a real bug the agents missed

W3 made `W1` resolvable via the canonical registry, which broke
`test_unsupported_timeframe_raises` (it asserted `_agg(Timeframe.W1)` raises).

**The test was right; the naive fix was wrong.** Making `W1` "work" by flooring
to a 604800s epoch multiple produced `bucket_start(Wed 2026-07-15) -> 2026-07-09`,
a **Thursday** — every weekly bar silently misaligned. 604800 divides evenly into
7 days, but the IST epoch origin is a Thursday, so an epoch floor lands mid-week.

Fixed in `bar_aggregator.bucket_start`: weekly buckets now floor to IST
**Monday 00:00** via `midnight - timedelta(days=midnight.weekday())`, alongside the
existing daily special case. W3 had also left `_WEEK_SECONDS` undefined and
`timedelta` unimported — both fixed.

Replaced the obsolete test with `test_weekly_floor_uses_ist_monday_midnight`
(Wednesday, Sunday, and Monday-boundary cases). `_tf_seconds` is now imported so the
"unsupported timeframe raises" contract is still pinned on a genuinely invalid input.

### Straggler closed
`analytics/studies/studies_complex.py:193` still re-derived `IST_OFFSET` — outside
W4's assigned file list, so it was correctly left alone. Fixed by the coordinator:
now imports `IST_OFFSET_SECONDS` from `domain/timezones.py`.

### W1 · Fix the two broken backfill scripts — REF-1
`ParallelHistoryFetcher.__init__` (`market_data/.../parallel_fetcher.py:184`) raises
unless `len(brokers) == 1`. Both scripts pass a multi-broker dict.

Root-cause fix, not symptom: the scripts want per-broker fetchers, the fetcher wants
exactly one broker. Fix by constructing **one fetcher per broker** at the call site
and merging results — do not weaken the guard (the single-broker invariant is load-
bearing for the rate limiter).

Add a test that constructs `ParallelHistoryFetcher` with two brokers and asserts the
`ValueError`, so the contract is pinned.

**Files:** `trading/scripts/backfill_2025.py`, `trading/scripts/backfill_parquet.py`, `trading/tests/datalake/test_parallel_fetcher.py`
**Verify:** `python -m pytest trading/tests/datalake -q` → 138 + new pass. Each script `--help` still runs.

### W3 · Adopt `domain/timeframe.py` + `domain/market_calendar.py` — REF-4 + REF-5
**Verified seam:** `from tradex_domain.timeframe import bucket_seconds` works and
`bucket_seconds(Timeframe.W1)` returns **604800** (confirmed by execution). The three
private copies in `runtime/` lack `W1` entirely, so a weekly request raises where the
registry answers. Delete the duplicates, call `bucket_seconds()`.

Same task: `feed_integrity.py:120` re-inlines `(time(9,15), time(15,30))` and two
scripts re-inline session times. Import from `domain.market_calendar`. The SQL
literals in `datalake_stats.py` become f-strings from `MARKET_OPEN_STR`/`MARKET_CLOSE_STR`
— assert the generated SQL is byte-identical to today's.

**Files:** `runtime/.../bar_aggregator.py`, `feed_recovery.py`, `feed_integrity.py`, `trading/scripts/seed_e2e_datalake.py`, `trading/scripts/datalake_stats.py`
**Verify:** `python -m pytest runtime/tests trading/tests/runtime -q`; add a `W1 → 604800` test through the runtime path.

### W4 · Single IST identity — REF-6
**Verified sites (all Python, exact lines):**
`analytics/.../studies_complex.py:193` · `analytics/.../seasonality.py:17` ·
`analytics/.../profiles.py:15,16` · `brokers/.../dhan/tick_parser.py:46` ·
`interfaces/.../replay_run.py:13` · `interfaces/.../routes/stream_indicators.py:23,72` ·
`interfaces/.../routes/chart.py:42` · `interfaces/.../routes/stream.py:715,905,928,930,973,1140`

`domain.market_calendar.IST` exists; **nothing outside `domain` imports it.**
`stream_indicators.py` is the worst case: `_IST = "Asia/Kolkata"` at L23, then
`timezone(timedelta(hours=5, minutes=30))` at L72 — the same zone, two
representations, one file.

Add `domain/timezones.py` exposing `IST_ZONE` (`ZoneInfo`) and `IST_OFFSET_SECONDS`
alongside the existing fixed-offset `IST`. Replace all Python sites.
`frontend/src/feed.ts` and `openalgo-charts/**` are **out of scope** — TS cannot
import Python, and the generated-contract fix (REF-10) was cut. Leave them; the
audit records the debt.

**Files:** `analytics/.../profiles.py`, `analytics/.../seasonality.py`, `brokers/.../dhan/tick_parser.py`, `interfaces/.../routes/stream_indicators.py`, `routes/chart.py`, `replay_run.py`, `routes/stream.py`
**Verify:** `python -m pytest trading/tests/interface trading/tests/analytics -q`; assert route timestamps unchanged.

### W5 · `product` field — REF-11 (collapsed)
**Verified seam:** `OrderRequest.product_type: ProductType = ProductType.INTRADAY` already
exists (`domain/execution.py:75,152`). `ProductType` has 5 members: INTRADAY, DELIVERY,
MARGIN, MTF, COVER_ORDER. **No existing test covers `product`** in the route layer.

`routes/orders.py:159-168` and `:216-231` build requests with **no `product`
argument**; `grep -n "product" routes/orders.py` returns zero hits. The UI sends
`product: 'MIS'` and renders a live selector. The selector does nothing.

Read the field, map `MIS/CNC/NRML → ProductType.INTRADAY/DELIVERY/MTF` via a small
explicit map, and **422 on an unknown value** rather than silently defaulting — a
dropped product is how a real order goes out with the wrong margin treatment.

**Files:** `interfaces/src/tradex_interfaces/routes/orders.py` + its route tests
**Verify:** order-submission test asserting the broker receives the selected product; unknown product → 422.

---

## Global constraints (binding on every agent)

1. **No git mutations.** No `add`/`commit`/`checkout`/`stash`/`restore`. The tree has
   432 pre-existing uncommitted changes; touching git would destroy attribution.
2. **No behaviour changes** outside the stated scope. Goldens, parity fixtures, and
   3856 collected tests must stay green.
3. **Reuse before adding.** If `domain/` already exports the constant, import it. Do
   not add a new module when an existing one answers the question.
4. **Mark shortcuts** with a `ponytail:` comment naming the ceiling.
5. **One runnable check** per non-trivial change. No new frameworks, no fixtures.
6. **Do not touch files outside your assigned list.** Another agent owns them.
7. If a task reveals the plan is wrong, **stop and report** — do not improvise scope.

---

## Sequencing

```
W2 (guards, alone)          ← regression net, file already dirty
   ↓
W1 ─ W3 ─ W4 ─ W5           ← 4 agents in parallel, disjoint files
   ↓
coordinator: full suite + re-verify
```

## Wave 3 — two more units (parallel, disjoint) — ✅ COMPLETE, VERIFIED

### W6 · Break 2 of 4 import cycles — REF-14 (partial)
`NSETradingCalendar` moved to `domain/trading_calendar.py` (stdlib-only; it only ever
imported `domain.market_calendar` — a pure domain concept in the wrong package).
`runtime/calendar.py` is now a re-export shim; all 4 import paths resolve to the
**identical class object**.

**Cycles 4 → 2** (recomputed independently, not taken on trust):
- BROKEN: `market_data → runtime → market_data`
- BROKEN: `market_data → runtime → strategy → market_data`
- REMAIN (legitimately): `market_data → replay → market_data`, `market_data → replay → strategy → market_data`

`KNOWN_CYCLES` allowlist 4 → 2.

**W6 correctly refused part of its task.** I told it to delete `ParquetBacktestLoader.run()`
(an unused convenience method importing `tradex_replay` lazily). It found **4 live passing
test callers** plus `README.md:199` and a docstring reference — my original grep searched
for `run_backtest` instead of the real call site `loader.run(`, so I had told it the method
was unused when it is not. It left it alone and reported the premise error. Good judgement.

### W7 · Unify the split-brain runtime dir — REF (new)
`TRADEX_RUNTIME_DIR` resolved to **two different directories** when unset: config said
`.tradex_v4` (cwd-relative, the exact bug `brokers/common/paths.py` was written to close),
brokers said `<repo>/runtime`. New stdlib-only `domain/paths.py` is the single resolver.

Verified independently: all three layers return the identical path with the var unset, and
all three honour an override.

**W7 deviated from my instructions, correctly.** I specified a module-level constant; it
used a lazy `default_runtime_dir_str()` + `field(default_factory=...)` because a constant
captures the env var at *import time* and goes stale — partially re-opening the very split
brain the fix exists to close. It also **corrected my `parents[N]`**: I said `parents[4]`,
the correct depth for `domain/src/tradex_domain/paths.py` is `parents[3]`.

---

## Coordinator fix — live-fire hazard (found by an agent, verified and closed)

W1's verification step **`backfill_2025.py --help` fired a real live backfill.**
`backfill_2025.py` has no argparse, and its quarter filter
`[a for a in argv[1:] if not a.startswith("--")]` silently swallows `--help`, making the
command indistinguishable from a real run. It connected to live Dhan + Upstox and began
backfilling before being killed (~342 gitignored, idempotently-upserted parquet files; no
corruption; progress file still `{"done":["Q1"]}`).

Fixed: a `--help`/`-h` guard returning **before any broker connection**, plus argv
validation (unknown quarter) moved ahead of the connect. Verified: `--help` exit 0 with
**0 connections**, `Q9` exit 1 with **0 connections**, script compiles.

**Stale fossil found, NOT deleted:** `../runtime/` (dated 2026-08-05) holds a `dhan/` dir
and a stale token-state lock from launches made from the parent cwd — a fossil of this
exact bug. Deleting it is a user decision (it may hold live tokens), so it is flagged only.

---

## Wave 4 — four more units (parallel, disjoint) — ✅ COMPLETE, VERIFIED

**A collision was created and caught mid-flight:** W8 (fee rounding) and W11 (enum identity)
both targeted `execution/.../fees.py`. Disjointness was re-proven mechanically after
rescoping W11 (4 units, 9 files, 0 collisions). Both agents respected the boundary:
W8 did the rounding, W11 left the file alone, and the coordinator made the deferred
one-line enum change after W8 finished. **No lost work.**

| Unit | Fix | Independently verified |
|---|---|---|
| W8 | `fees.py` money rounding → `q2` | 0 bare `quantize` remain; W8 found only **4** real sites (brief said 5 — line 172 was already `q2`) |
| W9 | Datalake root defaults | 0 hardcoded roots; 3 constructors agree, absolute, **cwd-independent** (tested via `os.chdir`) |
| W10 | CORS/CSRF origin split | **12/12 state×env combinations** produce the identical value to the old CSRF chain |
| W11 | Enum identity on side checks | `side.value == "BUY"` gone from `domain/` + `execution/` |

### W8 — the fee rounding bug, precisely characterised
The reachable divergence is **not** where the brief implied. `breakdown.total` is always
already 2dp-exact (every component goes through `q2`), so lines 119/174 could not
discriminate. The live bug was `total_cost`, where `price × quantity` is arbitrary
precision: price `1.005` × qty `1` was `1.00` (banker's) instead of `1.01` (half-up).

**No golden or fixture file shifted.** W8 mutation-tested its own 16 new tests against
the reverted buggy code and confirmed the 4 `total_cost` assertions genuinely fail
without the fix. It also chased a transient 2-test failure to root cause (concurrent
file writes by W11) rather than hand-waving, confirming 8/8 clean repeat runs.

### W10 — security claim proven exhaustively
The brief said: never ship something more permissive than the current CSRF check.
I verified that by enumerating every combination of `app.state.ui_origin` ×
`TRADEX_UI_ORIGIN` (including `None` and `""`) against the verbatim pre-existing
`getattr(...) or os.environ.get(...) or DEFAULT` chain: **12/12 identical**. The
unification cannot weaken the policy; it only makes CORS honour `app.state` the way
CSRF already did.

## Wave 5 — the last 2 cycles — ✅ COMPLETE. Import graph is now a DAG.

| Unit | Fix |
|---|---|
| W12 | `universe.py` → `domain/` (stdlib+domain only, a pure concept in the wrong package). `strategy` no longer imports `market_data`. |
| W13 | `loader.run()` **moved, not deleted** → `replay.run_with_loader()`. A data-loading package no longer knows about an execution engine. |

**Coordinator-verified, not taken on trust:**
- `grep tradex_replay market_data/src/` → **no matches**
- AST recomputation of the package graph → **0 cycles**
- `KNOWN_CYCLES` → **`{}`** (empty). The allowlist is retired, not shrunk.
- The one remaining `tradex_market_data` reference in `strategy/` is in
  `multi_symbol_sma_cross.py:7`, which AST proves is **docstring-only** — a phantom edge.

### W13's judgement call worth keeping
The brief suggested `replay.run_with_loader()` could import `ParquetBacktestLoader` for
its annotation. W13 typed the parameter `Any` instead, reasoning that an annotation-only
import still creates an AST edge and `replay → market_data` is already allowed — so the
import would buy nothing while making the scanner noisier. Correct.

## Wave 6 — two pure renames — ✅ COMPLETE, ZERO VALUE DRIFT

| Unit | Constant | Sites |
|---|---|---|
| W14 | `TRADING_DAYS_PER_YEAR = 252`, `SESSION_MINUTES_PER_DAY = 375` in `reports.py` | 22 in `reports.py`, 5 in `engine.py`, 1 default in `volatility.py` |
| W15 | `INSTRUMENT_KEY_EXPIRY_FORMAT = "%Y%m%d"`, `_API_KEY_HEADER = "X-API-Key"` | 3 + 3 |

Both are **wire contracts / financial formulas**, so the rule was: every value stays
byte-identical. Verified directly:
- 21/21 `_periods_per_year` frequencies return the same value; `realized_vol` and the
  full tearsheet field set unchanged
- W14 rewrote `int(252 * 6.25)` as `int(days * (minutes / 60))` so the intraday factor
  derives from `375` instead of restating it. Both compute **1575** — exact, verified.
- `%Y%m%d` now appears exactly once in `domain/` (the constant definition); keys built
  with the old literal still parse and re-serialise identically
- `"X-API-Key"` appears exactly once (the constant definition)

Deliberately **not** merged: the dashed `%Y-%m-%d` format (CLI/datalake) and
`%Y%m%d_%H%M%S` (candle filenames in `market_data/catalog.py:60`) — different contracts.

## Wave 7 — Dhan segments (via the `dhanhq` skill) + datetime format contracts — ✅ COMPLETE

### W16 · Dhan exchange segments — validated against the dhanhq skill
The `dhanhq` skill's SDK-constant table is authoritative ground truth. I verified the
repo's forward map against it **before** changing anything: **7/7 match**
(`NSE_EQ`, `BSE_EQ`, `NSE_FNO`, `BSE_FNO`, `MCX_COMM`, `NSE_CURRENCY`, `IDX_I`).

The forward table is a **strict superset** of the reverse table's value set
(extra: `NSE_COMM`), and all **8** wire-int codes round-trip through it. So the reverse is
now *derived/asserted* against one canonical table in
`brokers/src/tradex_brokers/common/dhan_segments.py` — while the wire ints stay explicit,
because they are a binary-protocol fact, not derivable.

**`SEGMENT_CANONICAL` was correctly left alone** — it is a third, different key space
(`(SEM_EXM_EXCH_ID, SEM_SEGMENT)` CSV char pairs), not a REST segment or a wire int.
Verified: forward 7/7 correct, wire 8/8 round-trip, **wire code 6 still absent** (it was
absent before and remains so — deriving a value for it would have been a behaviour change).

### W17 · Datetime format contracts
New `domain/src/tradex_domain/datetime_formats.py` with `DASHED_DATETIME` and
`DASHED_DATE`, and a docstring stating these are storage/wire contracts that must never be
"tidied" or unified. The compact `%Y%m%d` instrument-key contract stays separate (named
`INSTRUMENT_KEY_EXPIRY_FORMAT` in Wave 6). Verified: constants hold the exact literals, and
the three format families remain distinct.

**W17 corrected my brief.** I told it the tolerant-parsing ladder in `provider_common.py` was
7 entries, taken from the Wave 1 sub-agent's report. It is **9** — the two day-first variants
(`%d-%m-%Y`, `%d/%m/%Y`) and the space+aware variant were missed. It kept all 9 in order and
named only the 2 that genuinely match a family as constants; the ISO "T" variants and the
day-first ones stay literals with per-line reasons.

Coordinator-verified behaviourally: **9/9** formats still parse, **3/3** invalid inputs still
raise `SDKError` (`not-a-timestamp`, `2026-07-15 10:07`, `""`). The reject path matters as
much as the accept path — a "cleanup" that made the ladder greedier would silently start
accepting malformed broker timestamps.

W17 also mutation-tested its own tests (changing a space to "T" → 7 failures; collapsing
`DASHED_DATE` into the compact form → 8 failures), so the guards genuinely bite.

## FINAL STATE — ✅ ALL GREEN
- **4139 passed, 3 skipped, 0 failed** (audit start: 3856; **+283 tests**)
- Architecture guards: **26 passed** (audit start: 23)
- Parity suite: **155 passed** — unchanged across all 7 waves
- **Import cycles: 4 → 0.** The package graph is a DAG; `KNOWN_CYCLES` is `{}`.
- Zero bare `quantize` in fees · zero raw-string side comparisons · zero hardcoded
  datalake roots · zero IST re-derivations · config/brokers runtime dir unified ·
  CORS/CSRF origin single-sourced · annualisation literals named · wire formats named ·
  Dhan segments single-sourced and skill-validated

### Decided and done
- `../runtime/` fossil — inspected, **not** deleted (see below for why)
- `.tradex_v4` — verified it never existed; nothing to clean

### Two items requiring the user's explicit go-ahead
1. **`../runtime/` (parent dir, 2026-08-05).** I inventoried it: 3 broker subdirs, only
   `.lock` files plus two `totp_cooldown.json` stubs, **zero token material**, and a broker
   literally named `x` (a typo-era fork). It is untracked and not gitignored. It is safe to
   remove — but it lives **outside the repo root**, so the destructive-action gate correctly
   stopped me. Deleting it is your call.
2. **Frontend `?api_key=` query-param transport.** Removing it may break external WS
   clients. Needs a deprecation, not a deletion.

### Still not done, deliberately
- **TypeScript-side IST/session constants** — needs the generated wire contract
  (`contracts/wire_contract.json` -> TS + Python). Cut as overkill; it is the one remaining
  cross-language shotgun-surgery site.
- **Nothing is committed** — 400+ uncommitted changes across 7 waves. This is now the
  single largest risk to the work.

### Things the agents got right that I did not
- W6 rejected a deletion I ordered because the premise was wrong
- W7 corrected my `parents[N]` and my laziness-vs-staleness analysis
- W8 found 4 real rounding sites, not the 5 I claimed
- W1 disclosed that its own verification command caused live broker traffic
- **W17 corrected my brief: the parse ladder is 9 entries, not the 7 I passed down**

### Left undone, deliberately
- `market_data → replay` cycles: need the loader to stop importing `tradex_replay`, which
  means relocating `run()` and its 4 tests, not deleting it
- TS-side IST/session constants: need the generated wire contract (cut as overkill)
- `.tradex_v4` may be orphaned; `../runtime/` fossil needs a user decision
