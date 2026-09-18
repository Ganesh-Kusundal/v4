# v4 — kanban digest (2026-09-18T16:06:00Z)

## Work in progress
- none

## Blocked
- none

## Planned / backlog
- none

## Recently completed

### Architecture review roadmap — all six candidates resolved

The 2026-09-17 review listed six deepening candidates. Verified against source
and by test, then worked:

1. **Collapse the Execution Engine into a thin orchestrator** — DONE.
   `FeeCalculator.calculate_capped` is consulted, fill dedup is `FillDedup` in
   `idempotency.py`, the inline LRU is gone (`d10a157`). Engine is 886 LOC with
   the bulk in `_run_pipeline`/`cancel`/`modify`; not the ~200 the review
   sketched, but the delegation it asked for is in place.
2. **Unify the Datalake dual sync and fetch paths** — DONE (`d1fe277`).
   `simple_fetcher.py` deleted (155 LOC of duplication). `simple_sync` now
   fetches through `ParallelHistoryFetcher`, so the single path inherits
   multi-broker failover and clipped-tail repair. `fill_gaps`, `repair_gaps`
   and `sync_today` call it, handing the other broker in as failover.
   `SyncOrchestrator` stays for the parity benchmark, documented as superseded.
   7 new tests pin the contracts, including the IPO-skip semantics the
   unification had to preserve.
3. **Push broker-specific knowledge out of common** — DONE (`d7a13ca`).
   Rate tables live in per-broker `rate_table.py`; `common/` lazily imports them
   and re-exports for backward compat.
4. **Unify Paper Broker under the BaseBroker seam** — DONE (`d09c254`).
   `PaperBroker(BaseBroker)`, overriding only the fill engine, cash ledger and
   market sim. The failure mode changed, not the safety: extension methods are
   now present but capability-gated, verified to fire before args are consulted.
5. **Deepen the Position Accounting module** — DONE (`9155d90`).
   `PositionAccountant` is the seam; `position_math.py` shim deleted.
6. **Route shutdown through the ShutdownCoordinator** — DONE (`10d05c2`).
   The writer lock is constructed by the session it protects; the
   `session.stop = _stop_and_release` monkey-patch is gone.

### Also landed this session
- `6bac231` renamed `test_request_spacing` (a helper misnamed as a test that
  broke pytest collection).
- `6f6ae7b` added the `tradex sync` CLI; `--dry-run` swaps in the paper broker.
- `d359e41` + `8f489d5` added the frontend runs bar, panel surfaces and the
  live chart host shell.
- `823cf09` surfaced stop/target legs on recorded backtest fills.
- `937c00e` added `trade_metrics` (round-trip trades + statistics + sortino).
- `74d5047` classified session-edge stamps and detects clipped fetch tails.
- `b3be342` exposed `bracket_breakout` and strategy param defaults in chart.
- `098d5df` widened the duckdb query guards.
- `b240264` + `9af50f3` covered the operator scripts; added the top-gainers app.
- `89bb37a` paper-mode quote fallback to the datalake's last close.

## Tests
- last full suite: **3185 passed, 3 skipped**
  (domain + brokers + trading + root)
- brokers suite: 789 passed / 2 skipped
- frontend: `tsc --noEmit` clean, `vite build` succeeds

## Recent commits
- 8f489d5 feat(frontend): live chart host shell with api-key field and theme
- 89bb37a feat(stream): paper-mode quote fallback to the datalake's last close
- d09c254 refactor(brokers): PaperBroker extends BaseBroker (candidate 4)
- d1fe277 refactor(datalake): unify the dual sync paths onto simple_sync
- 1e513fc docs(kanban): record the WIP landing and the pytest invocation note
- 51b76ec chore: gitignore reconcile backups and runtime logs
- 9af50f3 feat(apps): top-gainers Streamlit viewer over the datalake
- b240264 test(scripts): cover the datalake audit, backfill, stats and reconcile helpers
- 10d05c2 chore: session owns the writer lock, add frontend CI gate
- 098d5df fix(duckdb-analytics): widen query guards and tighten catalog typing
- b3be342 feat(chart): expose bracket_breakout and strategy parameter defaults
- 74d5047 fix(datalake): classify session-edge stamps and detect clipped fetch tails
- d10a157 refactor(execution): decompose engine spine and extract bracket protection
- 937c00e feat(analytics): add round-trip trade derivation and statistics block
- 823cf09 feat(backtest): surface stop/target legs on recorded fills
- d359e41 feat(frontend): add backtest runs bar and panel surfaces
- 6f6ae7b feat(cli): add 'tradex sync' command for historical OHLCV backfill
- d7a13ca refactor(brokers): move broker-specific knowledge out of common/
- 9155d90 refactor(execution): deepen position accounting into PositionAccountant
- 6bac231 fix: rename test_request_spacing helper that broke pytest collection

## Architecture & components
- none

## Module dependencies (scalpr, module → imports)
- none

## Knowledge graph (graphify)
- 12510 nodes · 34591 edges · 477 communities (built 2026-09-18T13:37:00Z)

## Notes for the next session
- **pytest invocation:** `.venv/bin/python -m pytest <paths> -p
  no:cacheprovider --import-mode=importlib -c pyproject.toml`. Without `-c
  pyproject.toml`, rootdir discovery walks up to the parent directory's
  pyproject.toml and hits a sandbox PermissionError on
  `/Users/apple/Downloads/v2-cleanup-.../pyproject.toml`.
- **Do not delete untracked files.** The earlier `simple_sync.py` deletion plan
  was cancelled because those were uncommitted WIP. This session those same
  files became trackable (deletion is now recoverable) and the duplicate
  `simple_fetcher.py` was deleted deliberately — but the caution stands for any
  *new* untracked file.
- **Candidate 1 is partially open.** The delegation the review asked for is in
  place, but the engine is still 886 LOC; `_run_pipeline` (178), `modify` (93)
  and `cancel` (86) carry most of it. A follow-up could extract those.
