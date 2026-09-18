# v4 — kanban digest (2026-09-18T15:33:00Z)

## Work in progress
- none

## Blocked
- none

## Planned / backlog
- none

## Recently completed
- **WIP landed.** All 216 uncommitted working-tree changes across three
  efforts are now committed and the tree is clean. Each effort was verified
  by execution before its commit.
  1. Position accounting deepened into `PositionAccountant` (46 position tests).
  2. Broker-specific knowledge moved out of `common/` into per-broker packages
     (789 brokers tests).
  3. `tradex sync` CLI + frontend runs-bar feature + backtest leg surfacing
     + analytics `trade_metrics` + datalake gap/clip hardening + bracket
     protection extraction.
- **Bug fixed:** `test_request_spacing(spacing_seconds)` was a helper
  misnamed as a test, so pytest treated its parameter as a missing fixture
  and errored. Renamed to `run_request_spacing`.

## Tests
- last pytest run: 3176 passed, 5 skipped (full suite: domain + brokers +
  trading + root)
- frontend: `tsc --noEmit` clean, `vite build` succeeds (25 modules)

## Recent commits
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
- **pytest invocation:** run `.venv/bin/python -m pytest <paths> -p
  no:cacheprovider --import-mode=importlib -c pyproject.toml`. Without `-c
  pyproject.toml`, pytest's rootdir discovery walks up to the parent
  directory's `pyproject.toml` and hits a sandbox PermissionError on
  `/Users/apple/Downloads/v2-cleanup-.../pyproject.toml`.
- **Do not delete untracked files.** The earlier `simple_sync.py` deletion
  plan was cancelled: those were uncommitted WIP, not shipped code. They
  are now committed, but the caution stands for any new untracked file.
