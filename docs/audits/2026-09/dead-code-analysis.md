# Dead Code Analysis — TradeX v4

**Date:** 2026-09-28
**Lane:** 4d (analysis only — nothing was deleted or modified)
**Scope:** all 19 Python packages + the TypeScript frontend + loose scripts
**Branch:** `wave0-1-recovered`

---

## 1. Headline numbers

| Surface | Examined | Zero-importer candidates | Proven DEAD | Public surface | Uncertain |
|---|---:|---:|---:|---:|---:|
| Python modules (`*/src/**/*.py`, non-`__init__`) | 240 | 2 | **0** | 2 | 0 |
| Python packages (whole installable) | 18 | 2 | 0 | 2 | 0 |
| TypeScript modules (`frontend/src/*.ts`) | 20 | 6 | **4** | 1 (`main.ts`) | 1 |
| Public top-level symbols (Python) | 1,104 | 163 | 0 | 82 | **82** |

**The single largest finding is the frontend.** `frontend/src` holds **6 modules totalling 1,112 lines (16.5% of the 6,747 lines of TS in `src/`)** with no live importer. The prior review (`docs/audit/2026-09-27-full-platform-review.md`, finding F9) independently reached the same six-file set; this audit confirms it mechanically and adds the build-output proof.

**The Python side is much cleaner than the premise suggested.** Only 2 of 240 non-`__init__` modules have zero importers, and *both* are reachable as `python -m` entry points, so neither is dead. What Python has instead is a thin layer of **packages whose only importer is their own test file** (2 packages) and **82 exported symbols nothing references by name** — most of them analytics helpers reached by value through registry tables, which I could not prove dead and am not claiming.

---

## 2. How the evidence was produced

Analysis is read-only. All tooling ran from `$JCODE_SCRATCH_DIR` (outside the repo tree); the only file written in the repo is this report.

Two independent mechanisms were required to agree before any candidate was reported:

1. **AST index** — parses every `.py` in the repo (786 files), models Python loader semantics
   (`import a.b.c` loads `a`, `a.b`, `a.b.c`; `from a.b import c` loads `a.b` and, if it is a
   module, `a.b.c`; `import a` loads *only* `a`; relative imports resolved against the importing
   file's package), plus literal `importlib.import_module(...)` / `__import__(...)` arguments.
   Self-imports do not count.
2. **Raw `grep` import-context pass** — the same module searched as a string in import statements
   across all `.py` outside its own package directory.

A candidate is only reported when **both** report zero. Run on `.venv/bin/python` (3.13.5), the
interpreter the repo declares (`requires-python = ">=3.12"`); the system `python3` is 3.9 and
fails to parse 2 files.

### Dynamic-import mechanisms checked and ruled out

Searched repo-wide before any DEAD claim:

| Mechanism | Where it exists here | Result |
|---|---|---|
| `importlib.import_module("…")` | `interfaces/src/tradex_interfaces/routes/chart.py:152` — `tradex_strategy.extensions.strategies.{module_name}` | Enumerated: only `sma_cross`, `mean_reversion`, `bracket_breakout` (from `_STRATEGY_FACTORIES`). No un-accounted module is reachable. |
| `importlib.import_module` (type resolver) | `domain/src/tradex_domain/serialization.py:124` — `_resolve_type` accepts only `tradex_domain.`-prefixed markers | Enumerated: domain types only. Does not reach any candidate module. |
| `__import__(…)` | `trading/tests/**` only (`__import__("time")`, `__import__("tradex_domain.enums")`) | Test-only; names stdlib + one live domain module. |
| Plugin/registry discovery | `strategy/.../extensions/__init__.py` collects names from each sub-package's `__all__` and filters by the `Strategy` protocol / `ScannerDefinition` | Enumerated: `strategies/__init__.py` imports 8 strategy modules explicitly; `scanners/__init__.py` likewise. Every extension module is imported at package import time, so none is unreachable. |
| Value-keyed dispatch tables | `PROFILE_SPECS` (`analytics/.../profiles.py:662`), `TRANSFORM_SPECS` (`transforms.py:434`), `_STRATEGY_FACTORIES` (`interfaces/.../chart.py:126`), `REGISTRY` (`indicators.py:1232`) | These make symbols reachable **by value, not by name**. Symbols named in them were reclassified as reachable and excluded from the DEAD set. |
| `[project.scripts]` entry points | Only `interfaces/pyproject.toml` declares one (`bin/tradex`) | One entry point; points at `tradex_interfaces.cli:main`, which is live. |
| `python -m X` invocations | `services/duckdb-analytics/README.md:31,42` (`python -m duck_analytics.mcp.server`, `"args": ["-m", "duck_analytics.mcp.server"]`) | This is the disproof for both Python module candidates — see §3. |
| `if __name__ == "__main__"` guards | Both Python candidates carry one | Treated as a self-entry-point, not as an importer. |
| Test files importing the module | Checked per candidate | Counted as an importer; a module whose *only* importer is its own test is reported, not marked DEAD. |
| `__all__` re-exports | Checked per candidate | 55 symbols are re-exported this way; reclassified as public surface. |
| FastAPI route decorators | `@router.get/post/put/delete` | Handlers are reached by decorator, not by name; route modules whose `router`/`create_*_router()` is registered in `fastapi_app.py` are public surface. |
| Build/dev entry points (TS) | `index.html`, `vite.config.ts`, `playwright.config.ts`, `package.json` scripts | `main.ts` is the entry; the other five zero-importer TS files are not. |

**Tree-shake proof (frontend).** The checked-in build contains zero references to any of the six
dead TS modules:

```
$ grep -c chart-lifecycle dist/assets/*.js   →  0
$ grep -c ladder          dist/assets/*.js   →  0
$ grep -c trade-bar       dist/assets/*.js   →  0
$ grep -c shellbar        dist/assets/*.js   →  0
$ grep -c chart-state     dist/assets/*.js   →  0
$ grep -c chart-types     dist/assets/*.js   →  0
```

`frontend/dist/assets/index-DBq035Kz.js` is the shipped bundle; the absence confirms Vite already
drops all six. `tsc --noEmit` exits 0, so nothing type-checks through them either.

---

## 3. Module-level findings

### 3.1 Frontend TypeScript — the real dead code (DEAD)

All six are reachable only through each other or through `main.ts`, and `main.ts` imports none of them.

Negative evidence (run from `frontend/`):

```
$ grep -rn -E "from ['\"][./]*(chart-lifecycle|ladder|trade-bar|shellbar|chart-state|chart-types)['\"]" src e2e scripts index.html | grep -v node_modules
(no output — zero importers)
```

| Module | Lines | Class | Evidence it is dead | Risk | Recommended action |
|---|---:|---|---|---|---|
| `frontend/src/chart-lifecycle.ts` | 474 | **DEAD** | Zero importers; not an entry point; 0 refs in the built bundle; only textual mentions are in `docs/`. Its own imports of `./chart-state` and `./chart-types` are the *only* importers of those two — so deleting it cascades. | Low. Vite already excludes it; `tsc` passes without it. Its 474 lines carry the chart-mounting logic, so confirm no e2e spec reaches it via `openalgo-charts` before deleting. | **Delete.** Re-run `npm run build && npx playwright test` after. |
| `frontend/src/chart-state.ts` | 127 | **DEAD** | Sole importer is `chart-lifecycle.ts` (also dead). 0 refs in bundle. | Low. | **Delete** with `chart-lifecycle.ts`. |
| `frontend/src/chart-types.ts` | 26 | **DEAD** | Sole importer is `chart-lifecycle.ts` (also dead). 0 refs in bundle. | Low. | **Delete** with `chart-lifecycle.ts`. |
| `frontend/src/ladder.ts` | 32 | **DEAD** | Zero importers; not an entry point; 0 refs in bundle. Contains a `# ponytail:` comment noting the teardown is never wired. | Low. | **Delete.** |
| `frontend/src/trade-bar.ts` | 17 | **DEAD** | Zero importers. `mountTradeBar` has an **empty body** — the comment says controls moved to the Subbar; `getOrderQty()` is a one-line delegate to `shellbar.getOrderQty` and has no caller. | Low. | **Delete** with `shellbar.ts`. |
| `frontend/src/shellbar.ts` | 436 | **DEAD** | Sole importer is `trade-bar.ts` (also dead, empty body). 0 refs in bundle. | Medium. 436 lines; the e2e specs assert on a "Subbar" DOM surface, so confirm the live code path that renders it (`main.ts` → `createDock`) does not depend on this module's exports. | **Delete**, then run `npm run e2e`. |
| `frontend/src/main.ts` | 298 | **PUBLIC SURFACE** | Zero *importers* because it **is** the entry point: `frontend/index.html:24` → `<script type="module" src="/src/main.ts"></script>`. | — | **Keep.** Not dead. |

**Total deletable: 1,112 lines, 16.5% of `frontend/src`.** Deleting `chart-lifecycle.ts` makes
`chart-state.ts` and `chart-types.ts` dead; deleting `trade-bar.ts` makes `shellbar.ts` dead.
Delete in one pass, not six, and re-run the e2e suite.

### 3.2 Python modules — zero importers, both reachable (PUBLIC SURFACE)

| Module | Lines | Class | Evidence | Risk | Recommended action |
|---|---:|---|---|---|---|
| `duck_analytics.mcp.server` | 232 | **PUBLIC SURFACE** | Zero importers, but it is a documented entry point: `services/duckdb-analytics/README.md:31` `PYTHONPATH=… python -m duck_analytics.mcp.server`, and `:42` `"args": ["-m", "duck_analytics.mcp.server"]` in an MCP server config. It also has an `if __name__ == "__main__"` guard. | — | **Keep.** Not dead. |
| `duck_analytics.screener` | 62 | **PUBLIC SURFACE** | Zero importers, but its docstring is a usage contract (`python -m duck_analytics.screener --scanner topgainer --as-of "…"`) and it has an `if __name__ == "__main__"` guard. It is the CLI the package README documents. | — | **Keep.** Not dead. The prior review marked this DELETE for "no console script entry"; that is wrong — the entry is `python -m`, which needs no `[project.scripts]`. |

**No Python module is provably dead.**

### 3.3 Python packages — only importer is their own test

| Package | Lines | Class | Evidence | Risk | Recommended action |
|---|---:|---|---|---|---|
| `tradex_operations` | 41 | **PUBLIC SURFACE** (needs human decision) | Sole importer is `operations/tests/test_runbooks.py`. It ships `py.typed` and a documented `__all__` (`METRICS_CATALOG`, `RUNBOOKS`, `list_runbooks`, `runbook_path`); `.github/workflows/quality-gate.yml:108` measures `--cov=tradex_operations`. | Medium. Declared public API with a coverage hook in CI. Deleting it changes the wheel's public surface and the CI coverage line. | **Human decision.** If nothing outside the repo consumes `runbook_path`/`METRICS_CATALOG`, delete the package *and* the `--cov` line in one change. |
| `tradex_persistence` | 19 | **PUBLIC SURFACE** (needs human decision) | Sole importer is `persistence/tests/test_persistence_package_import.py`. Its docstring says it is "owned by execution", but `runtime/startup.py` imports from `tradex_execution` directly. Ships `py.typed`; `quality-gate.yml:113` measures `--cov=tradex_persistence`. | Medium. Same as above: a 19-LOC stub with a real `__all__` (`OrderStore`, `SQLiteEventStore`, `InMemoryOrderStore`, `SQLiteOrderStore`, `SQLiteIdempotencyGuard`, `attach_order_persistence`) and a CI coverage line. | **Human decision.** This is a half-finished extraction, not dead code. Either finish it (move the SQLite stores here) or delete it and its `--cov` line. |

### 3.4 Python modules whose only importer is a test file (not dead, listed for transparency)

These are *reached*, so they are not dead. They are reported because the previous review flagged
some of them as DELETE, and that classification needs the importer named to be evaluated.

| Module | Lines | Sole importer | Note |
|---|---:|---|---|
| `tradex_execution.envelopes` | 433 | `trading/tests/execution/test_envelopes.py` | 433 lines of typed envelope + `EnvelopeTranslator`. Not dead, but no production code uses it. The prior review's DELETE rests on "the test imports the shim" — **no `tradex_trading` shim exists** (I searched; the package was removed, see `pyproject.toml:141-143`). The test imports `tradex_execution.envelopes` directly. **UNCERTAIN**: this is a designed-but-unused integration seam. |
| `tradex_brokers.common.http_response` | 58 | `brokers/tests/common/test_http_response.py` | `HttpResponse` + `require_success_response`; no broker adapter imports it. |
| `tradex_interfaces.safety_report_store` | 64 | `trading/tests/interface/test_safety_wiring.py` | |
| `tradex_interfaces.workspace_store` | 89 | `interfaces/.../routes/chart.py` | Reached by production code; not dead. |

---

## 4. Symbol-level findings

1,104 public top-level symbols were scanned. A symbol was counted as a candidate only when no file
outside its own module referenced the name, and no `from <module> import <symbol>` existed anywhere.

**163 candidates** survived that filter. **87 were then disproved** by reachability mechanisms that
do not use the name — 69 are named inside a module-level registry/dispatch table (`PROFILE_SPECS`,
`TRANSFORM_SPECS`, `_STRATEGY_FACTORIES`, `REGISTRY`), 55 are listed in `__all__` (module or package),
18 are `@dataclass` types reached by their spec, 6 are argparse `set_defaults(func=…)` targets,
5 are `@router.get` handlers, 3 are `@runtime_checkable` protocols, 3 are `@tool`.

**82 remain, and I am classifying all 82 as UNCERTAIN, not DEAD.**

Why: these are exported symbols inside live modules. The name-reference scan proves no *name* reaches
them, but the same registries that rescued the other 87 could be hiding a reference — I verified the
four named registries, but there are more dispatch dicts I did not exhaustively enumerate, and the
analytics package in particular has a large value-dispatched surface. Proving a symbol dead requires
checking every one of them, which is a much larger job than this pass.

The single largest cluster is `tradex_analytics` (52 of 82): indicator helpers such as
`hull_suite`, `linreg_slope`, `connors_streak`, `smi_ergodic_indicator`, `aroon_oscillator`,
`ma_channel`, `chaikin_volatility`, `elder_force_index`, `mass_index`, `net_volume`, plus colour
constants in `seasonality` (`HEAVY_ALPHA`, `ramp_color`, `with_opacity`, …). These sit inside
modules that the indicator/`REGISTRY` machinery imports wholesale, so they are *probably* wired
through a registry key rather than genuinely unreachable — but "probably" is not proof.

Full list (symbol → module → module lines):

| Symbol | Module | Lines | Module reaches it via |
|---|---|---:|---|
| `mcp` | `duck_analytics.mcp.server` | 232 | `import mcp` inside the server module — it is the MCP framework import, live |
| `hull_suite` | `tradex_analytics.indicators` | 1830 | wired into `REGISTRY` via `_fn_hull_suite` (indicators.py:1473) |
| `linreg_slope` | `tradex_analytics.indicators` | 1830 | wired into `REGISTRY` via `_fn_linreg_slope` (indicators.py:1459) |
| `connors_streak` | `tradex_analytics.oscillators.oscillators_range_b` | 290 | none found |
| `smi_ergodic_indicator` | `tradex_analytics.oscillators.oscillators_strength` | 491 | none found |
| `smi_ergodic_oscillator` | `tradex_analytics.oscillators.oscillators_strength` | 491 | none found |
| `aroon_oscillator` | `tradex_analytics.oscillators.oscillators_trend` | 435 | none found |
| `bucket_price` | `tradex_analytics.profiles` | 716 | used by `price_buckets` (same module) |
| `cumulative_delta` | `tradex_analytics.profiles` | 716 | none found |
| `diagonal_imbalances` | `tradex_analytics.profiles` | 716 | none found |
| `naked_levels` | `tradex_analytics.profiles` | 716 | none found |
| `price_buckets` | `tradex_analytics.profiles` | 716 | none found |
| `row_of` | `tradex_analytics.profiles` | 716 | none found |
| `stacked_imbalances` | `tradex_analytics.profiles` | 716 | none found |
| `HEAVY_ALPHA` … `with_opacity` (8) | `tradex_analytics.seasonality` | 400 | used internally by `compute_seasonality` (same module) |
| `HOUR_SECONDS` … `PIVOT_LEVELS` (5) | `tradex_analytics.studies.studies_complex` | 809 | used internally by the study (same module) |
| `consolidation_breakout` | `tradex_analytics.studies.studies_signals` | 767 | none found |
| `run_transform` | `tradex_analytics.transforms` | 475 | used by `compute_transform` (same module) |
| `ma_channel`, `standard_error_bands` | `tradex_analytics.volatility.volatility_bands` | 571 | none found |
| `chaikin_volatility`, `standard_deviation`, `standard_error` | `tradex_analytics.volatility.volatility_chop` | 401 | none found |
| `chaikin_oscillator`, `elder_force_index`, `ulcer_index` | `tradex_analytics.volume.volume_flow` | 299 | none found |
| `KLINGER_FAST/SLOW/SIGNAL`, `mass_index` | `tradex_analytics.volume.volume_indices` | 511 | none found |
| `net_volume`, `volume_raw` | `tradex_analytics.volume.volume_simple` | 132 | none found |
| `SendPipeline` | `tradex_brokers.common.provider_client` | 371 | none found |
| `DEFAULT_COOLDOWN_SECONDS` | `tradex_brokers.common.totp_cooldown` | 348 | none found |
| `HEADER_SIZE`, `LEVEL_SIZE` | `tradex_brokers.dhan.depth_parser` | 120 | used internally by the parser |
| `DHAN_SUBSCRIBE_FULL` | `tradex_brokers.dhan.ws_streams` | 769 | none found |
| `MISSING_PRODUCT_TYPE_METRIC` | `tradex_execution.fees` | 477 | none found |
| `WorkspacePutBody` | `tradex_interfaces.routes.chart` | 1149 | Pydantic model in a route module — reached reflectively by FastAPI |
| `place_bracket_order` | `tradex_interfaces.routes.orders` | 627 | `@router` handler (registered via `create_orders_router`) |
| `FEED_STATUS_POLL_SECONDS` | `tradex_interfaces.routes.stream` | 1530 | used internally by `ws_stream` |
| `IndicatorSeriesCache`, `TAIL_CACHE_*` (3), `Tail` | `tradex_interfaces.routes.stream_indicators` | 865 | used internally by the indicator stream registry |
| `ACQUIRE_TIMEOUT_S`, `ERROR_LOG_CAP` | `tradex_market_data.parallel_fetcher` | 339 | none found |
| `LabelKey` | `tradex_observability.metrics` | 485 | none found |
| `SubscriberType` | `tradex_reactive.bus` | 473 | none found |
| `RunFn` | `tradex_replay.validation` | 392 | none found |
| `FORMING_FRAME_MIN_INTERVAL` | `tradex_runtime.bar_aggregator` | 802 | used internally by the aggregator |
| `JsonFetch` | `tradex_runtime.live` | 619 | none found |
| `Download`, `ParseRaw` | `tradex_runtime.master_lifecycle` | 196 | none found |
| `DEFAULT_FILL_PATH`, `FILL_PATHS`, `resolve_fill_path` | `tradex_runtime.paper_book_fill_source` | 379 | `resolve_fill_path` used internally |
| `StrategyArtifactStore` | `tradex_strategy.artifacts` | 125 | none found |
| `bracket_breakout_strategy` | `tradex_strategy.extensions.strategies.bracket_breakout` | 260 | named in the extensions `__all__` chain → discovered by `all_strategies` |

**Recommended action for all 82:** treat as a follow-up exercise, not a deletion list. The safe
order is: (1) confirm the frontend deletion first (it is proven), (2) then enumerate *every*
dispatch dict in `tradex_analytics` and re-run the symbol scan with those keys added to the
reachability set. Only symbols that survive that second pass are deletion candidates, and each one
should be grepped by name in `openalgo-charts/` as well, since several analytics functions are
parity ports of TS indicators.

---

## 5. Secondary finding — a stale route registry (a live bug, not dead code)

`interfaces/src/tradex_interfaces/routes/__init__.py` declares `ALL_ROUTES`, and its docstring
states:

> `create_app` (in `tradex_interfaces.fastapi_app`) iterates the `ALL_ROUTES` list and
> `include_router`s each one.

**That is false.** `fastapi_app.py` never references `ALL_ROUTES` — the only occurrences of the name in
the repo are its own definition, its own docstring and its own `__all__` (verified by
`grep -rn "ALL_ROUTES" --include="*.py" .`, 3 hits, all in `routes/__init__.py`). It imports the six
route modules by name (lines 250-282) and registers them explicitly:

```python
app.include_router(_orders_route.create_orders_router(replay_guard=app.state.replay_guard), ...)
for _r in (_health_route.router, _portfolio_route.router, _market_data_route.router,
           _account_route.router, _extensions_route.router):
    app.include_router(_r)
```

So `ALL_ROUTES` (9 lines) is dead weight *and* misleading, and its member list has drifted from the
truth: it omits `chart` (registered via `create_chart_router`) and `stream`/`stream_indicators`
(wired by the WebSocket closure), and it lists `account`/`extensions`/`health`/`market_data`/
`orders`/`portfolio` in a different order than the code registers them. Anyone editing `ALL_ROUTES`
to add a route would correctly believe they had wired it and would be wrong.

**Classification:** `ALL_ROUTES` — **DEAD** (zero consumers, proven by grep), but the fix is not
deletion alone: either delete it and the docstring, or make `create_app` actually iterate it.
Risk: low. Recommended action: **human decision** — this is a doc/code contract that has rotted;
do not delete silently.

---

## 6. What I could not prove, stated plainly

- **The 82 symbol candidates are not proven dead.** I proved no *name* reaches them. The
  `tradex_analytics` indicator/registry surface is large and value-dispatched; a definitive answer
  needs every dispatch dict enumerated. I am deliberately not calling them dead.
- **`tradex_execution.envelopes`** is reached only by a test. It may be a deliberately unused
  integration seam (Wave A3 typed envelopes, per its docstring) that production wiring was never
  built for. That is a design question, not a dead-code question.
- **`tradex_operations` / `tradex_persistence`** are packages with real `__all__` exports, `py.typed`
  markers, and CI `--cov` lines. Their only importer is their own test. Whether they are dead
  depends on whether anything outside this repo imports them, which I cannot determine from the tree.
- **`duck_analytics.screener`** is documented as a CLI entry point but has no `[project.scripts]`
  declaration. The prior review called that "no console script entry → DELETE". A `python -m`
  entry needs no console script, so I classify it PUBLIC SURFACE. If the intent is that this CLI is
  no longer supported, deleting it should also remove the README section.
- **The 6 frontend modules** are proven dead inside this repo. The one residual doubt is
  `openalgo-charts`: `ladder.ts` and `chart-lifecycle.ts` import from `openalgo-charts/trade` and
  the chart widget, and `openalgo-charts` is a separate package (excluded from this analysis). If
  any consumer of `openalgo-charts` reaches back into `frontend/src` — it does not, by the bundle
  check — the claim would fail. The bundle check is the strongest evidence: those names appear zero
  times in the shipped JS.
- **`poc/`, `apps/`, `benchmarks/`** are excluded from the module registry (they match ruff's
  `extend-exclude` and are documented as scratch). `poc/research/duckdb/*.py` is the sole importer
  of several `duck_analytics` modules; if `poc/` is ever deleted, those modules lose importers and
  the analysis would need to be re-run.

---

## 7. Recommended deletion order

| Step | Target | Lines | Pre-conditions | Verification after |
|---|---|---:|---|---|
| 1 | `frontend/src/{chart-lifecycle,chart-state,chart-types,ladder,trade-bar,shellbar}.ts` | 1,112 | Confirm no e2e spec reaches the chart mount path through `openalgo-charts` | `npm run build && npx playwright test && npm run typecheck` |
| 2 | `interfaces/src/tradex_interfaces/routes/__init__.py` `ALL_ROUTES` + its docstring | ~15 | Decide whether to re-wire `create_app` to iterate it | `uv run --no-sync python -m pytest trading/tests/interface -q` |
| 3 | `tradex_operations` package | 41 | Confirm no external consumer; drop `--cov=tradex_operations` from `quality-gate.yml` | root `pytest` + `uv sync` |
| 4 | `tradex_persistence` package | 19 | Same; drop `--cov=tradex_persistence`. This is a half-finished extraction — the better option may be to finish it | root `pytest` + `uv sync` |
| 5 | 82 symbol candidates | — | **Do not delete yet.** Re-run the symbol scan with every `tradex_analytics` dispatch dict enumerated | — |

Step 1 is the only one with a complete proof. Steps 2-4 are human decisions; step 5 is unfinished
analysis.

---

## 8. Reproducing this analysis

All tooling lives in `$JCODE_SCRATCH_DIR/dca/` (outside the repo) and is read-only:

| Script | Purpose |
|---|---|
| `loader_analysis.py` | AST import index; models Python loader semantics; emits `loaders.json` |
| `evidence.py` | Dual-mechanism negative evidence (AST + grep); emits `evidence.json` |
| `ts_analysis.py` | TS/ESM import graph for `frontend/`; emits `ts_index.json` |
| `symbols_fast.py` | Repo-wide name-reference index over 1,104 public symbols |
| `symbols_verify.py` | Disproves symbol candidates against the AST import graph |
| `routes.py` | FastAPI route-handler reachability via `@router` decorators |
| `refine.py` | Excludes symbols reached by registry value, `__all__`, argparse, decorators |
| `consolidate.py` | Emits the consolidated finding set |

Run with `.venv/bin/python` (3.13.5). The system `python3` (3.9.6) cannot parse 2 files:
`observability/src/tradex_observability/metrics.py:180` and
`strategy/src/tradex_strategy/core/scanner_limits.py:113`.
