# TradeX v4 — Independent Verification Review

**Date:** 2026-08-29
**Branch:** `main` @ `0e976e8` — 18 files modified, uncommitted
**Purpose:** Independent re-review. Verifies the claims in
`docs/reviews/code-review-2026-08-29.md` against the current working tree and
re-measures the baseline.

---

## Verdict

The prior review's assessment of the *design* holds up: boundaries really are
enforced, duplication is genuinely low, and there are no committed secrets.

Two of its headline claims, however, do **not** survive verification. The suite
is **not** green — it has 12 failures and 15 collection errors in `trading`
alone, all from one undeclared dependency. And the repo's most-marketed feature
(**datalake-backed backtesting**) cannot be imported at all on a clean install,
because `pyarrow` is used but never declared.

| Area | Rating |
|------|--------|
| Architecture & boundaries | Strong |
| Core code quality | Strong |
| Test suite | **Not green** — 12 failed, 15 errors |
| Packaging / dependency declaration | **Broken** |
| CI / quality gates | **Broken** (1 of 2 workflows) |
| Security posture | Adequate, two known gaps still open |
| Repo hygiene | Weak |

---

## Measured baseline

Measured on this checkout, not carried over from the prior document.

| Metric | Value |
|--------|-------|
| Python source | 82,199 LOC (domain 4,852 / brokers 22,858 / trading 54,489) |
| Test files | 181 |
| `domain` suite | **299 passed** (0.40s) |
| `brokers` suite | **765 passed, 2 skipped** (22.82s) |
| `trading` suite | **1363 passed, 12 failed, 15 errors** (50.94s) |
| root `tests/` (arch guards) | **4 passed** — but never run by the documented command |
| Documented combined command | **190 collection errors** |
| Ruff | 27 issues across 82k LOC (7 F401, 7 E501, 5 I001, 3 E741, 2 E402, 1 F841, 1 UP037, 1 UP028) |
| `TODO` / `FIXME` markers | 0 |

Per-package runs are what work. The combined command does not (see V2).

---

## C1 — CRITICAL: `pyarrow` is used but never declared

`trading/src/tradex_trading/datalake/parquet_storage.py` imports it at module
level, unconditionally:

```python
# ponytail: pyarrow is already installed (ParquetDataCatalog depends on it).
import pyarrow as pa
import pyarrow.parquet as pq
```

That comment is wrong. Verified:

- `grep pyarrow */pyproject.toml pyproject.toml` → **no matches**
- `grep -c pyarrow uv.lock` → **0**
- The `datalake` extra is `["duckdb>=0.10,<2", "pandas>=2,<3"]` — **no pyarrow**
- The `full` extra is also missing it
- `python -c "import pyarrow"` in `.venv` → `ModuleNotFoundError`

Nothing in the dependency chain pulls it. `duckdb` and `pandas` do not depend on
`pyarrow`.

**Blast radius.** `parquet_storage` is the base of the datalake, and
`tradex_trading/datalake/__init__.py` eagerly imports `ParquetBacktestLoader`,
which imports `ParquetMarketProvider`, which imports `parquet_storage`. So a
single missing package makes `import tradex_trading.datalake` fail outright.

Every measured test failure and collection error in the suite comes from this —
26 `ModuleNotFoundError: No module named 'pyarrow'` occurrences in the log,
covering all 12 failures and 15 errors:

- `sdk/test_session_provider_data.py` — `boot()` in `backtest` and `replay` mode
  raises at startup. **Backtest and replay sessions cannot boot.**
- `runtime/test_boot_safety.py`, `sdk/test_session_readiness.py` — same
- `interface/test_chart_history.py`, `test_ws_bars_replay.py` — the chart
  history endpoint 500s
- `interface/test_chart_backtest.py`, `test_chart_indicators.py` — 15 collection
  errors

The README markets `ParquetBacktestLoader` and `tradex serve` as headline
features. On a clean `pip install -e domain -e brokers -e trading`, none of it
works.

**Fix:** add `pyarrow` to the `datalake` extra (and to `full`). Until then every
datalake, backtest, and replay path is dead on a fresh install.

---

## C2 — CRITICAL: the `parity-gate` workflow cannot run

`.github/workflows/parity.yml` — the self-described *release gate* asserting that
backtest, replay, paper and live produce identical results:

```yaml
- name: Install packages
  run: python -m pip install -e ./domain -e ./brokers -e ./trading
- name: Parity + CQRS gate
  run: python -m pytest trading/tests/parity trading/tests/contracts -q
```

Three independent defects:

1. **No extras** — `pandas` and `pyarrow` are never installed, so the parity
   tests cannot import the datalake they depend on. Verified locally: all 5
   parity test modules error at collection.
2. **pytest is never installed.** Nothing in the step list installs it.
3. It runs from the repo root, which triggers the module-id collision in V2.

This workflow has never been able to pass. It is the most valuable idea in the
repo and it is inert.

---

## V1 — Verified fixed since the prior review

Credit where due — the prior review's C1 and H2 were acted on, in commits made
**today** (`ec6c288`, `fd57963`):

- `uv.lock` is now a real lockfile with resolution markers, not a 4-line stub.
- Root `pyproject.toml` now has `[tool.uv.workspace]` and a
  `[dependency-groups] dev = [...]` containing pytest, pytest-cov,
  pytest-timeout, ruff, mypy, and `tradex-trading[datalake,api,full]`.
- `uv.lock` `requires-python` is now `>=3.12`, aligned with all three packages
  and with CI. The version drift is resolved.
- `ruff` and `mypy` **are** installed in `.venv` (the prior review said they were
  installed nowhere).

So the `quality-gate` workflow should now be able to run. **Recommendation:
confirm it actually passes on a fresh `uv sync`.** Note it will still hit C1 —
the `dev` group pulls `tradex-trading[datalake,...]`, which does not include
`pyarrow`, so the same 27 tests will fail there too.

---

## V2 — HIGH: the documented test command is broken

README and `conftest.py` both document:

```
python -m pytest domain/tests brokers/tests trading/tests -q
```

This produces **190 collection errors**. Measured:

```
E   ModuleNotFoundError: No module named 'tests.common'
```

**Mechanism.** All three test packages carry `tests/__init__.py`, so pytest
derives the module id `tests.<sub>.<module>` for each — and all three collide on
the same top-level name `tests`. Whichever package imports first binds `tests` in
`sys.modules`, and the next one fails to find its own `tests.<sub>`. The root
`tests/` directory (no `__init__.py`) additionally resolves as a namespace
package shadowing all three.

The root `pyproject.toml` comment says `--import-mode=importlib` "removes the
collision". It does not — importlib mode still derives package-relative names
when `__init__.py` is present.

Confirmed by isolation — each package collects fine alone:

| Invocation | Result |
|------------|--------|
| `pytest domain/tests` | 299 collected, 0 errors |
| `pytest brokers/tests` | 767 collected, 0 errors |
| `pytest trading/tests/contracts` | 133 collected, 0 errors |
| `pytest domain/tests brokers/tests trading/tests` | **190 errors** |

**Fix options**, in order of preference:

1. Drop the three `tests/__init__.py` files so pytest derives unique
   rootdir-relative ids (`brokers.tests.common.test_x`). One-line change, fixes
   the combined run.
2. Or make the documented command three separate per-package invocations and
   correct the README + `conftest.py` docstring.

Either way, add the root `tests/` to the documented command — it holds the 4
architecture-guard tests (`test_import_boundaries.py`,
`test_no_foreign_private_writes.py`) that enforce the dependency boundary the
README advertises. They are **excluded from the documented command today, so the
boundary is unverified by the documented workflow.**

---

## Still open from the prior review (re-verified, all confirmed)

| # | Finding | Status |
|---|---------|--------|
| H1 | `fees.py` dispatches to a structurally different legacy model when any rate is overridden; docstring claims "one fee model, not two"; divergence is pinned by tests | **Open** |
| C2 | 5 POST endpoints unauthenticated; `POST /api/charts/backtest` and `POST /api/charts/scanner/run` are unbounded CPU/IO work with no rate limiting | **Open** |
| M1 | Half-finished auth refactor: `make_verify_api_key` is a byte-for-byte duplicate of `verify_api_key`; `api_key_header` imported but unused (ruff F401); `verify_api_key` assigned to a local at `fastapi_app.py:97` and never read (ruff F841); two E402 mid-file imports | **Open** |
| M2 | Scratch files untracked *and* unignored: `_frontend_files.txt`, `_git_log.txt`, `_git_status.txt`, `_pwd.txt`, `_todo.txt`, `candles_app.py`, `smoke_*.py` | **Open** |
| M4 | Blanket `ignore::DeprecationWarning` hides the project's own deprecations | **Open** |
| M5 | `DurableTokenManager` deprecated, no production consumer | **Open** |
| M6 | No frontend CI — 4,154 LOC of TypeScript with `tsc --noEmit` available but never run | **Open** |

Endpoint auth, verified directly against the route table: `orders.py` applies
`Depends(verify_api_key)` to its 2 POST routes; `chart.py` registers 5 POST
routes with no auth dependency at all.

---

## Corrections to the prior review

Recorded so the two documents are not read as agreeing where they do not.

1. **"2,740 passed, 2 skipped, 0 failed"** — not reproducible on this checkout.
   Measured: 12 failed and 15 errors in `trading`, plus a further ~25 modules
   that cannot be collected at all. The suite is not green.
2. **"H2: neither ruff nor mypy is installed anywhere"** — both are present in
   `.venv/bin/`.
3. **"C1: root pyproject.toml has no `[project]` table"** — the workspace and
   dev-dependency group now exist (V1).
4. **"H3: 32 genuinely unused imports"** — ruff, the configured linter, reports
   **7 F401** (6 in `fastapi_app.py`, 1 in `domain/events.py`). The AST scan
   overcounted, most likely by not accounting for `__all__` re-exports and
   string annotations. The real lint surface is 27 issues total, which is
   excellent for 82k LOC — the finding is much smaller than reported.

---

## What is genuinely good

- **Boundaries are really enforced.** Verified, not assumed: `domain` imports no
  internal package, `brokers` never imports `tradex_trading`. And there is a
  test suite that checks it.
- **27 ruff issues across 82,199 LOC.** Genuinely clean.
- **2.65% duplication.** Cohesive large files, not accreted ones.
- **Zero `TODO`/`FIXME`** at this size.
- **No secrets committed.** `.env.local` untracked; pattern scan for API keys,
  AWS keys, and private keys across all tracked files returns nothing.
- **Constant-time API-key comparison** and fail-closed live-broker gating.
- **The parity-gate concept is the right invariant** for a trading platform — it
  is just not executable yet.
- **The in-flight risk work is correct.** `reject_unknown_market_value` closes a
  real hole where MARKET orders with no price collapsed their notional to zero
  and bypassed the order-value gate. Ship it.

---

## Recommended order of work

1. **Declare `pyarrow`** in the `datalake` and `full` extras (C1). One line;
   unblocks 27 tests and the entire backtest/replay/datalake feature. Verify
   with `pip install -e "trading[datalake]" && python -c "import tradex_trading.datalake"`.
2. **Fix `parity.yml`** (C2) — install extras and pytest. Without this the
   release gate is decorative.
3. **Fix the test command** (V2) — remove the three `tests/__init__.py` files,
   then update the README and `conftest.py` docstring, and fold in the root
   `tests/` so the boundary guards actually run.
4. **Confirm `quality-gate` passes** on a fresh `uv sync` (V1) — it has never
   run to completion; expect C1 to surface there too.
5. **Add `pandas` to the `full` extra** — `full` is currently not a superset of
   `datalake`.
6. **Gate the two heavy POST endpoints** and add rate limiting.
7. **Finish the auth refactor** (M1) — delete the duplicate and dead wiring.
8. **Decide the fee model** (H1) — collapse to one path, fix the docstring that
   claims there is already only one.
9. **Hygiene pass** (M2, M4) — gitignore or delete the scratch, narrow the
   warning filter, add frontend `tsc --noEmit` to CI.
