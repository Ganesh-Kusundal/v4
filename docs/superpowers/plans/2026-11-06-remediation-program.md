# Remediation Program Implementation Plan (Waves 0–1)

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` to execute, one fresh subagent per task, **with the waves run in parallel git worktrees** (one per workstream). Steps use checkbox (`- [ ]`) syntax for tracking. Waves marked ⟂ are mutually parallel; ➜ is serialized.

**Goal:** close every finding in the two audit reports with parallel agent teams, without forking live/replay logic or breaking the parity gates.

**Architecture:** Fan-out by *file ownership*, not by theme. The execution spine is edited by exactly one agent (Track M) because `engine.py`, `cash_ledger.py` and `idempotency.py` cannot be safely co-edited; everything else is partitioned by directory so ~24 tasks can run concurrently across worktrees. Wave 2 (the architecture epic) is deliberately **not** in this plan — it reshapes the same files Wave 1 hardens and needs user decisions first (§Decisions).

**Tech Stack:** Python 3.12, uv workspace, `uv run --no-sync` for every tool; pytest; ruff/mypy via ratchets; import-linter + `tests/test_guardrails.py`.

**Spec:** `docs/audit/2026-11-06-principal-architecture-review.md` (system-wide; phases 1–5, §3 targets, §4 divergences, §7 API contract, §8 tests) and `docs/audit/2026-11-06-money-path-review.md` (MP-1…MP-6, remediation table §Remediation). Findings are cited by ID (A1…A15, B1…B13, C1…C9, D1…D6, MP-1…MP-6).

## Global Constraints

- Real-money posture: correctness > cleverness; prefer deletion over refactoring; determinism over convenience.
- `Decimal` for all money; frozen dataclasses; no wall clock outside infra/boot; no new `dict` bodies or returns on the wire; one path to the venue (`ExecutionEngine.submit()`); bus never carries orders; sells are never margin-gated (see Decision D-1).
- Parity gates and guard tests must stay green: `uv run --no-sync python -m pytest trading/tests/parity trading/tests/contracts tests/ -q`.
- Guardrail ratchets only shrink: do not raise `.ruff_baseline`/`.mypy_baseline`.
- **Never `git add -A`.** The tree carries the owner's WIP (untracked `analytics/screening.py`, `sensitivity.py`, `retry_config.py`, `broker_limits.py`, `clocks.py`, `strategy/base.py`, `runtime/environments.py`, modified `events.py`/`stream.py`/`market_data.py`/user gateway edits). Two files are contested and marked ✎ below.
- Commit per task; conventional commits; add explicit paths only.
- Tests live with the code they cover: per-package suites are smoke/contract; `trading/tests/` is behavioural; `tests/` is architecture guard. New money-path tests go in `trading/tests/execution/` unless a contract suite is the natural home.

## Review Focus

The five failure modes most likely to bite, in order, with owning tasks:

1. **Venue re-delivers a fill after restart → double-applied position/cash/fees.** Owner: T-M1 (Task 5). Pin: kill-mid-record republish property test.
2. **Account holds zero idle cash → the risk layer refuses the strategy's exit fill.** Owner: T-M3 (Task 7). Pin: SELL books with `cash=0`, fee paid from proceeds; BUY path unchanged.
3. **A route keeps/re-gains a bare `dict` body after the sweep.** Owner: T-X2 (Task 11). Pin: new AST guard `test_no_dict_route_bodies` (bounded to `interfaces/routes/`).
4. **Position loses strategy attribution across mark/fee rebuilds → per-strategy PnL silently corrupts.** Owner: T-M2 (Task 8). Pin: fold property test asserting `strategy_id` stable across fill→fee→remark→mark.
5. **Guard suite regresses again on a feature commit (A1 class).** Owner: T-T1 (Task 24). Pin: `tests/test_guardrails.py` green in every wave's merge gate; CI runs it on every PR.

---

## Orchestration model & workstreams

Worktrees: `git worktree add` per track (skill `using-git-worktrees` at execution time). Each agent rebases onto latest `main` at wave boundaries only.

| Track | Owns (exclusive) | Runs | Notes |
|---|---|---|---|
| **M** money path | `execution/*`, `domain/position_math.py` only via M2, new tests under `trading/tests/execution/` | ➜ serial within track | single owner of `engine.py` |
| **P** parallel positions | `execution/position_accountant.py`, `execution/mark_to_market.py` | ⟂ with M | disjoint files |
| **X** contract/API | `interfaces/**` (except ✎ `routes/stream.py`, `routes/market_data.py`, `fastapi_app.py` may only be *read-appended* by X unless owner clears; prefer separate new module `interfaces/contracts.py`) | ⟂ | 0003 tranche 1 |
| **G** gateway & gates | `gateway/**`, `.github/workflows/`, `scripts/predeploy_gate.sh`, root `pyproject.toml` (coverage list only), `deploy/` | ⟂ | ✎ folds T2 |
| **D** docs | `docs/**`, `README.md`, `CLAUDE.md` | ⟂ | no code |
| **T** test infra | `tests/test_guardrails.py` (region), new `trading/tests/parity/`, `contracts/`, `docs/DIVERGENCE.md` | ⟂ | adds no product code |

Ownership collisions handled: `runtime/startup.py` is single-owner (Track M, additive only — Tasks 4/5); `interfaces/routes/stream.py` (✎ owner WIP) — Track X must not edit it; the `_replay_error` duplicate (B11) stays out of scope, Wave 2. `trading/tests/execution/` new files are partitioned by author, not by shared file.

**Waves:** Wave 0 (hygiene, all parallel) → Wave 1 (M/P/X/G/D/T parallel) → merge gate (full suite + parity + guard) → Wave 2 gate review with user (not executed here).

---

## File structure touched (map before tasks)

- Money path: `execution/src/tradex_execution/engine.py` (MP-1 wiring, MP-3 proof, MP-4 adopt), `cash_ledger.py` (MP-3), `idempotency.py` (MP-5), `recovery.py` (MP-6), `position_accountant.py`, `mark_to_market.py` (MP-2)
- Contracts: new `interfaces/src/tradex_interfaces/contracts.py` (DTOs), `models.py` (extend), new `interfaces/src/tradex_interfaces/errors.py` reuse, `routes/orders.py`, `routes/chart.py`, `fastapi_app.py` (CSRF/Origin/enforce flag wiring only at lines already marked)
- Gates: `.github/workflows/quality-gate.yml`, `scripts/predeploy_gate.sh`, root `pyproject.toml` (`--cov` list, `testpaths`), `tests/test_guardrails.py`
- Docs: `README.md`, `CLAUDE.md`, `docs/ARCHITECTURE.md` (counts/claims/sections), `docs/DIVERGENCE.md` (new)

---

## Tasks

### Task 1: WIP triage & commit hygiene

**Files:** none to edit. Output: `docs/superpowers/plans/2026-11-06-wip-triage.md` (untracked scratch, listed in `.git/info/exclude` or left untracked).

- [ ] Step 1: `git status --porcelain` → bucket every path into (a) owner WIP (screening/sensitivity/retry_config/broker_limits/tests for them), (b) remediation targets, (c) audit docs (mine, safe), (d) contested (✎).
- [ ] Step 2: confirm `uv run --no-sync python -c "import tradex_interfaces, tradex_runtime, tradex_execution"` exits 0 on the owner WIP; if not, report and stop — agents fork from green commits only.
- [ ] Step 3: Done when the triage file lists every path with one bucket letter. No commit (owner-owned state).

### Task 2 (⟂ W0): Guard green — gateway `_tf_seconds` redefined ✎→G ✅ DONE UPSTREAM

**Status (2026-11-06):** fixed by `b739e892 fix(gateway): reuse the consolidated domain interval helper` (landed on `main` before this plan was executed; guard suite verified `13 passed` at HEAD). No action. Kept for audit completeness.

### Task 3 (⟂ W0): Delete `_backpressure_triggered` stub

**Files:** Modify `reactive/src/tradex_reactive/bus.py:41-45`; delete the dict + `# noqa: F401`; keep nothing referencing it (`grep -rn _backpressure_triggered` → 0 hits outside tests; if a test imports it, drop that import in the same commit and say so).

- [ ] Steps: remove → `pytest reactive/` green → commit `chore(reactive): drop obsolete backpressure stub`

### Task 4 (⟂ W0): Single secret-mode constant + refuse-at-boot wiring

**Files:** Modify `config/src/tradex_config/permissions.py` (keep `EXPECTED_SECRET_MODE`, the only definition), `brokers/src/tradex_brokers/common/token_lifecycle.py` (drop `SECRET_FILE_MODE`; inline import from config or delete repair-twin function — check callers at `token_lifecycle.py:190` first), Test: `tests/test_secret_boot_permissions.py`-style unit test (config-level; invokes the checker against a tmp tree).

**Interfaces:** Consumes `tradex_config.permissions.assert_secret_file_mode(path) -> None` (raises `InsecureSecretFileError`). Produces: `assert_runtime_secret_permissions(base) -> None` in `tradex_config.permissions` — iterates `runtime/{dhan,upstox}/token_state.json*`, `*.db`, `.env.local`; raises on group/world-readable. **Track H delivers the checker + unit tests only; Track M wires it into `startup.boot()` inside Task 5.** (File-ownership split keeps `runtime/startup.py` single-owner.)

- [ ] Step 1: write failing test: tmp dir mimicking `runtime/dhan/token_state.json` at 0o644 + a boot-called helper `assert_runtime_secret_permissions(base)` → raises `InsecureSecretFileError`.
- [ ] Step 2: run it → FAIL (function missing).
- [ ] Step 3: checker implemented, unit tests green (`pytest config/tests -q`); **boot wiring is done by Track M inside Task 5** (`runtime/startup.py` single-owner rule): after config validation, before broker construction, raise `InsecureSecretFileError` on failure. H does NOT touch `runtime/`.
- [ ] Step 4: rerun guard suite green.
- [ ] Step 5: Commit `fix(config): single refused secret-mode source + param runtime-secret perm checker`

### Task 5 (M1): MP-1 — durable fill dedup, one commit window ➜ M

**Files:** Modify `execution/src/tradex_execution/engine.py` (import `DurableFillDedup`/`fill_fingerprint` path), Test: new `trading/tests/execution/test_fill_dedup_durable.py`.

**Interfaces:** Consumes `DurableFillDedup(store, max_size=…)` with `.seen(fill)`/`.check_and_record(fill)`; store = `SQLiteEventStore` (implements `fill_seen`/`record_fill`, account-scoped). Produces: engine takes optional `fill_dedup` param override (already exists) — runtime boot additionally passes `DurableFillDedup(event_store)` when store present (edit `runtime/startup.py` one line, Task owned by M since boot wiring must match M's semantics — additive, additive-only).

- [ ] Step 1: failing test: two threads call engine `_apply_fill` with same `OrderFilled` (restart flow: fresh engine from same store) → exactly one `PositionUpdated`, one `Position`, cash debited once, fees once; venue re-publish after `engine.shutdown()` → still no second application.
- [ ] Step 2: run → FAIL (double apply today).
- [ ] Step 3: wiring only: in `ExecutionEngine.__init__`, if `event_store is not None and fill_dedup is None`: `self._fill_dedup = DurableFillDedup(event_store)`; keep plain LRU only when no store. Move the `seen()` check inside the `with scope:` block if scope is not None (it opens `self._cash_ledger.transaction()`); never commit fingerprint before durable append (keep current ordering: seen → scope(append → record)).
- [ ] Step 4: run new test → PASS; run `trading/tests/execution` + parity → green (fill dedup is hot path; expect no regressions).
- [ ] Step 5: wire `runtime/startup.py` boot (M owns this file for this plan): (a) after config validation, before broker construction, call the new `tradex_config.permissions.assert_runtime_secret_permissions(<runtime_dir>)`; (b) pass `fill_dedup=DurableFillDedup(event_store)` explicitly at engine construction. Both additive, one-liners; additive only — never restructure boot.
- [ ] Step 6: Commit `fix(execution): venue fill dedup survives restart (durable fill index wired)` + `fix(runtime): boot fails closed on permissive runtime secrets` (two commits)

### Track 6 (M2): MP-4 — venue-confirmed fill is booked and adopted/flagged, never dropped ➜ M

**Files:** Modify `execution/src/tradex_execution/engine.py` (`_apply_fill` failure branch, the `_require_cash_for_fill` catch), Test: new `trading/tests/execution/test_venue_fill_adoption.py`.

**Interfaces:** Consumes existing `ErrorOccurred`, `DriftItem`, `ReconciliationResult` events and `PositionAccountant`. Produces: when the affordability proof fails for a venue-confirmed fill, the engine must still book `PositionUpdated` + the fill in OMS/cache, emit a `DriftItem`/`ErrorOccurred` pair carrying `unaffordable_venue_fill=<order_id>`, increment a `bus.fill.unadoptable` metric, and never silently return. The bus's exception isolation must not swallow this path.

- [ ] Step 1: failing test: seed engine so cached fill is unaffordable (zero cash, BUY); simulate inbound `OrderFilled` from the "venue"; assert position/cash reflect the fill (venue is truth) AND an `ErrorOccurred`/drift marker was published; assert nothing raised into the bus loop.
- [ ] Step 2: run → FAIL (fill dropped today, no booking, swallowed raise).
- [ ] Step 3: implement the degrade-to-adopted path: still append `OrderFilled`, still apply `_apply_fill_effect` but with the BUY notional proof bypassed *for venue-confirmed fills with an explicit `reason="venue_confirmed"` marker on the event payload path if available* — if no marker exists in `OrderFilled`, the trigger is the `_apply_fill` door itself (it is definitionally venue-sourced); flag via metric + reconciliation item; keep local raise only as last resort after booking attempt.
- [ ] Step 4: rerun → PASS; `trading/tests/execution` + parity green.
- [ ] Step 5: Commit `fix(execution): venue-confirmed fills are booked and flagged, never refused locally (MP-4)`

### Task 7 (M3): MP-3 — SELL exits are never cash-gated ➜ M (DECIDED: net-proceeds proof)

**Files:** Modify `execution/src/tradex_execution/engine.py:1040-1044`, `cash_ledger.py` (`apply_fill` step-1 proof only), Test: `trading/tests/execution/test_sell_zero_idle_cash.py`.

**Interfaces:** Consumes existing `require_debit`. Produces: for `side is SELL`, proof is `notional - fee >= 0` (net proceeds), not `cash >= fee`; BUY unchanged.

- [ ] Step 1: failing test: `CashLedger(initial=0)`; `apply_fill(SELL, qty=1, price=100, fee=1)` → books, cash becomes +99; BUY on `cash=0` → `ValueError` unchanged; engine door test `_apply_fill` SELL with `cash=0` → applied, not refused.
- [ ] Step 2: run → FAIL today with `insufficient buying power: fee of 1.00…`.
- [ ] Step 3: implement in `CashLedger.apply_fill`: `debit = (notional if BUY else Decimal(0)) + fee` → for SELL replace proof line with `required = fee - notional` when `fee > notional` else skip proof; leave steps 3–4 untouched. Mirror identical change in `_require_cash_for_fill` (engine). Update both docstrings (the "fails closed" paragraph now reads "sells self-fund from proceeds; BUY is gated on cash incl. fee").
- [ ] Step 4: PR description must quote the doctrine line from ARCHITECTURE.md §7.3 ("sells are never margin-gated …") so the reviewer sees the conflict being closed.
- [ ] Step 5: run → PASS; full parity green.
- [ ] Step 6: Commit `fix(execution): sell orders self-fund fees from proceeds (unblock zero-idle-cash exits)`

### Task 8 (P2): MP-2 — strategy attribution survives mark/fee rebuilds ⟂

**Files:** Modify `execution/src/tradex_execution/position_accountant.py` (`on_fee`, `_remark_unlocked`), `execution/src/tradex_execution/mark_to_market.py` (rebuild site), Test: new `trading/tests/execution/test_strategy_attribution_persistence.py`.

- [ ] Step 1: failing property-style test (fixed RNG): fill BUY → `on_fee` → `remark(quote)` (engine rebuild paths) → `MarkToMarketService` update → assert `strategy_id` is the opening strategy and `realized_pnl` matches fold of domain math.
- [ ] Step 2: run → FAIL (attribution None).
- [ ] Step 3: add `strategy_id=existing.strategy_id` / `strategy_id=position.strategy_id` at all three rebuild sites (no other change).
- [ ] Step 4: rerun → PASS; `trading/tests/execution` green.
- [ ] Step 5: Commit `fix(execution): preserve strategy_id through mark and fee rebuilds`

### Task 9 (P3): MP-5 — bound the completed idempotency map ⟂

**Files:** Modify `execution/src/tradex_execution/idempotency.py` (`MemoryIdempotencyGuard`), Test: extend `execution/tests/…` or `trading/tests/execution/test_memory_guard_bounded.py`.

- [ ] Step 1: failing test: fill guard with 100_001 fake completions (max retained = `max_size=10_000` new ctor arg) → oldest key evicted; `release`/`record_result` contract unchanged; `check_and_reserve` after eviction answers as fresh (documented).
- [ ] Step 2: run → FAIL (unbounded).
- [ ] Step 3: implement: `self._completed: OrderedDict[str, Any]`; evict oldest beyond `max_completed` (default 10_000); reuse existing `_lock`.
- [ ] Step 4: rerun → PASS.
- [ ] Step 5: Commit `fix(execution): bound MemoryIdempotencyGuard completed map`

### Task 10 (P4): MP-6 — recovery stub counter + doc note ⟂

**Files:** Modify `execution/src/tradex_execution/recovery.py:92-105` (`_stub_order`), add counter via observability (`metrics.counter("cash_recovery_stub_fills_total")` — check optional `metrics` param pattern in existing code before introducing plumbing; if none exists in module, emit `log.warning` once per stub + TODO note in plan appendix, do not invent plumbing), Test: `trading/tests/runtime_audit`-style fold test asserting stub accepted and logged.

- [ ] Steps: write test (stub fill folds, warning logged) → implement minimal → PASS → commit `feat(execution): count strategy-orphaned recovery stubs`

### Task 11 (X1): 0003 tranche 1a — order DTOs + dict-body lint ⟂

**Files:** Create `interfaces/src/tradex_interfaces/contracts.py` (`PlaceOrderRequest`, `BracketOrderRequestDto`, `OrdersListResponse`, `OrderMutationResponse`), Modify `interfaces/src/tradex_interfaces/routes/orders.py` (swap `body: dict` on POST/orders, /bracket, PUT/{id}, DELETE/{id} → DTOs; response stays `OrderResponse`), Modify `interfaces/src/tradex_interfaces/models.py` (re-export), Test: `interfaces/tests/test_orders_dto_contract.py`; guard test `tests/test_guardrails.py` — new `test_no_dict_route_bodies` (owns file region; T-track rule).

**Interfaces:** Consumes existing `OrderRequest` validation (`__post_init__`) semantics but does NOT re-validate: thin pass-through DTO → `OrderRequest` mapper in `contracts.py::to_order_request`. Produces: 422 on malformed bodies with `{error:{code:"validation_error",message:…}}` (reuse `ErrorResponse`).

- [ ] Step 1: failing tests: POST `/orders` with missing `quantity` → 422 with code `validation_error`; with string-typed `quantity` coerced or rejected (choose: reject, matching domain) — decide in test; unknown extra keys rejected (`model_config extra="forbid"`).
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement DTOs + swap handlers + lint guard (AST scan `interfaces/routes/*.py` for `dict` in handler signature or `body: dict`; failure lists file:line).
- [ ] Step 4: rerun → PASS; `pytest interfaces/` green.
- [ ] Step 5: Commit `feat(interfaces): typed order-mutation DTOs (spec 0003 tranche 1a)`

### Task 12 (X2): spec 0003 tranche 1b — chart backtest/scanner DTOs ⟂

**Files:** Modify `interfaces/src/tradex_interfaces/routes/chart.py` (POST /backtest, /scanner/run bodies → `BacktestRequest`, `ScannerRunRequest` in `contracts.py`; backtest response → `BacktestResponse` with typed `equity_curve/metrics` only for pinned keys; unknown keys forbidden), Test: `interfaces/tests/test_chart_dto_contract.py`.

- [ ] Step 1: failing tests: `/api/charts/backtest` rejects unknown strategy id with typed 422; returns keys `metrics/equity_curve/trades` only (snapshot test).
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement.
- [ ] Step 4: rerun → PASS.
- [ ] Step 5: Commit `feat(interfaces): typed backtest/scanner DTOs (spec 0003 tranche 1b)`

### Task 13 (X3): history cache-hit/miss shape parity ⟂

**Files:** Modify `interfaces/src/tradex_interfaces/routes/chart.py:382-416` (cache-hit branch must emit same keys incl. `source`, `timeframe_resampled`), Test: `interfaces/tests/test_history_cache_shape.py`.

- [ ] Step 1: failing test hitting the endpoint twice (seed datalake fixture or cache) → assert key sets identical across hit/miss.
- [ ] Step 2: run → FAIL.
- [ ] Step 3: normalize hit payload through same shaper as miss.
- [ ] Step 4: rerun → PASS → commit `fix(interfaces): history response shape no longer varies by cache state`

### Task 14 (X4): error envelope & status-code unification ⟂

**Files:** Modify `fastapi_app.py` exception handlers only (no new modules), `auth/deps.py` (403→401 to match gateway policy *or* keep 403 — decide in test; keep 403 if CSRF-partition tests pin it; update if `test_cors_csrf_origin_parity` pins 401), Test: `trading/tests/interface/test_error_contract.py` (extend).

- [ ] Step 1: failing tests: both auth-failure paths (missing key, bad Origin for CSRF) and a 409 workspace conflict → assert `{error:{code,message}}` and codes from a closed enum: `auth_missing, auth_invalid, csrf_missing, not_found, validation_error, conflict, rate_limited, internal`.
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement envelope mapping table in the exception handlers (one dict); map workspace 409 detail dict into `message` without repr (construct `message="stale workspace revision"` + `detail` sub-object if schema allows; else `code:"stale_revision"`).
- [ ] Step 4: rerun → PASS → commit `feat(interfaces): one error envelope, closed code enum`

### Task 15 (X5): WS Origin check + session cookie `Secure` behind TLS flag ⟂

**Files:** Create `interfaces/src/tradex_interfaces/origin.py` helper or extend `auth/origin.py`; Modify `fastapi_app.py` (ws route pre-accept check; cookie `secure` from `TRADEX_TLS=on`), Test: `trading/tests/interface/test_ws_origin.py`.

- [ ] Step 1: failing tests: WS handshake with `Origin: https://evil.example` → connection refused/closed pre-accept; with `ui_origin` matching → accepted; cookie `Secure` set when `TRADEX_TLS=1` and unset otherwise.
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement; keep the loopback/dev fail-open semantics for paper mode *unchanged* (documented) — Origin check only denies cross-origin mismatch.
- [ ] Step 4: rerun → PASS → commit `fix(interfaces): Origin check on WS feed; Secure cookie behind TRADEX_TLS`

### Task 16 (X6): CSRF on remaining mutations + logout ⟂

**Files:** Modify `fastapi_app.py` route wiring (attach `require_csrf_if_session` dep to `/api/charts/backtest`, `/scanner/run`, `PUT /api/charts/workspace/{id}`, `DELETE …{id}`, `/auth/logout`), Test: extend `test_cors_csrf_origin_parity.py`-adjacent new test.

- [ ] Step 1: failing tests per route (session cookie, no CSRF header → 403; with header → not 403).
- [ ] Step 2: run → FAIL.
- [ ] Step 3: wiring only.
- [ ] Step 4: rerun → PASS → commit `fix(interfaces): CSRF guard on backtest, scanner, workspace writes, logout`

### Task 17 (X7): `live_orders_enabled` enforced server-side ⟂

**Files:** Modify `interfaces/src/tradex_interfaces/routes/orders.py` (preflight: if `app.state.config.live_orders_enabled is False` **and** session mode is `live` → 503 `live_orders_disabled`; paper/replay unaffected), `fastapi_app.py` (expose flag on app.state — verify existing `live_orders_enabled` symbol in `_helpers.py`), Test: extend orders DTO tests.

- [ ] Step 1: failing test: boot live config with flag false → POST /orders → 503 code `live_orders_disabled`; flag true → reaches engine (mock).
- [ ] Step 2: run → FAIL.
- [ ] Step 3: implement.
- [ ] Step 4: rerun → PASS → commit `fix(interfaces): enforce live order gate server-side`

### Task 18 (G1): gateway fold-prep — residual verification only ⟂ G

**Status re-scope (2026-11-06):** time helpers already consolidated upstream (`b739e892`); broker-owned chunk caps + lot sizes landed upstream (`af74c6ec`); the throwaway `_tf_seconds` is gone and the guard is green. Residual scope only:

- [ ] Step 1: read current `gateway/src/tradex_gateway/history.py` — verify no remaining duplicate of `parallel_fetcher` window logic (`_DHAN_INTRADAY_MAX_DAYS`, cap selection via `BROKER_LIMITS`); remove any that remains, routing through `tradex_domain.timeframe` / `tradex_config.broker_limits` as upstream established.
- [ ] Step 2: verify `broker_limits` is the single source (or document why both exist — they must not diverge silently; if two tables exist, file the drift as a finding in the report, don't fix blindly).
- [ ] Step 3: `gateway/tests` green; guard green; commit only if a change was made: `refactor(gateway): complete windowing consolidation with market_data/config limits`; else report "already consolidated" and close.

### Task 19 (G2): gateway wired into gates ⟂ G

**Files:** Modify root `pyproject.toml` (`--cov` package list to include `tradex_gateway`, `duck_analytics`), `scripts/predeploy_gate.sh` (import smoke list 14→16), `.github/workflows/quality-gate.yml` + `parity.yml` (clean-install imports), `deploy/Dockerfile` + `deploy/compose.yml` (gateway service stanza, profile-gated `gateway`), `gateway/README.md` (new: what it is, how to launch, env vars, its `/v1` policy), Test: none new — gate edits validated by running `bash scripts/predeploy_gate.sh` (may require docker for artifact step; record which steps ran).

- [ ] Steps: edit each file with one concern per change; run `uv run --no-sync python -m pytest tests/ -q` after pyproject edit; commit separately per file group: `chore(gates): coverage for gateway/duckdb`, `chore(deploy): gateway service definition`, `docs(gateway): launch runbook`.

### Task 20 (G3): duckdb second uv.lock removed ⟂ G

**Files:** Delete `services/duckdb-analytics/uv.lock` (after `grep -rn 'duckdb-analytics/uv.lock' . --exclude-dir=.git` → expect no CI consumer; confirm Docker COPY uses root lock), Test: `services/duckdb-analytics/tests` still green from root lock (`uv run --no-sync python -m pytest services/duckdb-analytics -q`).

- [ ] Steps: grep → delete → run its tests → commit `chore(duckdb): single root uv.lock`

### Task 21 (D1): ARCHITECTURE.md accuracy refresh ⟂ D

**Files:** Modify `docs/ARCHITECTURE.md` (recount via `find */src -name '*.py' | wc -l` and per-package lines; fix §4.1 table for `gateway` (add to L5 interfaces row or its own product row), §4.2 layering, §10 counts, §11 clear resolved drift items, add gateway + `screening.py`/`sensitivity.py` note as "unreleased WIP on this branch"; do **not** claim anything without re-running the measurement and pasting it).

- [ ] Steps: measure → update file → verify every `file:line` claim I added with `grep -n` → commit `docs(architecture): refresh counts/layers; gateway documented; drift section pruned`

### Task 22 (D2): CLAUDE.md/README pin-truth ⟂ D

**Files:** Modify `CLAUDE.md` ("Chart UI" section: pin be7418e = v2.3.2 label; remove v2.3.2/v2.3.2 mentions), `README.md` (TradeX wording: mention `gateway/` as external data plane + link to G2 doc; keep quick-start honest — no new claims), `docs/DIVERGENCE.md` (create: the twelve §4.3 divergences with owner=TBD/expiry=this-quarter row each).

- [ ] Steps: search-replace with exact pin label from `openalgo-charts.pin` → verify zero stale refs (`grep -nE '2\.1\.[78]' .`) → commit `docs: pin truth (v2.3.2); divergence ledger created`

### Task 23 (D3): boot doc-comment fixes only where false ⟂ D

**Files:** Modify `docs/audit/*` two files only if you find a fact you can falsify to be false (read-only otherwise); no code.

### Task 24 (T1): P0 parity tests — late-tick tape through SessionBarAggregator ⟂ T

**Files:** Create `trading/tests/parity/test_sba_late_tick_parity.py`.

**Interfaces:** Consumes `SessionBarAggregator`/`BarAggregator`, `TapeRecorder` replay helper (see `test_backtest_live_tape_parity.py` pattern), `SimulatedFillSource`. Produces: a tape with cross-minute late ticks + a revised bar; asserts (a) strategy receives bar close, (b) revised bar currently **fails** (documents A4) — `xfail(strict=True)` with issue link, (c) no duplicate fills after revision lands.

- [ ] Step 1: write test mirroring `test_backtest_live_tape_parity.py:585-785` structure; sources: `runtime/bar_aggregator.py:650-673` (`_revise_closed`), `:954-1000` (`SessionBarAggregator`).
- [ ] Step 2: run → expect xfail on the freeze part, pass on the rest.
- [ ] Step 3: commit `test(parity): session-bar-aggregator late-tick tape (A4 pinned as xfail)`

### Task 25 (T2): paper/live analytics wiring parity test ⟂ T

**Files:** Create `trading/tests/parity/test_paper_live_analytics_wiring.py`.

- [ ] Step 1: test: boot `TradingSession.paper(...)` minimal (follow `test_session_smoke.py` fixture pattern), strategy = `sma_cross`, feed 100 synthetic quotes; assert `context.analytics` is wired (not None) and signal order == backtest run of same bars (the golden trade count match). **Today this FAILS** (A3) — mark `xfail(strict=True, reason="A3: startup does not wire analytics in paper/live")` OR assert the *gap* exists as a diagnostic test. Choose xfail.
- [ ] Step 2: run → xfail.
- [ ] Step 3: commit `test(parity): pin A3 paper/live indicator-path divergence (xfail)`

### Task 26 (T3): replay-does-not-trade contract ⟂ T

**Files:** Create `trading/tests/contracts/test_replay_isolation_contract.py`: replay session must produce zero `OrderPlaced` on venue adapter spy and zero `ExecutionEngine.submit` calls (spy on engine).

- [ ] Steps: failing-asserting test (positive assertion today passes; the *contract* is what matters — pins A12) → verify green → commit `test(contracts): replay may never reach the venue`

### Task 27 (T4): Final-flush + tape determinism tests ⟂ T

**Files:** Create `trading/tests/parity/test_eod_flush.py` (assert 15:30 flush reaches strategy — expect FAIL, xfail A5) and `trading/tests/parity/test_tape_roundtrip.py` (record→replay produces identical fill stream; must pass on main).

### Task 28 (T5): CI guard — gateway stays under the coverage ratchet ⟂ T

**Files:** Modify `tests/test_guardrails.py` region reserved in T-track only: add `test_no_dict_route_bodies` here or keep it inside Task 11 — **assign to Task 11** (single owner); this task: add `test_gateway_in_coverage_list` asserting `--cov=tradex_gateway` present in the quality-gate file (grep-based, 5 lines).

- [ ] Steps: add test → PASS → commit `test(gates): gateway stays under coverage ratchet`

### Task 29: Merge gate & drift report ➜ all

- [ ] Step 1: rebase each worktree on latest main; resolve expected conflicts (docs only beyond M/P/X overlap — none by ownership map).
- [ ] Step 2: `uv run --no-sync python -m pytest -q` full suite; `trading/tests/parity` + `contracts`; `tests/test_guardrails.py`.
- [ ] Step 3: failing anywhere → the owning track fixes forward before merge; no `.gitignore` or baseline bumps permitted.
- [ ] Step 4: produce `docs/superpowers/plans/2026-11-06-remediation-report.md`: tasks done/blocked, commands run, delta metrics (guard failures, coverage flags). Commit `docs(plans): wave 0–1 remediation report`.

---

## Decisions (resolved 2026-11-06)

| ID | Question | Decision |
|---|---|---|
| D-1 | SELL exit doctrine | **Net-proceeds proof** (`apply_fill`: BUY gated on `notional+fee` vs cash; SELL gated on `max(0, fee − notional)`) — Task 7 |
| D-4 | Gateway disposition | **Fold in-repo** — CI coverage + clean-install + predeploy + Dockerfile stanza + docs (Tasks 18–20) |
| D-2 / D-3 / D-5 / D-6 / D-7 | Frontend; RevisedBar fate; duckdb; sizer divergence; 0003 order | Still open — none block Wave 0/1; D-5: fold+document (matches D-4); D-7: orders-first (Tasks 11→12) |
| Pre-flight | Task 2 upstream-fixed; Task 18 residual-only; dispatch = **track-per-worktree, parallel agents** | Recorded 2026-11-06 |

## Decisions (retained for reference)

| ID | Question | Recommended | Needed by |
|---|---|---|---|
| D-1 | SELL exit doctrine: net-proceeds proof (this plan, Task 7) vs exempting sells from all cash proofs | net-proceeds proof | Task 7 |
| D-2 | Frontend rebuild: green-light contract-first rebuild (per review §5), defer, or freeze forever? | green-light, after Wave 1 merges | Wave 2 |
| D-3 | `RevisedBar` fate: delete + delete `mark_filled`, or first-class `CandleRevision` with freeze | delete both (prefer deletion) | Wave 2 |
| D-4 | Gateway: fold into interfaces/repo home (after Task 19) vs spin out to own repo | fold; revisit if external consumer ships | Task 19 sign-off |
| D-5 | `duck_analytics`: keep in-repo with launch docs vs spin out | keep, wire in Wave 1 | Task 20 |
| D-6 | Sizer divergence (8,333 vs 7,916): accept + `DIVERGENCE.md` owner=you, expiry=90 days, vs unify immediately | accept beneficially w/ owner; unify is Wave-2 work | Wave 2 |
| D-7 | Spec 0003 order: orders→chart (this plan) vs chart→orders | orders-first | Task 11/12 |

## Out of scope for this plan (Wave 2, separate spec after D-2/D-3)

Incremental `IndicatorEngine` push-model; `BarClosed`/`MarketEvent` union; UTC-aware timestamps everywhere (datalake boundary pinning); `SessionLifecycle` with end-of-day seal (15:30 flush); replay-as-ReplayClock subsumption of `ReplayController`; frontend host rebuild; `application/` deepening; full 0003 rollout to remaining routes; CI chaos nightly for gateway.

## Self-review (performed while writing)

1. **Spec coverage:** A1 (T2), A2 (D2 portal — decision only; no code owed now), A3 (T-T2), A4 (T-T1 xfail + Wave 2 D-3), A5 (T-T4 xfail), A6 (Wave 2; wall-clock noted in D2 doc), A7/A8/A9 (Wave 2 + DIVERGENCE rows in Task 22), A10 (Wave 2), A11 (Wave 2), A12 (T-T3 contract), A13 (Wave 2 T + doc D1), A14 (Task 4), A15 (Task 4), B1 (Tasks 11–12 tranche 1; rest of routes Wave 2), B2 (Task 17), B3 (Task 15), B4 (accepted dev fail-open; not changed — documented in Task 15), B5 (Task 14), B6 (Task 19/D-4 — one `/v1` policy both services; interfaces stays unversioned root mounts with documented policy; flagged in Task 19 docs), B7 (Task 16), B8 (Task 13), B9 (defer: keep both endpoints, add Deprecation header on `/api/charts/account` — not assigned; **gap**, accepted for Wave 2), B10 (Wave 2 0003 tranche 2), B11 (stream.py ✎ — moved Wave 2), B12 (Wave 2 with 0003), B13 (Task 11 OpenAPI gate covers), C1–C9 (Tasks 3–5, 9–10, G-track), D1–D6 (Tasks 18–20, D-track), MP-1…MP-6 (Tasks 5,7,8,9,10). Gap found & accepted: **B9/B11** — moved to Wave 2 explicitly; recorded in D2's DIVERGENCE.md (add row).
2. **Step scan:** every step names one action + one checkable result; no bodies embedded except DTO shapes (interfaces) which are signatures-by-contract — acceptable.
3. **Type consistency:** `DurableFillDedup(store, max_size, on_evict, account_id)` used consistently; `bucket_seconds(tf)` signature matches domain; `OrderRequest` untouched.
4. **Review Focus:** lines 1–5 map to Tasks 5, 7, 11/28, 8, 2/19. All pinned with named tests.
5. **Proportion:** 29 tasks ≈ 2.5 pages of steps + headers — consistent with the size of the two specs; no transcripts of code.

**Known conflicts to watch at merge:** `runtime/startup.py` appears in Task 5 (one-line) and Task 4 (boot assert) — same file, additive regions; Track M merges after Track D's boot-adjacent changes or vice versa at the merge gate; `tests/test_guardrails.py` edited by Tasks 11 and 28 — sequential ownership (T) and non-overlapping regions.
