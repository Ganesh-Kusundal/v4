# Cleanup Overhaul — Design Spec

**Date:** 2026-09-22
**Branch:** `chore/cleanup-overhaul` (to be cut from `refactor/simple-sync-consolidation` @ `0b2ce8e`)
**Source:** architecture review report `architecture-review-20260922-130000.html` (6 candidates)
**Status:** approved for planning

## Goal

Make the v4 repository clean and neat: remove dead tree mass, eliminate duplicate
state roots, collapse shallow order-persistence modules, consolidate operator
scripts, and re-cut analytics files by concept. One commit per phase, each
independently revertible.

## Constraints (carried from `.kanban/CONTEXT.md`)

- **Never delete untracked files.** Every deletion target must be either
  (a) gitignored **and** rebuildable, or (b) tracked and verified by grep/audit +
  green tests before removal.
- **Test invocation:** `.venv/bin/python -m pytest <paths> -p no:cacheprovider
  --import-mode=importlib -c pyproject.toml`. Without `-c pyproject.toml`,
  rootdir discovery walks up to the parent directory and hits a sandbox
  PermissionError.
- **Baseline:** 3185 passed / 3 skipped (domain + brokers + trading + root);
  brokers 789 passed / 2 skipped; frontend `tsc --noEmit` clean.
- **Dependency direction unchanged:** `domain ← brokers ← trading` (never reverse).
- The 2026-09-17 review's six candidates are done; this spec does not re-litigate them.
- Vocabulary: `CLAUDE.md` is the domain guide (no root `CONTEXT.md`, no ADRs).

## Phase 0 — Prep

1. Cut `chore/cleanup-overhaul` from current HEAD.
2. Run the full suite; record the baseline count in the commit message of Phase 1.
3. No code changes.

## Phase 1 — Purge dead tree mass (candidate 1)

**Delete (gitignored, rebuildable — zero code edits):**

| Target | Size | Rebuild path |
|---|---|---|
| `.freebuff/` | 23 MB | tool scratch; never regenerated into the repo's flow |
| `graphify-out/` | 57 MB | regenerate via graphify |
| `__pycache__/` at repo root | — | recreated by pytest |
| `**/.benchmarks/` (5 locations) | — | recreated by pytest-benchmark |
| `**/.DS_Store` | — | OS noise; already gitignored |
| `frontend/test-results/` | — | Playwright rerun |
| `poc/data/*.parquet` (keep tracked `build_0950_dataset.py`) | — | dataset builder script |
| `trading/runtime/` stale dumps (`*-2026-09-02.json`, `*-2026-09-03.json`) | — | broker connect regenerates |

**Move (tracked → git mv, history preserved):**

- `research/scan_0945_forward.py`, `research/vp_strategy_backtest.py` → `poc/research/`
- `services/duckdb-analytics/research/*` (14 files) → `poc/research/duckdb/`
- **`.gitignore` must be updated in the same commit:** the `poc/*` ignore with
  whitelist (`!poc/README.md`, `!poc/rank_0950.py`, …) would silently untrack the
  moved files. Replace the per-file whitelist with `!poc/**/*.py` (keep
  `poc/data/*` ignored except `build_0950_dataset.py`), then `git add` the moves
  and verify `git status` shows them tracked.
- Fix the two known references: `_simulate_vp` import in
  `vp_strategy_backtest.py` (Streamlit app path), and any `research/` paths in
  docs/tests. Grep for `research/` before committing.
- Remove the now-empty `research/` directory and the
  `services/duckdb-analytics/research/` entry.

**Deferred (audit evidence recorded here for the later pass):**

- `trading/data/ohlcv/` (277 MB shadow lake) — **stays on disk** per owner policy.
  Audit result (2026-09-22): zero code reads it. `datalake/paths.py` anchors the
  real lake at `<repo>/data` via `Path(__file__).parents[4]` + `TRADEX_DATALAKE_ROOT`;
  the only textual reference is the docstring of
  `trading/tests/interface/test_datalake_root.py`, which documents it as the
  *nonexistent* directory from the 2026-09-02 cwd bug. The shadow lake is residue
  **created by** that bug (a process cwd'd into `trading/` writing relative
  `data/`). Deletion is safe once the owner gives the second yes.

**Guard:** `.gitignore` already covers every deleted path; verify no rule needs
adding (e.g. `.DS_Store` inside `trading/`).

## Phase 2 — One runtime root (candidate 2)

**Problem:** three runtime directories (`runtime/`, `trading/runtime/`,
`brokers/runtime/`); root and `trading/` copies each hold a complete Upstox
`token_state.json` + `totp_cooldown.json` — forked auth state is a live
correctness risk.

**Change:**

1. Add `runtime_root()` to `tradex_brokers/common/paths.py`, copying the exact
   pattern `datalake/paths.py` already uses:
   - repo root anchored from `__file__` (parents count adjusted for brokers layout),
   - env override `TRADEX_RUNTIME_ROOT`,
   - default `<repo>/runtime/`.
2. Grep every writer/reader of `token_state`, `totp_cooldown`, instrument dumps
   (`dhan-instruments-*.json`, `upstox-instruments-*.json`) across `brokers/src`,
   `trading/src`, `trading/scripts`, `interface/cli.py`; re-point them through
   `runtime_root()`.
3. Delete `trading/runtime/` and `brokers/runtime/` (both gitignored).
4. Keep the `.gitignore` rule blessing only `/runtime/`.

**Tests:**

- New: boot a paper session → assert exactly one `totp_cooldown.json` and one
  `token_state.json` exist under `runtime_root()`, and none under any legacy path.
- Existing auth/token-lifecycle/totp_cooldown suites must stay green unchanged.

**Unchanged:** file formats, env vars for credentials, `build_broker_from_env()`.

## Phase 3 — Collapse order persistence (candidate 3)

**Problem:** four modules for one behaviour — `order_store.py` (39 LOC, imported
by `engine.py`), `order_persistence.py` (88, imported by `startup.py`),
`order_manager.py` (89, public façade), `sqlite_store.py` (476, the real backend).

**Change:**

1. Fold `order_store` + `order_persistence` bodies into `sqlite_store.py`.
2. `OrderManager → SqliteStore` is the single seam; re-point `engine.py` and
   `runtime/startup.py` to it.
3. Delete `order_store.py` and `order_persistence.py`.
4. `execution/__init__.py` exports unchanged (`OrderManager` already exported;
   the two shims are not in `__all__` — verified).

**Safety net:** `test_modify_cancel_idempotency`, `test_boot_safety`,
`test_inflight_duplicate`, `test_order_manager_gaps`,
`test_bracket_engine_pipeline` — must pass without behavioural edits.

**New test:** pin the single import path (engine and startup both obtain
persistence via `OrderManager`/`sqlite_store`; no module imports the deleted names).

## Phase 4 — Sync-script consolidation (candidate 4)

**Part A — orphan deletion (tracked; verify-then-delete):**

Candidates: `trading/scripts/probe_review_fixes.py`, `quick_start_live.py`,
`backfill_2025.py`, `e2e_smoke.py` (zero test references — grep-verified).

For each, run the deletion gate **before** removal:

1. grep `.github/` workflows, `Makefile`, `package.json`, docs for invocations;
2. grep `trading/tests`, `tests` for imports (currently 0 for all four);
3. if a workflow references one, either update the workflow in the same commit
   or keep the script — record the decision in the commit message.

**Part B — thin delegates (no doc breakage):**

- `fill_gaps.py`, `repair_gaps.py`, `topup_gaps.py`, `sync_today.py` become
  thin delegates: one argparse block → call the shared `simple_sync` /
  `ParallelHistoryFetcher` function that `tradex sync` already uses.
- `backfill_parquet.py` stays as the documented wrapper (CLAUDE.md references it).
- Shared guards (≤90-day Dhan intraday rule, IST conversion) are described in
  exactly one place — the shared function's docstring — not re-explained per script.

**Not in scope:** changing `simple_sync` semantics, `SyncOrchestrator` (already
gone), fetch/failover behaviour.

## Phase 5 — Analytics re-cut (candidate 5)

**Problem:** `oscillators_range_a.py` / `oscillators_range_b.py`,
`studies_simple.py` / `studies_complex.py` are size-based shards;
`indicators.py` is 1823 LOC (largest file). The real seam — `registry.py` —
already defines the taxonomy.

**Change (pure file moves, zero logic edits):**

1. Move each indicator/study member into its family package
   (`oscillators/`, `studies/`, `volatility/`, `volume/`, or top-level
   `analytics/` for the ones `indicators.py` hosts), named by concept.
2. Re-point imports in `registry.py` and any direct importers (grep the old
   module names first — engine, routes, tests).
3. Leave `registry.py` itself untouched apart from import lines — it remains
   the Tier-2 deep interface the engine and UI both consume.

**Safety net:** `test_golden_parity*`, `test_tier2_contract_2_1_0`,
`test_frontend_contract_parity`, `test_indicator_registry` — they test through
the registry, so they pin the move.

**Explicitly out of scope:** splitting `indicators.py` internals (follow-up if
wanted); any formula/behaviour change.

## Phase 6 — Golden-parity consolidation (candidate 6, opportunistic)

- Execute **only if** Phases 1–5 disturb golden paths or generators.
- Otherwise: recorded as a deferred follow-up in the kanban digest — one
  `tests/goldens/` package (baseline vs crossimpl contracts) and one
  parameterised generator script.

## Validation (every phase)

1. Full suite green via the sanctioned invocation, count ≥ baseline (3185+3).
2. `ruff check` / pre-commit clean.
3. Frontend: `tsc --noEmit` (source untouched; sanity only).
4. After Phase 2: manual boot of paper session checking single runtime state.
5. Final commit updates `CLAUDE.md` Architecture Quick Reference if any path
   moved (script invocations, experiment home).

## Sequencing & commits

```
chore/cleanup-overhaul (branch)
  1. chore: purge dead tree mass + consolidate experiment homes   (Phase 1)
  2. refactor(brokers): single runtime root behind common/paths   (Phase 2)
  3. refactor(execution): collapse order persistence into seam    (Phase 3)
  4. refactor(scripts): delete orphans, delegate sync wrappers    (Phase 4)
  5. refactor(analytics): re-cut shards by indicator family       (Phase 5)
  6. docs: update CLAUDE.md quick reference                       (final)
```

Phase 6 lands only if triggered.

## Risks

| Risk | Mitigation |
|---|---|
| A "rebuildable" artifact isn't actually reproducible | Only delete paths whose .gitignore comment states the rebuild command; keep `trading/data/ohlcv` out entirely |
| Runtime-root move orphans a live token file | Copy (not move) first run; delete legacy dirs only after the new-root test passes |
| Folded persistence changes idempotency interplay | Existing idempotency/boot-safety suites are behavioural and must pass unedited |
| Script deletion breaks an undocumented caller | Verify-then-delete gate per script, decisions recorded in commit messages |
| File moves break wildcard imports in tests | Grep old module names repo-wide before each move commit |