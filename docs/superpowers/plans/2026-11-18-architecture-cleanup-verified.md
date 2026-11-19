# Architecture Cleanup (verified findings only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplication and dead code that the 2026-11-18 audit and scans confirmed in the code, without changing any output a user can see, except where a task says so.

**Architecture:** Each task is one small, behaviour-preserving change with its own commit. Tasks 1, 2, 4 are mechanical (same results before and after, pinned by a test written first). Task 3 changes backtest numbers and needs the user's call before it runs.

**Tech Stack:** Python 3.13 monorepo, `.venv/bin/python -m pytest`, `ruff` (only linter installed).

**Spec:** No design doc. Evidence is the audit in this session plus these scans: `$TMPDIR/scan/dups.py` (AST-identical function bodies, 6 groups), `$TMPDIR/scan/dead.py` (top-level symbols named once in the whole tree), `$TMPDIR/scan/rsi.py` (strategy RSI 51.50 vs analytics RSI 53.87 on one seeded series).

## Global Constraints

- The shared `.venv` is an editable install of the main checkout: do the work **in place** on `integration/wave1`, not in a worktree (CLAUDE.md lesson 1).
- No new dependency. No behaviour change in Tasks 1, 2, 4, 5: the package's existing tests must pass unchanged, and each task adds a test that fails if the behaviour moves.
- Before deleting or moving any name, `git grep -n "<name>"` across `*.py`, docs and config, including string references (CLAUDE.md lesson 2).
- Each task ends green on its package's tests and is its own commit. `git add` only that task's files; the tree has unrelated untracked files.
- `.git/index.lock` writes need the sandbox disabled.

## Review Focus

- A moved helper keeps its exact gap and `None` handling: the two EMA copies must stay byte-identical in output on input with a `None` gap (Task 2).
- `IST` alias keeps `tzinfo` equality: code comparing `ts.tzinfo == IST` still holds, and `to_ist_naive` output is unchanged (Task 1).
- Importing `_builtin_specs` from its new module does not create an import cycle at package import time (Task 4).
- A "dead" function is not reached by a decorator, `getattr` or a string name (Task 5).

---

### Task 1: One IST definition

**Files:**
- Modify: `domain/src/tradex_domain/market_calendar.py:28` (old `IST`)
- Modify: `domain/src/tradex_domain/timezones.py` (the kept definition, `IST_ZONE`)
- Modify: `runtime/src/tradex_runtime/feed_integrity.py:33`, `live_fill_bridge.py:74`, `tape_recorder.py:102`; `market_data/src/tradex_market_data/parquet_storage.py:127-129`, `gap_detector.py:40-41` (local aliases)
- Test: `domain/tests/` (new `test_ist_single_definition.py`)

**Interfaces:**
- Produces: `tradex_domain.market_calendar.IST is tradex_domain.timezones.IST_ZONE` (same object); no local `_MARKET_*`/`IST` re-definitions in the files above.

- [ ] **Step 1: Failing test** — `test_market_calendar_ist_is_the_timezones_object`: `assert market_calendar.IST is timezones.IST_ZONE`; and `test_to_ist_naive_unchanged`: `to_ist_naive(datetime(2026,5,4,3,45,tzinfo=UTC)) == datetime(2026,5,4,9,15)`.
- [ ] **Step 2: Run, expect the identity test to FAIL** — `.venv/bin/python -m pytest domain/tests/test_ist_single_definition.py -q`. If both are already the same object, the finding is void: record that in the commit message and stop the task.
- [ ] **Step 3: Implement** — in `market_calendar.py` replace the `IST = timezone(...)` definition with `from tradex_domain.timezones import IST_ZONE as IST`. In each listed file, delete the local alias and import from `tradex_domain.market_calendar` / `timezones`. Do not change `scripts/generate_fixtures.py` here (outside `src/`).
- [ ] **Step 4: Run** — `.venv/bin/python -m pytest domain/tests market_data/tests runtime/tests -q` and `trading/tests/datalake -q`. Expected: same pass count as before the change.
- [ ] **Step 5: Commit** — `refactor: one IST definition, drop local session-constant aliases`.

### Task 2: Remove exact-duplicate indicator helpers

**Files:**
- Modify: `analytics/src/tradex_analytics/studies/studies_complex.py:154` (`_seeded_ema`), `studies/studies_simple.py:138` (`_vwma`)
- Keep: `analytics/src/tradex_analytics/_math.py:418` (`_seeded_ema`), `:459` (`_vwma`)
- Test: `analytics/tests/` (new `test_math_dedup.py`)

**Interfaces:**
- Produces: `studies_complex` and `studies_simple` import `_seeded_ema` / `_vwma` from `tradex_analytics._math`. The scan found these bodies AST-identical to the `_math` ones, so output must not change.

- [ ] **Step 1: Failing test first** — `test_seeded_ema_and_vwma_pinned`: call each study's current helper on a fixed series that includes a `None` gap and a zero-volume bar, assert the exact lists (capture them from the current code before editing). Run it: it passes now and must still pass after.
- [ ] **Step 2: Implement** — delete the two local functions and add `from tradex_analytics._math import _seeded_ema` (and `_vwma`) at the top of each study module.
- [ ] **Step 3: Run** — `.venv/bin/python -m pytest analytics trading/tests/analytics -q`. Expected: same pass count, goldens unchanged (`git diff --stat trading/tests/analytics/goldens` empty).
- [ ] **Step 4: Commit** — `refactor(analytics): reuse _math seeded EMA and VWMA`.

Note: the two other EMA helpers (`indicators._sma_seeded_ema` vs `_math._sma_seeded_ema_gapped`) are **not** identical. One skips a `None` and continues, the other stops at the first gap. They are out of scope here (see "Not planned").

### Task 3: Strategies use analytics RSI and ATR — NEEDS USER DECISION

**Decision owed before this task runs.** `mean_reversion._rsi` and `bracket_breakout._atr` are plain averages, on purpose ("reference-grade, not Wilder's"). Switching them to `tradex_analytics.indicators.rsi` / `atr` (Wilder-smoothed) makes backtest signals match the charts, but **changes backtest results** for these two strategies (measured: RSI 51.50 vs 53.87 on one seeded series). Options: (a) switch and accept new numbers; (b) keep the plain version and rename it so it is not confused with chart RSI; (c) leave as is. Do not start without a choice.

**Files (option a):**
- Modify: `strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py:141`, `bracket_breakout.py:228`
- Test: `strategy/tests/` (new `test_indicator_parity.py`)

- [ ] **Step 1: Failing test** — assert the strategy's RSI/ATR on a fixed series equals `analytics.rsi(...)[-1]` / `analytics.atr(...)[-1]` within 1e-9. Fails today.
- [ ] **Step 2: Implement** — call the analytics function on the strategy's stored closes/candles, return the last value.
- [ ] **Step 3: Run** — `.venv/bin/python -m pytest strategy trading/tests -q -k "mean_reversion or bracket"`; update the expected values in any strategy test that pinned the old numbers, and list each changed expectation in the commit message.
- [ ] **Step 4: Commit** — `fix(strategy): mean_reversion RSI and bracket ATR use analytics Wilder versions`.

### Task 4: Move indicator parameter schemas out of `indicators.py`

**Files:**
- Create: `analytics/src/tradex_analytics/indicator_specs.py` (moves `_builtin_specs`, currently `indicators.py:1038-1532`)
- Modify: `analytics/src/tradex_analytics/indicators.py` (remove it and the mid-file `REGISTRY` import at `:892` if the move makes it unnecessary)
- Test: `analytics/tests/` (new `test_indicator_catalogue_stable.py`)

**Interfaces:**
- Produces: `indicator_catalogue()` returns exactly the same list as before. Public names `register_indicator`, `compute_indicator`, `get_indicator_spec`, `validate_indicator_request` still import from `tradex_analytics.indicators`.

- [ ] **Step 1: Failing test first** — `test_indicator_catalogue_matches_snapshot`: dump `indicator_catalogue()` to JSON and compare with a snapshot file saved from the current code. Passes now, must pass after.
- [ ] **Step 2: Implement** — move the function and its helpers; import it where `indicators.py` builds the registry. If an import cycle appears, keep the lazy import and say so in a comment.
- [ ] **Step 3: Run** — `.venv/bin/python -c "import tradex_analytics.indicators"` then `.venv/bin/python -m pytest analytics trading/tests/analytics -q`.
- [ ] **Step 4: Commit** — `refactor(analytics): move built-in indicator specs to their own module`.

### Task 5: Delete three confirmed-unreferenced private functions

**Files:**
- Modify: `analytics/src/tradex_analytics/_round_trips.py:302` (`_executed`, 24 lines), `market_data/src/tradex_market_data/parallel_fetcher.py:171` (`_provider_for`, 9 lines), `execution/src/tradex_execution/reconciliation.py:594` (`_cleared_position`, 7 lines)

- [ ] **Step 1: Re-verify** — for each name run `git grep -n "<name>"` over everything including docs and `getattr`/string use. Expected: only the definition line (the 2026-11-18 scan found exactly one occurrence each). Any other hit: drop that name from this task.
- [ ] **Step 2: Delete** the confirmed ones and any import that becomes unused (`.venv/bin/ruff check <file>`).
- [ ] **Step 3: Run** — `.venv/bin/python -m pytest analytics execution market_data -q`.
- [ ] **Step 4: Commit** — `chore: remove three unreferenced private helpers`.

---

## Not planned (with the reason)

- **Share one quote/chain serializer between gateway and interfaces.** Checked: the two quote shapes are different contracts (`ltp`/`ts` epoch milliseconds/`broker`/`oi`/`ohlc` in the gateway; `last_price`/ISO `timestamp` in interfaces). Merging them would break one set of clients. At most the field extraction could be shared, and that saves little. Finding 2 is downgraded.
- **`BrokerRateLimited`, `FeedUnavailable`** (gateway `errors.py`): unreferenced, but they are part of the error vocabulary. Kept.
- **FastAPI route functions and MCP tools flagged by the dead-code scan** (`get_future_chain`, `expired_options`, `run_screener` and similar): registered by decorator, not dead.
- **The two non-identical EMA helpers; the Dhan/Upstox `history()` copy; `_ensure_ws` copies in the broker streams** (the scan did find three near-identical bodies, 24 lines each): need their own per-case diff and a decision on gap policy.
- **God files** (`startup.py`, `ws_stream`, `chart.py`, `engine.py`) and **9 `FakeBroker` classes**: each deserves its own plan after a per-file read. The fakes differ in shape, so merging them blindly would break tests.
- **Auth, rate limiter and metrics duplicated across the two servers:** security-touching, needs its own plan and the user's call on the gateway's live-bind guard.
- **`vulture`**: could not run (the package cache path is blocked in the sandbox). The scan above is a home-made substitute and finds only top-level names.

## Self-review

- Coverage: findings 8 (T1), 10-part (T2), 7 (T3), 10-split (T4), dead code (T5). Others are listed under Not planned with a reason.
- Names consistent: `IST_ZONE`, `_seeded_ema`, `_vwma`, `_builtin_specs`, `indicator_catalogue`.
- Unverified assumptions the implementer must confirm in step 1: that `market_calendar.IST` and `IST_ZONE` are not already the same object, that the three `_seeded_ema`/`_vwma` copies are still identical, and that an import cycle does not block Task 4.
- Proportion: five tasks, each a small edit; the plan describes decisions and tests, not the code.
