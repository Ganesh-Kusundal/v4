> ## ⚠️ SUPERSEDED IN PART — read `trading-deep-review-findings.md` FIRST
>
> A 6-agent deep review (2026-09-26, after `df377a0`) **overturned three conclusions in this document.** Acting on §6 (DELETE) or §F (Plan) as written **would have destroyed working code and left security holes open.**
>
> | This document says | Deep review measured |
> |---|---|
> | 40 dead shims | **17** dead — 18 leaf shims have live test importers |
> | `_PRODUCT_BY_WIRE` unused, 1 occurrence | **ALIVE** — 13 tests assert its exact contents |
> | `application` package DEAD | **ALIVE** — deleting it **stops the server booting** |
> | `position_manager.py`: 2 importers | **4** production importers |
> | `trading/` = 24,672 LOC | **2,659 src** + 3,629 scripts + 53,138 tests |
> | mypy 475 errors of debt | **14 real** (465 are `py.typed` artifacts) |
> | `cli.py` has no teardown | It has two `finally` blocks; **SIGTERM bypasses them all** |
> | money path "sound" | 3 **Critical** sequencing/concurrency defects |
>
> **Also missing entirely from this document:** 26 of 42 mutating HTTP operations have no auth (incl. unauthenticated `PUT`/`DELETE /api/charts/workspace/{id}`); CI has **never run a single test** (`ruff check` halts the job at step 4); 90 stale pre-bump goldens ship in the wheel; 61 tests never execute.
>
> **Still valid:** §B (finish the extraction — do not roll back), §C target package map, §E.5 KEEP list, and the gate discipline. **M1/M2 remain complete and correct** (`f0ebfed`, `df377a0`).

# Pre-Deployment Review — Independent Validation, Target Architecture & Implementation Plan

**Repo:** `/Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4`
**Date:** 2026-09-26
**Supersedes nothing:** `trading-predeploy-review.md` (2026-09-26 14:27) remains the first-pass review. This document **independently re-verifies** that review, corrects five material errors in it, adds six findings it missed, and produces the target architecture plus a gated migration sequence.

**Status:** analysis only. No code was modified. No migration phase was started.

---

## Verified baseline

| Fact | Value | How established |
|---|---|---|
| Test suite | **4,194 passed / 7 failed / 3 skipped** (110s) | `pytest` full run |
| Money path | **correct** | 6 runtime probes (§A) |
| Package graph | **DAG, 0 cycles, 42 cross-package edges** | AST + the repo's own guard |
| Architecture guard | **green, 23 tests** | `pytest tests/test_import_boundaries.py` |
| `ruff check` | **exit 1** | `ruff check` |
| `mypy` (outside `domain`) | **475 errors** | per-package runs |
| UI artifact | **stale** | `npm run verify-artifact` fails |
| Tree state | **236 M / 91 D / 135 untracked**; 11 of 16 packages 100% uncommitted | `git status` |

**Exact size (corrected — see §A.2):** 16 packages · **404 src files / 56,383 src LOC** · 364 test files / **69,231 test LOC** · 15 scripts / 3,629 LOC.

---

## A. Review Validation

### A.1 Verdict summary

The first-pass review was directionally right and materially incomplete. Its architectural conclusions hold. Its **LOC accounting was wrong by an order of magnitude**, and it **missed a Critical operational defect on the deployment path** while reporting that area as "duplicate code, one copy dead."

### A.2 Corrections to the prior review

| # | Prior claim | Verdict | Evidence |
|---|---|---|---|
| 1 | `tradex_trading.interface.cli` shim is "test-only, deletable" | **INCORRECT** | It is the **only declared entry point in the repo**: `trading/pyproject.toml:38` → `tradex = "tradex_trading.interface.cli:main"`. `interfaces/pyproject.toml` declares **no** `[project.scripts]`. `README.md:228` documents `tradex serve` as *the* run command. Deleting it as "test-only" breaks the primary deployment path. |
| 2 | "24,672 LOC in `trading/`; deletion saves 24,672" | **INCORRECT** | `trading/src` = 159 files / **2,659 LOC** (154 shims = 1,673; 5 real files = 986). `trading/scripts` = 15 files / **3,629 LOC of real operational tooling** (backfill, gap repair, reconcile, audit, e2e seed) that must be **moved, not deleted**. The 24,672 figure was an artifact of a `find` that swept in `trading/tests`. |
| 3 | "337,160 total Python LOC" | **INCORRECT** | Real total: **56,383 src + 69,231 test = 125,614**. The 337k figure swept in `.kilo/worktrees/sour-ragdoll/` (a full duplicate repo), `graphify-out/`, and `services/duckdb-analytics/.venv/`. |
| 4 | FillSource gap is a *live* double-projection risk | **PARTIAL** | Runtime: 3 of 4 sources (`Simulated`, `Paper`, `Replay`) lack **both** members; only `BrokerFillSource` defines them. The `False` default is currently *correct* for those 3. It is a **latent contract gap**, not an active bug. |
| 5 | "Shutdown duplication — two implementations, one dead" | **UNDERSTATED — materially** | Not duplication. `tradex serve` has **no shutdown at all** (see N1/N2). This inverts the finding. |

### A.3 New findings the prior review missed

| # | Finding | Evidence | Severity |
|---|---|---|---|
| **N1** | **`tradex serve` never calls `session.stop()`.** `start_fastapi_server` ends at `uvicorn.run(app, ...)` (`fastapi_app.py:542`) with no `try/finally`; `create_app` (81–366), `serve_app` (427–460) and `start_fastapi_server` (463–542) contain **zero** shutdown handling. The strings `lifespan`, `atexit`, `signal`, `shutdown`, `on_event` appear **nowhere** in the 542-line file. | AST scan of `interfaces/src/tradex_interfaces/fastapi_app.py` | **Critical** |
| **N2** | **`engine.shutdown()` has zero production callers** — only `engine.py:411` (`__exit__`). So on the server path the kill switch is never set, and the SQLite idempotency-guard + event-store `close()` methods (`sqlite_store.py:318/484`, `sqlite_event_store.py:239`) never run. The code's own comment: *"released on shutdown — not leaked for the process life."* | Call-graph scan across `interfaces/src`, `runtime/src`, `trading/src`, `trading/scripts` | **Critical** |
| **N3** | **Two strategy catalogs with different contents.** `extensions/strategies.__all__` (8 strategies, drives live boot) vs `interfaces/routes/chart.py::_STRATEGY_FACTORIES` (3, drives backtest). `bracket_breakout` — CLAUDE.md's *"reference producer"* for the bracket claim — is in the route catalog only. Resolved by `importlib.import_module` **inside the request handler** (`chart.py:146–152`): untyped, mypy-invisible, HTTP 500 on rename. | Runtime: `_build_strategy('bracket_breakout', …)` succeeds; `__all__` omits it | High |
| **N4** | **`auto_register_examples: bool = False`** (`config/schema.py:122`) ⇒ **by default zero strategies are registered at boot**. Only 5 tests opt in. | Config read + `startup.py:517` | Medium |
| **N5** | **`submit()` returns `OrderReceipt \| OrderId`** — a union return contract at the public boundary (same dual-contract smell as `sync` inside the pipeline). | Runtime probe | Low |
| **N6** | Ruff resolves config from a **`pyproject.toml in the parent directory, outside the repo** (no root `[tool.ruff]`). The 738-error figure is environment-specific; CI-equivalent is 3,283. Gate is red either way; the number is not portable. | `ruff check --show-settings` | Low |

### A.4 Confirmed as stated

**VERIFIED**

- 40 dead shims (0 refs, working-tree AST scan of 847 `.py` files)
- 4 dead packages: `persistence` (19), `operations` (41), `research` (445) have **0 non-test importers**; `application` (100) has **1** caller
- `_PRODUCT_BY_WIRE` (`interfaces/routes/orders.py:47`) — 1 occurrence tree-wide; duplicates `ProductType.from_wire`
- Universe 3-hop chain (`domain.universe` → `market_data.universe` → `trading.datalake.universe`) with CLAUDE.md documenting the 2-hop shim
- `FillSource` declares 3 of 5 required members
- `ExecutionEngine` 15-param constructor; `_run_pipeline` = 302 lines with 6 `if sync`
- `ws_stream` = 1,464-line function inside a 1,512-line file
- Bar-key bucketing implemented 3× across 2 files + 3 private `_tf_seconds` reads
- Test placement: 2,806 of 4,204 tests in `trading/tests`; 6 packages have only a 5-line import smoke
- `ruff check` exits 1; 475 mypy errors outside `domain`; 7 of 10 mypy steps are `continue-on-error`
- `parity.yml` installs 3 of 16 packages
- `verify-artifact` fails — stale bundle
- CLAUDE.md describes 7 directories; **8 of the 11 new packages have zero mention**
- 6 competing architecture documents

**REFINED**

- *"The graph is a tree"* is imprecise. The package graph is already an **acyclic DAG** (the guard's `TYPE_CHECKING` exclusion is correct) across **42 edges**. The real defect is **width**, not cycles: **`interfaces` imports 11 of 16 packages**. The 23-test architecture guard is **green**.

### A.5 Money-path verification (the decisive input)

Established by execution, not inspection:

```
IDEMPOTENCY replay      → same OrderId, returned as OrderId (documented union)
payload mismatch (cid)  → IdempotencyKeyReuseMismatch          ✓
cash after 1 fill       → 100000 → 75000                        ✓
kill switch             → REJECTED, active=True                 ✓
Order FSM               → FILLED→NEW raises SessionStateError   ✓
duplicate fill replay   → cash 75000 → 75000, no kill trip      ✓
```

**The engine's problems are structural and testability-related, not correctness.** This is the single most important input to the architecture decision (§B).

### A.6 Method

| Question | Method | Validated by |
|---|---|---|
| Structure, LOC, entry points | Census over 847 working-tree `.py` | Static |
| Dependency direction | AST import graph, package level, `TYPE_CHECKING` excluded | Static |
| Shim reachability | AST scan of the **working tree** — *not* `git ls-files`/`git grep`, because 11 packages are untracked and git-based search under-reports (it reported 53 dead shims vs the correct 40) | Static |
| Dynamic loading / plugins | Explicit audit of `importlib`, `pkgutil`, `__subclasses__`, `[project.scripts]`, `__all__`-driven discovery | Static |
| Does it run? | `boot_context()` in paper mode; order submit; strategy build | **Runtime** |
| Behavioural correctness | Full suite + 6 money-path probes | **Runtime** |
| Static quality | `ruff check`, `mypy` per package | Static |
| UI deployability | `npm run verify-artifact` | **Runtime** |

**Uncertainty:** `poc/` and `benchmarks/` were excluded from the shim reference scan (dev tooling; cannot change a production conclusion). Live-broker paths (Dhan/Upstox auth, WS reconnect, 401 retry) were **not** executed — no credentials, and CLAUDE.md forbids investigating auth internals.

---

## B. Architecture Decision

## Option A — finish the extraction, with two narrow additive corrections.

**Why not B (roll back).** Rolling back re-creates a 597-file monolith. The DAG is already acyclic, the guard is green, `domain` is clean and well-tested, and 12 of 16 packages are genuinely used. Rolling back trades a solved layering problem for an unsolvable file-count problem.

**Why not C (redesign part).** The money path is verified correct. `domain` is verified good. The package graph has no cycles. A redesign would put verified-correct code at risk to solve a problem a mechanical import rewrite solves completely.

**The evidence that decides it:**

```
trading/src = 159 files, 2,659 LOC
  ├─ 154 shims ........ 1,673 LOC   (pure indirection)
  └─   5 real files ....   986 LOC   (sdk/session 509 + live_fill_bridge 283 + …)
```

**Exactly one** shim is used by production source (`runtime.startup`, via `sdk/session.py`). The extraction is **~97% complete**. The remaining work is **mechanical** (repoint 97 test imports; relocate 283 test files) plus **one** real extraction (`TradingSession`) — not a redesign. The dominant cost is *moving 53,138 LOC of tests*, which carries near-zero behavioural risk.

**The two corrections (Option C applied to two seams only):**

1. **Shutdown** — N1/N2 are not duplication, they are a **missing** capability. Keep `ShutdownCoordinator`, make it reachable, delete the inline teardown in `session.stop()`. Additive.
2. **Strategy catalog** — N3 is genuine duplicate ownership. One catalog, imported not string-matched. Additive.

Both *reduce* risk. Neither touches the money path's logic.

---

## C. Package Map

**16 packages → 12.**

| Package | src LOC | Verdict | Justification |
|---|---|---|---|
| `domain` | 3,630 | **KEEP** | Pure, zero internal deps, mypy-clean, 430 tests. Best code in repo. |
| `brokers` | 12,939 | **KEEP** | Venue I/O; 866 tests — best-tested component. |
| `execution` | 5,880 | **KEEP** | Money path. Verified correct. |
| `interfaces` | 6,438 | **KEEP, narrow** | HTTP/WS/CLI. Fan-in 11 → 2. |
| `runtime` | 4,313 | **KEEP + absorb** | Composition root; absorbs `TradingSession`. |
| `market_data` | 2,457 | **KEEP** | Parquet lake; real, distinct responsibility. |
| `analytics` | 12,022 | **KEEP** | Pure indicator compute. Biggest test debt (1 test). |
| `strategy` | 2,608 | **KEEP** | Strategy compute + extension discovery. |
| `replay` | 1,569 | **KEEP** | Backtest / walk-forward / optimization. |
| `config` | 419 | **KEEP** | `AppConfig` contract, 4 dependents. |
| `reactive` | 668 | **KEEP** | Bus primitive, 5 dependents. |
| `observability` | 176 | **KEEP** | Cross-cutting metrics; folding saves ~nothing and costs a migration. |
| **`persistence`** | **19** | **DELETE** | Pure re-export of `tradex_execution.sqlite_store`. Its own docstring says *"owned by execution"*. 0 non-test importers. |
| **`operations`** | **41** | **DELETE** | A dict of markdown paths. 0 non-test importers. Not code. |
| **`research`** | **445** | **DELETE pkg, SALVAGE `pit.py`** | 0 production importers, but 44 tests. `pit.py` (PIT / lookahead-bias validators) is genuinely useful → move to test-support and keep its tests. `manifests.py` → verify, then delete. |
| **`application`** | **100** | **DELETE** | 1 caller. 3 of 4 functions are `return engine.x(request)`. Salvage `BrokerCapabilityError` + the capability gate into the route/engine. |
| **`trading/`** | **2,659** | **DELETE src** (scripts + tests **MOVE**) | 154 shims + 5 real files. `scripts/` (3,629 LOC) and `tests/` (53,138 LOC) relocate, never deleted. |

### Target dependency direction

```
                    tradex_domain          (pure · zero internal imports)
                         ▲
      ┌──────────┬───────┴───────┬──────────┬──────────┐
   brokers    execution     market_data   strategy    analytics
      │          │                              │           │
      └──────────┴──────── replay ─────────────┴───────────┘
                         ▲
                  tradex_runtime              ← the ONLY composition root
                         ▲
                         │
                  tradex_interfaces           ← imports domain + runtime ONLY
```

**Enforced as 5 rules:**

1. `domain` imports nothing internal.
2. Exactly one composition root.
3. `interfaces` imports **only** `domain` + `runtime`.
4. No cross-package private reads.
5. Protocols are complete — if the engine calls it, the Protocol declares it.

### State ownership (unchanged — already correct)

| Concern | Owner |
|---|---|
| Orders / positions / cash | `ExecutionEngine` |
| Derived projection | `TradingCache` |
| Historical bars | Parquet lake (`market_data`) |
| Venue truth | `BrokerAdapter` |
| Configuration | `AppConfig` (`config`) |

### Integration boundaries

`BrokerAdapter` (venue) · `ParquetMarketProvider` (lake) · `FillSource` (execution mode) · FastAPI/WS (HTTP) · the one dynamic `import_module` in `chart.py` (**to be removed**).

---

## D. Execution Flow

### Current (verified by execution)

```
tradex → [shim] → tradex_trading.interface.cli → tradex_interfaces.cli
  → boot(AppConfig)                                    runtime/startup.py:155 (1,005 L)
  → ExecutionEngine(bus, fill_source, risk, guard,     execution/engine.py:191
       cache, metrics, fee_calculator, cash, clock,
       feed_supervisor, event_store, require_risk,
       require_feed, require_durable_events)           ← 15 params
  → _run_pipeline(request, sync=True)                  engine.py:427  ← 302 L, 6× `if sync`
       kill-switch → feed → risk → guard.reserve → risk.check
       → event-append → fill.submit → OMS → cash
  ⇒ OrderReceipt(FILLED), cash 100000 → 75000
  ✗ SHUTDOWN: uvicorn.run(app) returns; session.stop() never called
```

### Target

```
tradex → tradex_interfaces.cli:main                    (entry point lives in interfaces/)
  → boot(AppConfig) -> TradingSession                  runtime/startup.py
  → engine.submit(request) -> _run_pipeline(request)   ← no `sync` parameter
       returns Outcome; submit() shapes the receipt
  ⇒ OrderReceipt(FILLED)
  ✓ SHUTDOWN: ShutdownCoordinator
     (strategies → engine → broker → session → writer_lock)
     wired into TradingSession.stop(); kill switch set;
     idempotency guard + event store closed
```

**Two changes only:** `sync` leaves the pipeline; shutdown becomes reachable and ordered. **The order → fill → position → cash logic is untouched.**

---

## E. Delete / Move / Merge / Redesign / Keep

### E.1 DELETE — provably safe

Reachability established four ways: AST import scan of the working tree · `__all__` chain inspection · `[project.scripts]` / entry-point audit · dynamic-import (`importlib` / `pkgutil` / `__subclasses__`) audit.

| Target | LOC | Safety proof |
|---|---|---|
| 40 dead shims | ~440 | 0 importers across 847 working-tree `.py`; all targets exist on disk; not in any `__all__` chain; not a declared entry point; not dynamically imported (the only `import_module` call sites are the shims' own templates plus `chart.py`'s `tradex_strategy.*`) |
| `persistence/` + `pyproject` | 19 | 0 non-test importers; pure re-export of `execution` |
| `operations/` + `pyproject` | 41 | 0 non-test importers; a dict of doc paths |
| `research/` package shell | ~200 | 0 production importers (`pit.py` **salvaged** to test-support) |
| `application/` | 100 | 1 caller; 3 of 4 functions are pass-throughs; capability gate salvaged |
| `position_manager.py` alias | 13 | `PositionAccountant` is the sole implementation; repoint 2 importers first |
| `_PRODUCT_BY_WIRE` | 6 | 1 occurrence tree-wide (its own declaration); `ProductType.from_wire` is the real owner |
| `runtime/calendar.py` | 5 | 2-hop facade → `tradex_domain.trading_calendar` |
| `market_data/universe.py` | 21 | Re-export of `tradex_domain.universe` |
| `runtime/session.py` | 9 | 2-hop facade; dissolves when `TradingSession` moves |
| 5 of 6 architecture docs | ~110 KB | Superseded; 5 contradict current code |
| `.kilo/worktrees/`, `graphify-out/`, `services/…/.venv/`, `$JCODE_SCRATCH_DIR/` | ~340 MB | gitignored build/tool output, incl. a full duplicate repo |

### E.2 Explicitly NOT deleted

| Item | Why it stays |
|---|---|
| `bracket_breakout.py` (10 KB) | "Dead" only to auto-discovery. **Live** via `chart.py`'s catalog and used by 3 Playwright specs. Deleting it would break the backtest bracket surface. |
| `ShutdownCoordinator` | Dead, but it is the **correct** implementation of ordered teardown. Make it reachable rather than delete it. |
| `trading/scripts/` (3,629 LOC) | Real operational tooling — backfill, gap repair, reconcile, audit, e2e seed. **Move**, never delete. |
| `trading/tests/` (53,138 LOC) | The behavioural safety net. **Move**, never delete. |
| 90 analytics goldens | Parity ground truth. `git mv`, never delete. |

### E.3 MERGE

- 3× bar-key bucketing → one public `bar_key()` in `tradex_domain.timeframe`; kills 3 private `_tf_seconds` reads.
- 2× strategy catalog → **one** `__all__`-driven catalog; `chart.py` imports it instead of `importlib`-ing a string.
- 2× shutdown → `ShutdownCoordinator` only.
- 2× universe path → `tradex_domain.universe` only.
- `position_manager` alias → `PositionAccountant`.

### E.4 REDESIGN — two seams, both additive

1. **Shutdown** — `ShutdownCoordinator` becomes the single teardown, wired into `TradingSession.stop()`. Fixes N1/N2.
2. **`_run_pipeline`** — remove the `sync` parameter; the pipeline returns an outcome and `submit()` shapes it. **Logic unchanged** — signature refactor, verified against the 4,194-test baseline.

### E.5 KEEP — do not touch

- `domain` in its entirety, including the `Order.transition_to` FSM and `Price` / `Quantity` / `Money`
- `fill_model.FillModel` cross-mode parity
- `protective_request()` and the single-declaration-path rule
- `position_math.apply_fill` as the single money authority
- `brokers` (866 tests)
- The idempotency fingerprint and `IdempotencyKeyReuseMismatch`
- Duplicate-fill dedup
- The 23-test architecture guard's **intent**
- `interfaces/auth/**` — separate concern, already isolated

---

## F. Ordered Implementation Plan

### F.1 MUST FIX BEFORE ANY REFACTOR — correctness, reproducibility, confidence

| # | Change | Gate |
|---|---|---|
| **M1** | **Commit the tree.** 236 M / 91 D / 135 untracked; 11 packages 100% uncommitted (`git ls-files interfaces` = 0). Record the `4,194/7/3` baseline in the commit message. | CI runs on the real tree |
| **M2** | **Add `[project.scripts] tradex = "tradex_interfaces.cli:main"` to `interfaces/pyproject.toml`.** Verify `tradex --help`. Protects the entry point *before* any shim moves. | `tradex` runs without `trading/` |
| **M3** | **Fix the server shutdown (N1/N2).** Wire `ShutdownCoordinator` into `TradingSession.stop()`; add a FastAPI lifespan calling it. Add a test asserting the idempotency guard and event store are closed. | New test green; no regression |
| **M4** | **Resolve the 7 failing tests.** Rewrite `tests/runtime_audit/` against the real API, or delete it. | **Suite green: 4,194 / 0 / 3** |
| **M5** | **Declare the 2 `FillSource` members** on the Protocol; delete the 5 `getattr` probes; assert each source's values. Add a guard test. | 4 sources pass contract test |

> **M3** is first among code changes: a **live operational defect on the deployment path**, and purely additive.
> **M5** precedes any engine refactor so the contract is explicit before the signature changes.

**The 7 current failures** are all API drift in a 320-LOC hand-written audit harness (`tests/runtime_audit/`): `boot() got an unexpected keyword argument 'mode'`, `ExecutionEngine.__init__() got an unexpected keyword argument 'cash_ledger'`, `BrokerFillSource.__init__() got an unexpected keyword argument 'adapter'`, `Quantity.value must be Decimal`.

### F.2 SAFE STRUCTURAL CLEANUP — no behaviour change

| # | Change | Gate |
|---|---|---|
| **S1** | Delete the 4 dead packages + all `pyproject` / workspace-member / testpath / boundary-table entries. Salvage `pit.py` → test-support; salvage `BrokerCapabilityError`. | 4,194 green |
| **S2** | Delete the 40 dead shims. | 4,194 green |
| **S3** | Repoint the 97 test-only shims (`tradex_trading.X` → real module) across `trading/tests/**`. **Mechanical.** | 4,194 green, 0 shims imported |
| **S4** | Repoint the 15 script/benchmark shims. | 4,194 green |
| **S5** | Move the 90 goldens to their final home with `git mv`. | parity green |
| **S6** | Relocate 283 test files beside their packages. **Mechanical; largest single task (53k LOC).** | 4,194 green; per-package counts match |
| **S7** | Delete `position_manager.py`, `_PRODUCT_BY_WIRE`, `market_data/universe.py`, `runtime/calendar.py`. | 4,194 green |
| **S8** | Delete junk trees; gitignore `$JCODE_SCRATCH_DIR/`. | — |

### F.3 ARCHITECTURAL REDESIGN — alters ownership or boundaries

| # | Change | Gate |
|---|---|---|
| **A1** | Extract `TradingSession` + `live_fill_bridge` into `tradex_runtime`. | 4,194 green |
| **A2** | **Delete `trading/` src entirely.** Scripts already handled at S4. | 4,194 green · 12 packages |
| **A3** | Remove `sync` from `_run_pipeline`; `submit()` shapes the outcome. | 4,194 green + money-path probes re-run |
| **A4** | One strategy catalog; `chart.py` imports it and drops `importlib`. | 4,194 green + backtest e2e |
| **A5** | One `bar_key()` in `domain`; drop the 3 private reads. | 4,194 green |
| **A6** | Narrow `interfaces` to `domain` + `runtime`; move `ReplayRun` to `replay`. | 4,194 green |
| **A7** | Delete `boot_context` / `RuntimeContext` (folded into `TradingSession.stop()`). | 4,194 green |
| **A8** | Rewrite `test_import_boundaries.py` to the 5 target rules; delete the cycle detector (the DAG already holds). | New guard green |

### F.4 FINAL HARDENING

| # | Change | Gate |
|---|---|---|
| **F1** | Root `[tool.ruff]`; clear `ruff check`. | **exit 0** |
| **F2** | Clear 475 mypy errors; remove the 7 `continue-on-error` steps. | **mypy clean** |
| **F3** | `parity.yml`: install all 12 packages. | parity green in CI |
| **F4** | Own the tests: `execution`, `interfaces`, `market_data`, `analytics`, `strategy`, `replay`, `runtime` get real suites. | no 5-line smokes remain |
| **F5** | Rewrite the `CLAUDE.md` architecture section + 1 doc; delete 5. Fix `session.market` (does not exist), the universe import path, the `boot()` signature, and `tradex serve`. | docs match code |
| **F6** | Rebuild `openalgo-charts` → `frontend`; `verify-artifact`. | **artifact green** |
| **F7** | Playwright E2E. | **E2E green** |

### F.5 Hard gate chain

```
M1 committed
  → M2 entry point
    → suite green (4,194/0/3)
      → M3 shutdown → M5 FillSource contract
        → S1 dead packages → S2 dead shims
          → S3 test repoint → S4 scripts → S5 goldens → S6 test relocate
            → A1 extract TradingSession
              → A2 delete trading/
                → A3 pipeline signature → A4/A5 ownership → A6 interfaces narrow → A7/A8 guards
                  → F1/F2 lint + types → F6/F7 UI
                    → deploy
```

**A phase may not start while its predecessor's gate is red.** In particular **S3 must precede S6**, and **A2 must not precede M2**.

---

## G. First Implementation Step

**M1 + M2 only. Nothing else.**

- **M1 — commit the working tree** (11 untracked packages, 236 modifications, 90 golden relocations via `git mv`), recording the verified baseline `4,194 passed / 7 failed / 3 skipped` in the commit message.
- **M2 — add `[project.scripts] tradex = "tradex_interfaces.cli:main"` to `interfaces/pyproject.toml`** and confirm `tradex --help` resolves.

**Why this and nothing else:**

1. Every other finding is currently **unreproducible from git** — CI validates a 3-package world that no longer exists. No gate can be trusted until the tree is committed.
2. **M2 is the one change that makes the eventual `trading/` deletion safe.** One line, and it closes the only path by which this plan could break the deployment command.
3. M1+M2 have **zero** behavioural risk: no runtime logic touched, no test expectation changed.
4. Starting with M3/M4/M5 instead would mean making behavioural changes to a shutdown path and a money-path contract **while the tree cannot be reproduced or diffed against a baseline**.

**Do not touch the engine, the shims, or the tests until `4,194/7/3` reproduces from a clean checkout.**

---

## H. Risks

1. **M1 is a ~400-file commit with no CI signal.** Review it as a *move*: `git diff --stat` must show the 90 "deletions" as relocations, with no unintended content changes. The local suite is the only evidence.
2. **Entry-point breakage is the highest-severity regression risk** (correction #1). Mitigated only by M2 preceding A2. Add a CI step that runs `tradex --help`.
3. **S6 (53,138 LOC relocation) is large and mechanical** — high volume, low risk, but a missed path string or `conftest.py` dependency surfaces as import errors. Do it package-by-package with a green gate each time; never in one commit.
4. **A3 touches the money path.** The `_run_pipeline` signature change is logic-preserving *in intent*; gate it on re-running the 6 money-path probes (§A.5) plus the relocated execution tests. If any doubt, ship the `sync` removal as a separate PR with no behaviour change.
5. **`stream.py` (1,512 L, one 1,464-line function) is the least-understood module** and A6 touches it. Budget disproportionate time; consider deferring A6 past the deploy.
6. **Live-broker paths remain unexercised** — Dhan/Upstox auth, 401 retry, WS reconnect, and the documented 15:14 Dhan intraday tail have code comments but no proving test. A3/A6 must not reach them.
7. **The 3,283-vs-738 ruff discrepancy** means local lint counts are not portable. Rely on the exit code, not the count.

---

## I. GO / NO-GO

### GO — for M1 + M2 only.

**The repository is ready to begin the plan, but only at its first gate.** This differs from the first-pass review's NO-GO, and the difference is deliberate:

- **The prior NO-GO was correct for _deployment_.** N1 (`tradex serve` has no shutdown), N2 (`engine.shutdown()` never called → kill switch never set, SQLite connections leaked), a red suite, a red lint gate, 475 type errors, and a stale UI bundle are each independently disqualifying. **That NO-GO stands.**
- **GO for _implementation_** because the path is now known, cheap, and low-risk. The decisive facts, established by execution rather than inspection:
  - The money path is **correct** — idempotency, mismatch detection, cash, kill switch, FSM, and duplicate-fill dedup all verified at runtime.
  - The package graph is an **acyclic DAG**; the architecture guard is **green** (23 tests).
  - The extraction is **~97% complete** — exactly **one** shim is used by production source.
  - The dominant cost is **relocating tests**, which carries near-zero behavioural risk.

**Do not begin S1, A3, or any engine change until M1 and M2 are committed and the `4,194/7/3` baseline reproduces from a clean checkout.**

**M3 (server shutdown) should ship as its own reviewed change ahead of the structural work** — it is a live defect on the deployment path, and it is purely additive.

---

## Appendix — shim inventory (`trading/src/tradex_trading`, 154 shims / 1,673 LOC)

| Category | Count | Note |
|---|---|---|
| Dead (0 references anywhere) | **40** | Safe to delete now (S2) |
| Declared entry point | **1** | `interface.cli` — `tradex` console script. **Protected by M2.** |
| Test-only | **97** | Repointed at S3 |
| Script / benchmark-only | **15** | Repointed at S4 |
| Used by production source | **1** | `runtime.startup` ← `sdk/session.py`. Dissolves at A1. |

Also of note: `tradex_trading.interface.cli` is the *only* entry point declared anywhere in the repository; `interfaces/pyproject.toml` declares none.
