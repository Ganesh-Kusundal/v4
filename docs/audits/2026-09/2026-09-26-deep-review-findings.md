# Deep Review Addendum & Corrected Plan

> **Status note added 2026-09-28 (lane 4f).** Two findings below have since been acted on and are
> no longer true; both were correct when measured. **(1)** The "`python -m tradex_trading.interface.cli`
> is a silent no-op" finding (§4) — the whole `tradex_trading` package was removed by `b941da2`, and
> the real entry point is now `interfaces/pyproject.toml:38` → `tradex = "tradex_interfaces.cli:main"`,
> with a working `__main__` guard at `interfaces/src/tradex_interfaces/cli.py:619`. **(2)** The
> "`config/tests` (7) and `services/duckdb-analytics/tests` (54) are absent from `testpaths`, so 61
> tests never run" finding (§4) — both are now listed in `pyproject.toml:26-33` with a comment
> recording the omission, and collection now stands at **5470 tests**. The remaining CI findings
> (no `py.typed`, no root `[tool.mypy]`, coverage without `--cov-fail-under`) were not re-verified
> as fixed. The vacuous-guard findings in §5 refer to `tests/test_import_boundaries.py`, which has
> since been substantially rewritten — the `tradex_trading` subtraction trick it describes is gone,
> though 2 of its 23 tests currently fail in the working tree against *new, untracked* code from
> other lanes (`reconciliation_monitor.py` imports `tradex_interfaces`), which is concurrent work,
> not a documentation issue.

**Date:** 2026-09-26 · **Repo:** `v4` @ `df377a0` · **Method:** 6 parallel read-only agents (superpowers `dispatching-parallel-agents`), one per independent domain: money path · composition root/shutdown · test suite · deletion boundaries · interfaces/WS · CI/packaging. 13+ probe scripts executed in `/tmp`. **No repo file was modified by any agent** (`git status` clean throughout).

**This document supersedes §6 (DELETE) and §F (Plan) of `trading-predeploy-plan.md`.** A banner has been added there.

---

## 0. Headline: three of my own prior conclusions were wrong, and two would have caused damage

| Prior claim | Independently measured | Consequence if acted on |
|---|---|---|
| "40 dead shims, safe to delete" | **17** dead. **18 of 32 leaf shims have live test importers** | S2 would have broken 18 test files |
| "`_PRODUCT_BY_WIRE` — 1 occurrence tree-wide, redundant with `from_wire`" | **ALIVE.** 3 references; a test asserts its **exact contents**; deliberately *not* equivalent to `from_wire` (4 keys, no `None`, no enum names) | S7 would have broken 13 tests |
| "`application` is DEAD (1 caller)" | **ALIVE.** Live production caller + a live 422 capability gate on `POST /orders/bracket` | Deleting it **breaks the server at boot** |
| "`position_manager.py` — 2 importers" | **4** production importers (`__init__.py:20`, `engine.py:67`, `recovery.py:327`, `replay/backtest.py:24`) | Unrepointed deletion breaks backtest |
| "214 test files contain `is not None`" | Does not reproduce. 484 occurrences / 143 files; only **16** strictly trivial | Overstated weak-test claim |
| "ruff: parent config made it worse" | **Inverted.** Parent config is *looser* (`ll=100` vs `88`) → *fewer* errors. Real CI count is **766**. And 4 packages have `[tool.ruff]`, not 1 | Wrong fix target |
| "`mypy`: 475 errors of debt" | **465 of 479 are `py.typed` artifacts.** Real debt = **14** errors. No root `[tool.mypy]` exists, so all 17 per-package configs are inert in CI | 33× overstated debt |
| "`boot_context`/coordinator is dead code" | **Stranded-correct** — finished, priority-ordered, 3 properties tested, exported public API, reached by 5 test files and 0 production | Confirmed: wire up, don't delete |

**Two findings the shallow review missed entirely and that outrank everything in the old plan:**

1. **Security: 26 of 42 mutating HTTP operations have no auth.** Only 4 have the full stack. Unauthenticated: `/positions`, `/holdings`, `/account`, `/api/charts/book`, `/api/charts/account` (balance, margin, P&L) — and **unauthenticated `PUT`/`DELETE` on `/api/charts/workspace/{id}`**, a persistent write to `runtime/workspace.sqlite`. The WebSocket is the *only* surface with real auth **and** the only mutating surface without it — the exact inverse of where the risk is.
2. **Money path: three Critical defects.** The earlier "money path is sound" verdict verified *arithmetic*, not *sequencing*. A committed fill can be **silently destroyed**; a live order can be **placed twice under one idempotency key**; a fee can be **permanently lost** with the live and recovered books permanently disagreeing.

---

## 1. CRITICAL — money path (`execution/`)

The earlier probe confirmed position math, cash arithmetic, fees, and the Order FSM. Those remain correct. Sequencing around them is not.

| # | Sev | Site | Defect | Proof |
|---|---|---|---|---|
| **M-1** | **Critical** | `mark_to_market.py:85→111` | `on_quote` does an unguarded read-modify-write on the position book. `ThreadSafeReactiveBus` cannot save it — the imperative `engine.submit()` door is not bus-mediated. `startup.py:415` wires `MarkToMarketService`, which reimplements the mark math **without the instrument lock** that `PositionAccountant` takes. The docstring claim "exactly one place mutates position state" is false in production. | 5 committed shares **vanished**; book showed 5 where 10 were booked |
| **M-2** | **Critical** | `engine.py:596` + `fill_sources.py:26-36` | `submission_boundary_crossed` is undeclared on the `FillSource` Protocol and defaults `False`. A live source that crosses the venue boundary and raises a non-typed error is filed as a **definitive rejection**: cid released, no kill switch, client retry places a **second live order**. `OrderSubmissionUnknownError` is real defence but is unrequired broker-layer policy. | 2 live orders at the venue under 1 key; OMS shows 2 REJECTED; cash/position never move |
| **M-3** | **Critical** | `engine.py:651` vs `669-699` | `record_result` completes the idempotency key **before** any money moves; the 5-mutation fill block has **no rollback**. A mid-fill exception completes the cid, so the failure is unretryable, and the same log rebuilds a **different** book. | `OrderFilled` durable, position booked, **fee lost**, cid completed; replay yields `cash -0.42 / fees 0` |
| M-4 | High | `engine.py:543, 568` | Both `_no_receipt` returns leak the reservation (no `try/finally`). | `reserved={'cid-A'}`; retry → `PENDING idempotency_in_flight` forever; with `SQLiteIdempotencyGuard` the row **survives restart** |
| M-5 | High | `engine.py:679-687` | Fill-not-durable returns a **`FILLED` receipt** while the OMS holds `FILLED / filled_quantity=0`, no position, no cash, and — under the default `require_durable_events=False` — **no kill switch** | verified |
| M-6 | High | `engine.py` 10 of 14 `_append` sites | Return value **ignored** at 10 sites; fail-**open** under the default flag. `require_durable_events=True` is a **no-op in live** without `cfg.persistence.path` (`startup.py:264-270`, `_append` returns `True` when store is `None`). | probe: `OrderPlaced`/`BrokerOrderAcknowledged` missing, fill still applied |
| M-7 | High | `sqlite_store.py:147` + `:566/584` | `SQLiteOrderStore` omits `check_same_thread=False` (unlike the other two stores). The resulting `ProgrammingError` is **swallowed**, so the `orders` table silently stops being written from the feed thread while `attach_order_persistence` reports success. | 0 rows written, no exception propagated |
| M-8 | High | `idempotency.py:150-157` vs `recovery.py:185-212` | In-memory `FillDedup` collapses two genuine equal-lot/equal-price **partials**; recovery's `_is_duplicate` correctly counts both. **Live and recovered books disagree for the same log.** | live drops 2nd partial; recovered `filled=100/100` |
| M-9 | High | `order_manager.py:38-45` | Overfill: the **full** fill quantity goes to position and cash; only the OMS clamps. `replace()` bypasses the Order FSM, so the illegal-transition guard never runs for fills. | 2 distinct fills → OMS `10/10`, position 20 |
| M-10 | Med | `cash_ledger.py:72` | A short credits **full notional** to cash, inflating the number the risk cash gate compares against. | SELL 10@100 from 1000 → `cash 2000` |
| M-11 | Med | `engine.py:754` | `restore_cash` hard-codes `allow_negative=True`, silently discarding the constructor arg and disabling the buying-power floor on the production boot path. | probe |
| M-12 | Med | `engine.py:978, 503` | `submit()` annotated `-> OrderReceipt` but returns `OrderId` on replay. `_no_receipt` invents a UUID for an order never created/published/durable. | `AttributeError: 'OrderId' has no attribute 'status'` |
| M-13 | Med | `idempotency.py` / `engine.py:233` | `MemoryIdempotencyGuard` holds an `RLock`, SQLite a `Lock` — so `_guard_lock` protects nothing, and is applied to 4 of 10 sites. Three unbounded dicts (`_cid_for_order`, `_brokerage_accrued`, `guard._completed`): 2000 entries after 2000 orders. | probe |

**Verified correct — do not re-litigate:** position math (weighted avg, realized P&L, long↔short flip, short cover); cash ledger arithmetic; all six fee components incl. GST-on-capped-brokerage and sell-only STT; `_record_applied_fill` before `publish` (correct re-publish suppression); fill-after-cancel recovery; `cancel()` venue-first ordering; `modify()` re-risks before dispatch; `_request_fingerprint` canonical-Decimal coverage; reservation-before-mutation *ordering* for durability-critical writes; `SQLiteIdempotencyGuard` PK-insert race closure; `SQLiteEventStore.replay` fail-on-unreadable; `PositionManager is PositionAccountant` (no duplication).

---

## 2. CRITICAL — shutdown: the SIGTERM bypass

**Correction to my prior claim:** `cli.py` **does** have teardown — `cli.py:417-419` and `cli.py:300-301` both call `session.stop()` in `finally`. Verified by instrumenting the real `cli.main(["serve", ...])`: SIGINT → `stop()` entered and exited, `state=STOPPED`, rc=0.

**But teardown is bypassed on SIGTERM, which is what `docker stop`, Kubernetes, systemd and plain `kill` all send.** `uvicorn.run()` never returns: its `capture_signals` re-raises the captured signal *inside* the `with` block, and for SIGTERM the restored disposition is `SIG_DFL`, so the kernel terminates the process before control returns to any `finally`.

| signal | exit | `session.stop()` | `atexit` |
|---|---|---|---|
| SIGINT | 0 | ran | ran |
| **SIGTERM** | **143** | **never entered** | **never ran** |

Additional confirmed findings:
- **`workers>1` / `reload` in non-live mode is permitted** (`fastapi_app.py:494` gates only `mode=="live"`) and each worker's session, built in `serve_app()`, is never stopped. *Verified: `tradex serve --broker PAPER --workers 4` leaks 4 sessions.*
- `engine.shutdown()` closes **1 of 3** SQLite connections. `SQLiteEventStore.close()` and `SQLiteOrderStore.close()` have **zero production callers repo-wide**.
- `_safe_teardown` (`startup.py:315-320`) leaks **all three** connections on a failed live boot and is passed `session=None` in both branches, so any failure after `startup.py:685` discards the session.
- `writer_lock.py:71-93` — `acquire()`'s `O_EXCL` create path **never takes the `flock`** (only `_takeover` does), so an empty lockfile is taken over from a **live** holder.
- `session.stop()` disposes the bus (`:202`) before setting `STOPPED` (`:233`), so mid-teardown the session reports `READY` over a dead bus; admission is blocked only incidentally, by a raw `RuntimeError` from the reactive layer.

**Verdict on the 150 LOC:** `ShutdownCoordinator` is **stranded-correct**, not dead. It is the only path that closes the idempotency guard, the only one that sets the kill switch, it is exported public API (`runtime/__init__.py:25,30`), and its guard test `test_runtime_context_uses_coordinator` **calls `session.stop()` instead** — it protects nothing. Fix = wire it into an ASGI lifespan `shutdown` handler (which also fixes the workers>1 child leak for free), not delete it.

---

## 3. HIGH — goldens: a stale fixture is shipping to consumers

| | |
|---|---|
| Trees | `analytics/src/tradex_analytics/goldens/` (**90**, ships in the wheel) vs `trading/tests/analytics/goldens/` (**145** incl. `settings2/`, `transforms/`, `profiles/`, `fixtures.json`, `PROVENANCE.json`) |
| Who reads it | **Nothing.** All 6 consumers resolve `Path(__file__).parent / "goldens"`. The `src/` copy has no `__init__.py`, so it is not importable. |
| Who writes it | **Nothing.** All generators (`generate_goldens*.mjs`, `write-provenance.mjs`, `goldens-provenance.mjs:25`) target `trading/tests/analytics/goldens/` only. |
| Divergence | **30 of 90 differ.** 8 differ in `settings`; 20 lack plots. The direction is decisive: `src/` has `sma.length: 20`, `stochastic.kSmoothing: 3`; the real set has `9` and `1` — **exactly the 2.1.8 bump CLAUDE.md documents.** `PROVENANCE.json` records the real set as `2.3.2` / `be7418e9…`, matching the pin. |
| Consequence | The wheel carries 90 expected-values files that are a **pre-bump snapshot, read by zero tests, and can never be regenerated**. This is precisely the failure CLAUDE.md warns about ("fixtures captured from a version nobody runs"). It is not covered by the provenance hash, which is why it rotted unnoticed. |

Two structural blindnesses in the parity gate itself:
- **`test_golden_parity.py:373-374` iterates the *backend's* output keys**, so a plot the backend stops emitting is never asserted. `supertrend.bodyMid` is in the fixture, absent from `analytics/src/`, and the test is **green**.
- **`PARAM_MAP` is empty for 11 of 102 indicators** (`atr, supertrend, twap, awesome-oscillator, balance-of-power, chop-zone, adl, volume, pvt, klinger-oscillator, net-volume`), so those run on **backend defaults** and the golden's recorded settings are discarded. CLAUDE.md's claim that "the gate takes its parameters from each golden's own recorded settings" is **false for 11% of the suite**.

Also: the 3rd skipped test is a **bug, not a gap** — `_golden_ids()` globs `PROVENANCE.json` as an indicator id. Registry 102, goldens 103, `goldens − registry = {PROVENANCE}`. **All 102 real indicators are covered; zero real parity skips.**

---

## 4. HIGH — CI has never run a test

| workflow | step | verdict |
|---|---|---|
| quality-gate | `uv sync --frozen` | **PASS** — `uv lock --check` exit 0; 17 editable entries match 17 members |
| quality-gate | **`ruff check`** | **FAIL — 766 errors, exit 1. The job halts here.** |
| quality-gate | 9 × mypy | **NEVER RUNS** |
| quality-gate | **4,204-test pytest** | **NEVER RUNS** |
| quality-gate | *(if reached)* `mypy brokers` | would **FAIL** — 154 errors, and it is a **hard** gate with no `continue-on-error` |
| parity-gate | `pip install -e ./trading` | **FAIL** — `No matching distribution found for tradex-analytics` (plain pip has no uv-workspace concept) |
| frontend-gate | `npm run e2e` | PASS structurally, but the staleness check is a **tautology**: `e2e` = `build && test`, and `build` writes the stamp seconds before `globalSetup` compares it. `verify-artifact` appears in **no** workflow. `check()` also never compares `library.pin`, which is recorded but unenforced. |

**No test has ever executed in CI.** Additional:
- **No root `[tool.mypy]`** → running mypy from the root resolves *no config*, so all 17 per-package blocks (incl. `ignore_missing_imports`) are inert. **465 of 479 errors are `py.typed` artifacts**; adding a root config collapses the count to **~14**. No package ships `py.typed`.
- 4 packages have `[tool.ruff]` (domain, brokers, research, trading), not 1. 207 of 834 files fall through to ruff **defaults** (413 rules, `ll=88`) — 543 of the 766 errors. `research` has `[tool.ruff]` with **no `[tool.ruff.lint]`**, so it alone gets 413 rules.
- **`61 tests never run**: `config/tests` (7, incl. a documented split-brain guard) and `services/duckdb-analytics/tests` (54) are absent from `testpaths`. **Both suites are green** — this is free coverage.
- Coverage names **9 of 17** packages, with **no `--cov-fail-under`**.
- `python -m tradex_trading.interface.cli` (documented at `CLAUDE.md:194` + 3 runbooks) is a **silent no-op** — no `__main__` guard, exits 0 doing nothing.

---

## 5. HIGH — architecture guards are string tests, and one is vacuous

All **38** assertions across the 916-line guard suite are string/AST tests. **Not one executes the code it governs.**

- **`test_trading_only_imports_allowed_internals` (`:183-190`) can never fail.** `_ALLOWED["trading"] = set(_INTERNAL) - {"tradex_trading"}` → `forbidden == {"tradex_trading"}`, then line 188 subtracts the same set. It presents as the guard over 159 shims and asserts nothing.
- **`test_known_import_cycles_all_still_exist` (`:760`) can never fail** — `KNOWN_CYCLES = {}`.
- Verified **misses** (ran the guards' own helpers on synthetic input): `importlib.import_module` / `__import__`; `AugAssign`; `NamedExpr`; `Delete`; `setattr(obj,"_x")`; `obj.__dict__["_x"]`; **9 of 9** cross-package private *reads* (the rule only covers writes in `Store` context); alias duplication (`_top_level_names` collects only `ClassDef`/`FunctionDef`, so `PositionManager = PositionAccountant` is invisible).
- The `_SINGLE_AUTHORITY_SYMBOLS` accommodation (`:352-357`) has a **factually wrong comment**: it claims `position_manager.py` is skipped as a `"Compatibility shim"`; it is not — the real cause is `_top_level_names` ignoring `Assign`.
- Single-authority scan covers **43 of 245** src modules.
- `TYPE_CHECKING` is legal to 8 guards and illegal to 5 — same input, opposite verdicts.

**Test-quality triage (4,204 nodes):** 82.7% behavioural · 8.0% raises-only · 2.2% call-only/no-assert · 2.1% tautology · 0.8% mock-call-only · 0.6% static guards. AsyncMock used **0** times despite a large async WS/replay surface.

**Repoint mapping (`trading/tests`, 129 distinct shimmed paths):** **121 mechanical** (verified symbol-for-symbol) · **3 need a source move first** (`sdk.session`, `sdk.live_fill_bridge`, `sdk.streaming`) · **2 are negative-assertion tripwires to leave alone** · **1 monkeypatch target should skip a hop to `tradex_domain.universe`** · **1 silently corrupts**: `test_datalake_root.py:29` `parents[4]` is correct for shim depth and wrong for target depth, and the resulting scan **passes silently**.

**Relocation obstacles:** 8 `trading/tests/scripts/` files reach `parents[2]/"scripts"` (no package has a `scripts/` dir after relocation; 3 `exec_module` at import scope) · 12 `parents[N]` sites calibrated to 3-level paths · 147 non-`.py` data files ride along, and 2 tests reach **across** test subdirs into `trading/tests/analytics/goldens` · 7 string-coupled mock targets · 11 chart tests **silently skip in CI** (no datalake) so the chart contract is untested there.

---

## 6. CORRECTED deletion manifest

### DELETE — verified dead, 0 references in all 8 reachability channels

| Target | Count | Notes |
|---|---|---|
| Shim modules | **17** (not 40) | machine-checkable SHA-256 manifest exists at `/tmp/audit-dc/DELETION_MANIFEST.json` |
| `analytics/src/tradex_analytics/goldens/` | **90** | orphaned, diverged, ships in wheel, 0 readers, 0 writers |
| `persistence/` package | 3 files | 0 production importers; all 6 exports resolve to `tradex_execution.*` |

### DELETE — requires config edits first (4 files each)

`persistence` requires editing root `pyproject.toml` (`members`, `[tool.uv.sources]`, `testpaths`, `pythonpath`, dev group) **and** `tests/test_import_boundaries.py` `_SRC`/`_INTERNAL` + `self_name` — otherwise `test_all_workspace_packages_present:222` hard-fails.

### DE-DUP — keep code and one test copy

`research/tests` and `trading/tests/research/` are **44 nodes / 519 LOC duplicated**, differing only in the import path. `research` is production-dead but has 44 real tests. Note `research/pyproject.toml` declares `dependencies = []` while `pit.py` imports pandas — a clean install cannot import it. **De-dup, do not delete.** Same for `operations` (0 importers, but it is the only executable guarantee that 7 runbooks exist).

### DO NOT DELETE — corrected

| Item | Live reference |
|---|---|
| **`strategy/.../extensions/shared/`** (the real one) | `extensions/__init__.py:20` — deleting it breaks **all** strategy + scanner discovery |
| `application/` | `interfaces/routes/orders.py:18`; `interfaces/pyproject.toml:15`; **the server will not start** |
| `_PRODUCT_BY_WIRE` | `test_order_product.py:49,126,132` — 13 tests |
| 18 "dead" leaf shims | live test importers (csrf, rate_limit, session_store, market_provider, envelopes, recovery, sqlite_event_store, research×2, feed×2, strategy×4, application) |
| `position_manager.py` | **4** production importers |
| `runtime/streaming.py` | **4** production importers — it is the **canonical** copy |
| `_attach_event_store` | 3 real tests assert durable OMS event coverage |
| `market_data/universe.py`, `runtime/calendar.py` | 2–4 importers each |

---

## 7. Corrected plan — reordered

The old order (M3 shutdown → M4 tests → M5 FillSource → S1 deletions) is **wrong**. Security and money-path integrity outrank structural cleanup.

### TRACK 0 — Already done
`f0ebfed` (runtime/ tracked; secrets excluded) · `df377a0` (`tradex` entry point on `interfaces`)

### TRACK 1 — BLOCKERS (do before any cleanup)
| # | Change | Gate |
|---|---|---|
| **T1.1** | **Close the auth holes.** 26 mutating ops unauthenticated; unauth `PUT`/`DELETE /api/charts/workspace/{id}`; unauth `/positions`, `/holdings`, `/account`, `/api/charts/book`, `/api/charts/account`. Decide the auth policy per surface, then apply `require_auth` (+ CSRF where session-cookie auth is used). | every mutating op authenticated; new negative test per route |
| **T1.2** | **M-1 mark-to-market fill destruction.** Route all marking through `PositionAccountant` (take the instrument lock) or give `MarkToMarketService` the same lock; delete the duplicated mark math at `mark_to_market.py:96-111`. | agent's lost-update probe now passes under concurrency |
| **T1.3** | **M-2 idempotency-key double-order.** Declare `position_projection_owned` + `submission_boundary_crossed` on the `FillSource` Protocol with explicit `bool`; make the engine's read **fail toward unknown** for a source that does not declare itself non-live. | agent's 2-live-orders probe impossible |
| **T1.4** | **M-3 cid-completed-before-money.** Move `record_result` after the fill block succeeds; make the 5-mutation block compensatable. | agent's torn-write probe: live book == recovered book |
| **T1.5** | **M-7 swallowed `ProgrammingError`.** Add `check_same_thread=False` + a lock to `SQLiteOrderStore` (as the other two stores have); stop swallowing. | cross-thread write lands; exception propagates |
| **T1.6** | **SIGTERM teardown bypass.** Register the teardown as an ASGI lifespan `shutdown` handler in `create_app`; adopt `ShutdownCoordinator`; give the engine ownership of `event_store` + `order_store` so all 3 stores close; fix `_safe_teardown`. | SIGTERM test: exit 143 **with** teardown observed |
| **T1.7** | **M-4/M-5/M-6.** Release the reservation on both `_no_receipt` paths (try/finally); return a non-FILLED receipt when the fill is not durable; trip the kill switch on a failed fill write regardless of the flag. | agent's probes pass; `guard._reserved == set()` asserted |
| **T1.8** | **`BacktestEngine.run` is synchronous, called inside `async def`** (`chart.py:785`) — blocks the event loop that `ws_stream` shares. `asyncio.to_thread` it. | WS responsiveness under a backtest |

### TRACK 2 — Make the gate real (nothing below is verifiable until this is green)
| # | Change |
|---|---|
| **T2.1** | `parity.yml`: `pip install uv` + `uv sync --frozen` + `uv run pytest` (the job is currently dead at install) |
| **T2.2** | Root `[tool.mypy]` + `py.typed` in all 17 packages → collapses 479 → ~14; un-hides the `brokers` hard gate |
| **T2.3** | Root `[tool.ruff]`; `extend-exclude` `poc/`, `apps/`, `benchmarks/`; fix or baseline the remainder. **Target: exit 0.** |
| **T2.4** | Add `config/tests` + `services/duckdb-analytics/tests` to `testpaths` (61 dark tests, both green) and add duckdb-analytics as a workspace member |
| **T2.5** | Coverage: add the 8 missing `--cov=` + a floor. Delete the `trading` `[project.scripts]` (ends the console-script collision; fix the "as well"→"instead" comment) |
| **T2.6** | `verify-artifact` as a real CI step after Build; add a `library.pin` comparison to `check()` |

### TRACK 3 — Verified-safe deletions (corrected list, §6)
Goldens (90) → 17 shims → `persistence` (+4 config edits) → `research`/`trading/tests/research` de-dup. **Gate after each: full suite + 23 guards green.**

### TRACK 4 — Goldens gate hardening
Make `_compare` bidirectional (assert every golden plot is present in backend output — catches `bodyMid`) · populate `PARAM_MAP` for the 11 empty entries or assert recorded settings == backend defaults · exclude `PROVENANCE` from `_golden_ids()`.

### TRACK 5 — Structural (repoint → relocate → extract)
S3 repoint (121 mechanical; **fix `test_datalake_root.py:29` first**) → S6 relocate (resolve the 8 `scripts/` files and 12 `parents[N]` sites first; give the 11 chart tests a seeded lake) → A1 extract `TradingSession` + `live_fill_bridge` → A2 delete `trading/` src.

### TRACK 6 — Guards, docs, UI
Delete the 2 vacuous guards · fix the `_SINGLE_AUTHORITY_SYMBOLS` comment and extend `_top_level_names` to `Assign` · extend the private-access guard to reads, `AugAssign`, `NamedExpr`, `Delete`, `setattr`, `__dict__` · single strategy catalog · single `bar_key` · narrow `interfaces` fan-out · rewrite `CLAUDE.md` (`session.market` does not exist; universe path; `boot()` signature; the `python -m` command is a no-op) · delete 5 of 6 architecture docs.

---

## 8. What I got wrong, stated plainly

1. **I proposed deletions that would have broken working code.** "40 dead shims" was 17. `_PRODUCT_BY_WIRE` and the `application` package are both alive. Deleting `application` stops the server booting. Two agents independently found my list wrong; I should have run the 8-channel reachability audit *before* publishing a deletion manifest, not after.
2. **I published LOC figures from a `find` that swept in test files** (24,672 for `trading/`; 337,160 repo-wide) and did not reconcile them against a per-directory count.
3. **I called the money path "sound" on 6 probes** that verified arithmetic and the happy path. Sequencing, concurrency, and durability were untested.
4. **I claimed `cli.py` has no teardown.** It has two `finally` blocks. The real defect is subtler and worse — uvicorn never returns under SIGTERM.
5. **I overstated weak-test evidence** ("214 files") and **understated real type debt is tiny** (14, not 475) while **overstating lint debt's cause** (config accident, not code).
6. **I missed a security finding** that a 6-agent team surfaced in one pass: 26 unauthenticated mutating endpoints, including a persistent write.

**The one thing that held up:** the plan to *finish* the extraction rather than roll it back or redesign it. The DAG is acyclic, the guard suite is green, `domain` is genuinely good, and the money path's core arithmetic is correct. That decision stands — but it is now gated behind Track 1, which the earlier plan did not know existed.
