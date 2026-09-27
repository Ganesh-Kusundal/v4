# Pre-Deployment System Review — `trading/` package (shim tree, legacy paths, composition root)

**Repo:** `/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4`
**Branch:** `chore/cleanup-overhaul` · 482 modified files
**Method:** AST-based reference graph + word-boundary textual scan over all 917 git-tracked files. No files modified.

---

## 0. HEADLINE CORRECTION TO THE BRIEF

The brief describes `trading/` as "the claimed composition root" holding ~1,673 LOC of shims. Two corrections:

| Claim | Finding | Evidence |
|---|---|---|
| `trading/` **is** the composition root | **FALSE.** The composition root is `runtime/src/tradex_runtime/startup.py` (`boot()`, 1,005 lines, `__all__` at `:1005`). | `runtime/src/tradex_runtime/startup.py:155` |
| `trading/` **is** a shim tree | **MOSTLY TRUE, but 5 files are real code.** | See below |

`trading/src/tradex_trading/` = **159 files / 2,584 lines**, of which:
- **154 files = importlib compatibility shims → 1,673 LOC** (exactly the brief's figure)
- **5 files = REAL production code, 911 LOC** — these are the *only* real code left in the package

### The 5 real-code files (KEEP — not shims)

| LOC | Path | Role |
|---|---|---|
| 509 | `trading/src/tradex_trading/sdk/session.py` | `TradingSession` — the SDK façade |
| 283 | `trading/src/tradex_trading/sdk/live_fill_bridge.py` | live fill → bus bridge |
| 73 | `trading/src/tradex_trading/sdk/streaming.py` | stream subscription |
| 33 | `trading/src/tradex_trading/__init__.py` | package façade (re-exports) |
| 13 | `trading/src/tradex_trading/sdk/__init__.py` | SDK façade |

Every other file under `trading/src/tradex_trading/` is the 11-line template:
```python
_impl = _import_module("tradex_interfaces.routes.health")
globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})
```
Verified: **43/43 dead shims match this template exactly, 0 suspicious, 0 missing targets.**

---

## 1. DELETE LIST — HIGHEST VALUE OUTPUT

### 1A. DELETE NOW — 32 leaf shim files, 352 LOC, zero references anywhere

(plus 1 more file, `strategy/extensions/shared/__init__.py`, in §1B → **33 files / 363 LOC total**)

Proof method: `git grep` for the dotted path (word-boundary corrected), excluding the file itself; then AST import scan across all 917 tracked `.py`; then string/`importlib`/`monkeypatch` scan. All three return empty. Every real target exists on disk.

| # | Shim path | LOC | Real target | External refs |
|---|---|---|---|---|
| 1 | `interface/routes/health.py` | 11 | `tradex_interfaces.routes.health` | **none** |
| 2 | `interface/routes/stream.py` | 11 | `tradex_interfaces.routes.stream` | **none** |
| 3 | `interface/routes/account.py` | 11 | `tradex_interfaces.routes.account` | **none** |
| 4 | `interface/routes/portfolio.py` | 11 | `tradex_interfaces.routes.portfolio` | **none** |
| 5 | `interface/routes/market_data.py` | 11 | `tradex_interfaces.routes.market_data` | **none** |
| 6 | `interface/routes/extensions.py` | 11 | `tradex_interfaces.routes.extensions` | **none** |
| 7 | `interface/routes/deps.py` | 11 | `tradex_interfaces.routes.deps` | **none** |
| 8 | `interface/routes/_market_helpers.py` | 11 | `tradex_interfaces.routes._market_helpers` | **none** |
| 9 | `interface/auth/csrf.py` | 11 | `tradex_interfaces.auth.csrf` | **none** |
| 10 | `interface/auth/deps.py` | 11 | `tradex_interfaces.auth.deps` | **none** |
| 11 | `interface/auth/rate_limit.py` | 11 | `tradex_interfaces.auth.rate_limit` | **none** |
| 12 | `interface/auth/routes.py` | 11 | `tradex_interfaces.auth.routes` | **none** |
| 13 | `interface/auth/session_store.py` | 11 | `tradex_interfaces.auth.session_store` | **none** |
| 14 | `interface/queueing.py` | 11 | `tradex_interfaces.queueing` | **none** |
| 15 | `interface/replay_run.py` | 11 | `tradex_interfaces.replay_run` | **none** |
| 16 | `interface/workspace_store.py` | 11 | `tradex_interfaces.workspace_store` | **none** |
| 17 | `analytics/probability.py` | 11 | `tradex_analytics.probability` | **none** |
| 18 | `analytics/trade_metrics.py` | 11 | `tradex_analytics.trade_metrics` | **none** |
| 19 | `analytics/volatility/volatility.py` | 11 | `tradex_analytics.volatility.volatility` | **none** |
| 20 | `datalake/market_provider.py` | 11 | `tradex_market_data.market_provider` | **none** |
| 21 | `execution/envelopes.py` | 11 | `tradex_execution.envelopes` | **none** |
| 22 | `execution/recovery.py` | 11 | `tradex_execution.recovery` | **none** |
| 23 | `execution/sqlite_event_store.py` | 11 | `tradex_execution.sqlite_event_store` | **none** |
| 24 | `research/manifests.py` | 11 | `tradex_research.manifests` | **none** |
| 25 | `research/pit.py` | 11 | `tradex_research.pit` | **none** |
| 26 | `runtime/feed_monitor.py` | 11 | `tradex_runtime.feed_monitor` | **none** |
| 27 | `runtime/feed_recovery.py` | 11 | `tradex_runtime.feed_recovery` | **none** |
| 28 | `strategy/artifacts.py` | 11 | `tradex_strategy.artifacts` | **none** |
| 29 | `strategy/registry.py` | 11 | `tradex_strategy.registry` | **none** |
| 30 | `strategy/runtime.py` | 11 | `tradex_strategy.runtime` | **none** |
| 31 | `strategy/extensions/strategies/bracket_breakout.py` | 11 | `tradex_strategy...bracket_breakout` | **none** |
| 32 | `application/orders.py` | 11 | `tradex_application.orders` | **none** |

Grep evidence for the pattern (representative, all 16 interface ones returned `0 external hits`):
```
$ git grep -n "tradex_trading.interface.routes.health"   →  (empty)
$ git grep -n "tradex_trading.interface.auth.csrf"        →  (empty)
$ git grep -n "tradex_trading.interface.workspace_store"  →  only docs/superpowers/specs/…:34 (prose, not code)
```
Two apparent hits were disproved as substring artifacts, not imports:
- `stream` → matched `tradex_trading.interface.routes.stream_indicators` (a *different*, live module)
- `market_data` → matched prose in `docs/superpowers/plans/…:188`

**What breaks if you delete them: nothing.** No import statement, no `importlib` string, no `monkeypatch`/`setattr` target, no `sys.modules` key, no `pyproject` entry, no CI reference. They are unreachable by any executed code path.

### 1B. DELETE AS A UNIT — 1 directory, 1 shim, 11 LOC

`trading/src/tradex_trading/strategy/extensions/shared/` — contains only `__init__.py`, an alias for `tradex_strategy.extensions.shared/`, which is **also an empty placeholder package** referenced by nothing:
```
$ git grep -n "extensions.shared" -- '*.py'
trading/src/tradex_trading/strategy/extensions/shared/__init__.py:1,5   (self only)
```
Both sides are empty. Delete `shared/` from **both** `trading/` and `strategy/`.

### 1C. DO NOT DELETE — 10 "dead" `__init__.py` are required package markers

These have zero references *as modules*, but deleting them **breaks every sibling submodule import**, because Python requires `__init__.py` for regular packages (no `__namespace__` fallback here). Keep them until the whole tree goes:

| Path | Live siblings that would break |
|---|---|
| `analytics/oscillators/__init__.py` | 4 |
| `analytics/studies/__init__.py` | 4 |
| `analytics/volatility/__init__.py` | 4 |
| `analytics/volume/__init__.py` | 4 |
| `application/__init__.py` | 1 (`orders` — also in 1A, so this dir dies together) |
| `interface/auth/__init__.py` | 5 (all in 1A → dir dies together) |
| `reactive/__init__.py` | 3 (`bus`, `event_log`, `thread_safe_bus` — **live**) |
| `replay/__init__.py` | 4 (`backtest`, `optimization`, `synthetic_ticks`, `walk_forward` — **live**) |
| `strategy/core/__init__.py` | 5 (**live**) |
| `strategy/extensions/scanners/__init__.py` | 3 (**live**) |

**Sequencing note:** `interface/auth/*` and `application/*` become empty after 1A, so those two *directories* can go entirely (marker + all children). `reactive/`, `replay/`, `strategy/core/`, `strategy/extensions/scanners/`, and the four `analytics/*` subpackages must keep their `__init__.py`.

**Net safe deletion: 33 files / 363 LOC now, plus 2 directories that become empty** (`interface/auth/`, `application/`).

---

## 2. MERGE — 91 TEST-ONLY shims (1,001 LOC)

These are **not dead in production but dead-in-production, live-in-tests**. The shim tree is the *only* thing keeping ~91 module paths importable, and the test suite is the dominant consumer: **109 of 154 shims are referenced from `trading/tests/`** (e.g. `test_indicator_registry.py` alone has 83 import lines; `test_fastapi_app.py` 29; `test_remaining_gaps.py` 28).

**This is the real cost centre: the migration debt is in the test suite, not in production.** Deleting any of these breaks tests immediately.

Highest-value merges (retarget the import, then delete the shim):

| Shim | LOC | Referenced by (sample) | Real target to use |
|---|---|---|---|
| `execution/engine.py` | 11 | `benchmarks/bench_order_path.py:21` + many tests | `tradex_execution.engine` |
| `execution/fill_sources.py` | 11 | `benchmarks/bench_order_path.py:22` + many tests | `tradex_execution.fill_sources` |
| `reactive/bus.py` | 11 | `benchmarks/bench_bus_throughput.py:20`, `bench_order_path.py:23`, ~8 test files | `tradex_reactive.bus` |
| `datalake/{gap_detector,parallel_fetcher,parquet_storage,simple_sync,symbol_resolve,universe}.py` | 66 | **12 `trading/scripts/`** + tests | `tradex_market_data.*` |
| `config/env.py`, `config/schema.py` | 8 | 10 `trading/scripts/` | `tradex_config.env`, `tradex_config.schema` |
| `runtime/live.py` | 11 | 6 `trading/scripts/` | `tradex_runtime.live` |
| `replay/{optimization,walk_forward}.py` | 22 | `trading/scripts/backtest_datalake.py:314,359,360,410,411` | `tradex_replay.*` |
| `analytics/*` (the 40+ registry/indicator shims) | ~450 | `test_indicator_registry.py` (83 lines) | `tradex_analytics.*` |

**Scripts are the cheapest win:** 12 of 14 `trading/scripts/*.py` import shim paths with `# noqa: E402` comments. They are not run by CI, so retargeting them is zero-risk. `backtest_datalake.py`, `sync_today.py`, `fill_gaps.py`, `topup_gaps.py`, `repair_gaps.py` are the densest.

---

## 3. KEEP — only 2 shims are genuinely load-bearing in production

| Shim | LOC | Why it must stay (or be fixed) |
|---|---|---|
| `runtime/startup.py` | 11 | **The only production code path that transits a shim.** `trading/src/tradex_trading/sdk/session.py:380` and `:483` both do `from tradex_trading.runtime.startup import boot` |
| `interface/cli.py` | 11 | Sole `[project.scripts]` entry point: `trading/pyproject.toml:38` → `tradex = "tradex_trading.interface.cli:main"` |

Two apparent "ACTIVE-PROD" hits were **disproved** on inspection — they are docstrings, not imports:

| Apparent ref | Reality |
|---|---|
| `brokers/src/tradex_brokers/common/paths.py:36` | Inside a module docstring: ``"the exact pattern ``tradex_trading.datalake.paths`` uses…"`` — prose. No import. |
| `domain/src/tradex_domain/events.py:102` | Sphinx cross-ref in a dataclass docstring: ``:class:`tradex_trading.runtime.feed_integrity.GapKind` `` — prose. No import. |
| `brokers/tests/test_paper_broker_no_trading_import.py:33-34` | A test that *deliberately blocks* `tradex_trading` in `sys.modules` to prove brokers work without it. |
| `brokers/tests/test_v3_port.py:1192` | A real import, but in a test. |

**Definitive proof of the production surface** — every real `import`/`from` statement naming `tradex_trading` in non-test, non-script, non-benchmark source, in the entire repo:
```
$ git grep -nE "^\s*(from|import)\s+tradex_trading" -- '*.py' | grep -v "/tests/" | grep -v "test_"
trading/src/tradex_trading/sdk/__init__.py:6,7          (self-package)
trading/src/tradex_trading/sdk/session.py:380            ← through a shim
trading/src/tradex_trading/sdk/session.py:483            ← through a shim
```
**That is the whole production surface: 2 lines.**

---

## 4. STEP 1b — Entry points

| Entry point | Kind | Called by | Verdict |
|---|---|---|---|
| `tradex` console script → `tradex_trading.interface.cli:main` | CLI | `trading/pyproject.toml:38`; impl at `interfaces/src/tradex_interfaces/cli.py:254` | **WIRED** (packaging) — but **0 CI invocations** |
| `boot()` — `runtime/src/tradex_runtime/startup.py:155` | composition root | `sdk/session.py:380,483`; `boot_context` at `:990`; ~60 test call sites | **WIRED** — exercised by `trading/tests/contracts/test_session_smoke.py` (20 calls) which CI runs |
| `create_app()` — `interfaces/src/tradex_interfaces/fastapi_app.py:81` | FastAPI factory | `cli.py serve` → `fastapi_app.py:541` | **WIRED** (via CLI) — 0 CI |
| `serve_app` / uvicorn | ASGI | `fastapi_app.py:471,525` | **WIRED** (via `serve`) — 0 CI |
| CLI subcommands: `quote, order, health, scanner, positions, account, orders, watch, serve, sync` | CLI | `cli.py:37-102` | **MANUAL-ONLY** — 0 CI |
| `mypy trading/src/tradex_trading` | CI step | `.github/workflows/quality-gate.yml:64` | **WIRED** — `continue-on-error: true` (non-blocking!) |
| `pytest trading/tests/parity trading/tests/contracts` | CI step | `.github/workflows/parity.yml:27,29,31` | **WIRED** — blocking |
| 14 `trading/scripts/*.py` | ops scripts | **nothing** | **ORPHANED from CI** — manual only |
| 4 `benchmarks/bench_*.py` | benchmarks | **nothing** | **ORPHANED** — manual only |

**Entry-point defects worth fixing before deploy:**

1. **`quality-gate.yml:64` is non-blocking.** `mypy trading/src/tradex_trading` carries `continue-on-error: true`. The shim tree is type-checked but *cannot fail the build*. Every other package step has the same escape hatch (lines 39, 43, 47, 51, 55, 59).
2. **`parity.yml:25` installs only `-e ./domain -e ./brokers -e ./trading`** — but `trading/pyproject.toml:10-27` declares 16 `tradex-*` dependencies. The parity gate therefore relies on transitive resolution rather than an explicit workspace sync, unlike `quality-gate.yml:27` (`uv sync --frozen`). Fragile.
3. **No CI job runs the CLI, the FastAPI app, or any script.** The shim-backed console script and the whole 14-script ops surface are untested in CI.

---

## 5. STEP 2 — Live execution flow: CLEAN, no shim hops

Traced end-to-end. **Every hop uses real package paths. The live money path never transits a shim.**

```
1. boot(cfg, ...)                        runtime/src/tradex_runtime/startup.py:155
2.   _safe_teardown / _boot_tail          runtime/src/tradex_runtime/startup.py:315 / :356
3.   → ExecutionEngine(...)               runtime/src/tradex_runtime/startup.py:31 (imported)
4.   → ReactiveStrategyEngine(...)        runtime/src/tradex_runtime/startup.py:45 (imported)
5.   → ScannerEngine(...)                 runtime/src/tradex_runtime/startup.py:46 (imported)
6.   → all_scanners / all_strategies      runtime/src/tradex_runtime/startup.py:47 (auto-discovery)

  --- order flow ---
7. strategy emits Signal                  strategy/src/tradex_strategy/core/engine.py:290 (_maybe_publish_order)
8. bus.publish(PlaceOrderCommand)         strategy/src/tradex_strategy/core/engine.py:347
9. engine CQRS subscription               execution/src/tradex_execution/engine.py:295  (of_type(PlaceOrderCommand))
10.  → _process_request                   execution/src/tradex_execution/engine.py:296 → :413
11.  → _run_pipeline                      execution/src/tradex_execution/engine.py:709 → :427
       · kill switch                      execution/src/tradex_execution/engine.py:445
       · feed readiness gate              execution/src/tradex_execution/engine.py:455-470
12.  _append(BrokerOrderRequested)        execution/src/tradex_execution/engine.py:564   ← DURABILITY BARRIER
13.  fill.submit(request)                 execution/src/tradex_execution/engine.py:572
14.    → BrokerFillSource.submit          execution/src/tradex_execution/fill_sources.py:252
15.      broker.submit_order(request)     execution/src/tradex_execution/fill_sources.py:272
16.        (PaperBroker / Dhan / Upstox)  brokers/src/tradex_brokers/  — constructed at startup.py:21
17.  inbound live fill                    execution/src/tradex_execution/engine.py:304 (of_type(OrderFilled)) → _apply_fill :305
18. persistence: event_store._append      execution/src/tradex_execution/engine.py:313 (_append), wired :946 (_attach_event_store)
```

**Verdict on the critical question:** 0 of 18 hops transit a shim. The one production shim import (`sdk/session.py:380,483`) is at step 1's *entry*, not inside the order pipeline, and is trivially retargetable.

**Consistency defect at `sdk/session.py`:** lines 19-40 import 10 packages by their real paths (`tradex_config.schema`, `tradex_execution.engine`, `tradex_reactive.bus`, `tradex_runtime.streaming`, …) — then lines 380 and 483 reach back through the shim for `boot`. Same file, same imports, two different conventions. There is no circular-import reason: `tradex_runtime.startup` does not import `tradex_trading`. **One-line fix, zero risk.**

---

## 6. STEP 3 — Shotgun surgery

| # | Concept | Files requiring simultaneous edit | Note |
|---|---|---|---|
| 1 | **The shim tree itself** | 1 shim + 1 real target + N importers | Every shim is a 2-file edit by construction. 154 of them = 308 files. **This is the dominant shotgun.** |
| 2 | **Datalake namespace** | `trading/…/datalake/*` (13) + `market_data/src/tradex_market_data/*` (13) + 12 scripts | *Not* duplicate logic — pure alias. `datalake/paths.py:5` → `_import_module("tradex_market_data.paths")`. But every datalake import needs 2 coordinated edits (importer + shim) forever. |
| 3 | **`sys.path` bootstrap** | **14 of 14** `trading/scripts/*.py` | Each hand-rolls `sys.path.insert(0, str(ROOT / sub))` then imports shims with `# noqa: E402`. Duplicated 14×. |
| 4 | **Analytics indicator registry** | `analytics/` shims (~40) + `test_indicator_registry.py` (83 import lines) | A single 1,464-line test file pins the entire shim surface. |
| 5 | **Config namespace** | `trading/…/config/` (3) + `config/src/tradex_config/` + `tests/test_import_boundaries.py:209` | The boundaries test *iterates the shim dir* — deleting it makes the test vacuous, not failing. |
| 6 | **Research namespace** | `trading/…/research/` (3) + `research/src/tradex_research/` + `tests/test_import_boundaries.py:474` | Same vacuous-pass trap. |
| 7 | **`tradex_trading` allowlist** | `tests/test_import_boundaries.py:38,98,102,157,257-278` | 31 mentions; the runtime-allowlist encodes the strangler state. |

---

## 7. Step 4 — DELETE / MERGE / KEEP

### DELETE (safe now, zero risk)
1. **32 leaf shim files — 352 LOC** (table in §1A) + `strategy/extensions/shared/` (§1B) = 33 files / 363 LOC
2. **`trading/src/tradex_trading/strategy/extensions/shared/`** (whole dir) and **`strategy/src/tradex_strategy/extensions/shared/`** (whole dir) — both empty, both unreferenced
3. **After 1A, delete 2 now-empty dirs**: `interface/auth/`, `application/` (marker + all children gone)

### MERGE (retarget importer, then delete shim — ordered by safety)
1. **12 `trading/scripts/*.py`** → real packages. Zero CI risk. Unlocks 8 more shims.
2. **4 `benchmarks/bench_*.py`** → real packages. Unlocks 3 shims.
3. **`sdk/session.py:380,483`** → `from tradex_runtime.startup import boot`. Unlocks `runtime/startup.py`. **2 lines, do this first.**
4. **`trading/pyproject.toml:38`** → `tradex = "tradex_interfaces.cli:main"`. Unlocks `interface/cli.py`.
5. **`trading/tests/`** (109 shim-referencing modules) → real packages. Unlocks the bulk of the tree. Highest effort, do last.
6. **Delete `tradex_trading/config/` + `tradex_trading/research/`** only *after* also removing the now-vacuous boundary tests at `test_import_boundaries.py:206,499`.

### KEEP
1. **5 real-code files** (§0) — 911 LOC of genuine production code
2. **10 package-marker `__init__.py`** (§1C) — until their subpackages die
3. **6 "dead" shims that are actually pinned** — none; recheck after scripts merge
4. **`mypy trading/src/tradex_trading`** in CI — but make it **blocking** (drop `continue-on-error`)

---

## 8. Deployment risks (unresolved, flagged not fixed)

1. **23 shim files are UNTRACKED in git** — present in the working tree, absent from the commit. A fresh `git clone` + `uv sync` **will not have them**:
   ```
   application/{__init__,orders}.py
   execution/{envelopes,recovery,sqlite_event_store}.py
   interface/auth/{__init__,csrf,deps,rate_limit,routes,session_store}.py
   interface/{replay_guard,replay_run}.py
   research/{__init__,manifests,pit}.py
   runtime/{feed_integrity,feed_monitor,feed_recovery,feed_supervisor}.py
   strategy/{artifacts,registry,runtime}.py
   ```
   **This is the single biggest pre-deploy hazard.** CI would behave differently from this machine. Note `interface/replay_guard.py` and `runtime/feed_supervisor.py` are *live* (test-referenced) yet untracked.
2. **482 files modified, uncommitted.** The `trading/.../analytics/goldens/*.json` entries show as `D` in `git status`, but this is a **legitimate move, not data loss**: all 90 golden files now live in `analytics/src/tradex_analytics/goldens/` (verified: 90 present there, 0 remaining in `trading/`). Any test still resolving goldens via the old `tradex_trading.analytics.goldens` path would break — `trading/tests/analytics/test_goldens_provenance.py` and `test_frontend_contract_parity.py` are the ones to re-check.
3. **`quality-gate.yml:64` cannot fail the build** (`continue-on-error: true`).
4. **`parity.yml:25` installs 3 of 17 workspace packages.**

---

## 9. UNVERIFIED / out of scope

- **Runtime reachability not empirically executed.** Deletion safety is proven by static reference analysis (3 independent methods), not by running the suite with files removed. Recommend: delete §1A, run `python -m pytest` (root config) to confirm.
- **`apps/`** — the directory exists at repo root but was not in scope and produced no `tradex_trading` references.
- **Untracked files outside `trading/src/`** were not reference-graphed; my corpus was the 917 git-tracked files. Untracked test files could in principle import a "dead" shim. *Low risk* — the working tree's test files are tracked per `git status`.
- **`persististence`/observability/operations** packages are thin (19/176/41 LOC) and may have their own duplication, but were out of scope.

---

## 10. Recommended order of operations

```
1. git add the 23 untracked shim files        ← fixes the deploy hazard FIRST
2. sdk/session.py:380,483 → tradex_runtime.startup   (2 lines)
3. trading/pyproject.toml:38 → tradex_interfaces.cli:main
4. retarget 12 trading/scripts + 4 benchmarks  (zero CI risk)
5. DELETE the 32 leaf shims + shared/ + 2 empty dirs ← verify with full pytest
6. retarget trading/tests/ (bulk migration)
7. make mypy trading/src/tradex_trading blocking
```
Steps 2-4 unblock roughly 40 shims and remove the only two production shim traversals.
