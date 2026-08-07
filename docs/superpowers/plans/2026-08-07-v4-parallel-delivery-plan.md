# Delivery Plan: v4 → Documented Target Architecture (Parallel Agent Swarm)

## Status

- **Date**: 2026-08-07
- **Owner**: v4 coordinator
- **Approver**: User
- **Precedent**: `docs/superpowers/specs/2026-08-06-env-local-loading-design.md` — completed spec cycle (implemented: `config/env.py::load_env_file`, `--env-file` in `cli.py` + `check_connection.py`, tests green). This plan follows the same superpowers workflow: **spec → plan → parallel task dispatch → review → integration**.

---

## 1. Problem

The implementation-focused documentation (file organisation, architecture, flows) describes the **target architecture**. The codebase is already ~95% there — but a small set of structural deltas remain, and the working tree carries an uncommitted protocol refactor. The remaining work is small enough to be **fully parallelizable** across independent file scopes, which is exactly what an agent swarm is for.

Goal: converge v4 to the documented layout, close the gaps, stabilize + commit the in-flight refactor, and re-sync the architecture docs — using one agent per workstream, run concurrently where dependency-free.

---

## 2. Verified Baseline (2026-08-07)

Measured today; this is the floor every workstream must preserve:

| Repo | Source files | Tests | Lint |
|------|-------------|-------|------|
| `domain/` | 13 | **343 passed** | ruff clean |
| `brokers/` | 30 | **899 passed, 2 skipped** | ruff clean |
| `trading/` | 83 | **1291 passed** | ruff clean |
| **Total** | **126** | **2,533 passed** | clean |

- Installed/effective RxPY: **`rx==3.2.0`** (this is what all three packages import and test against).
- `runtime/` **exists** (`startup.py` boot, `live.py` build_broker_from_env, `market_feed.py`, `calendar.py`, `health.py`, `metrics.py`, `session_states.py`, `master_lifecycle.py`) — F26 ✅ as documented.
- The 2026-08-06 `.env.local` spec is **implemented and tested** (`--env-file` + `load_env_file` in both CLIs).

### Working tree (uncommitted, all tests green)

The refactor already in the tree should be **committed first** (WS-0), not re-done:

- `domain`: `Clock`, `IndicatorComputer` protocols added + exported.
- `trading`: `ScannerEngine` now depends on `IndicatorComputer` (protocol, not concrete `AnalyticsEngine`).
- `strategy/engine.py`: `ReactiveStrategyEngine` bridges `Signal → PlaceOrderCommand` (CQRS per documented flow).
- `replay/backtest.py`: removed `ExecutionEngine` delegation — FillSource is the only execution seam (matches documented flow).
- `replay/optimization.py`: `walk_forward` extracted to `replay/walk_forward.py`.
- Deleted: `strategy/scanner_runtime.py`, `strategy/scanner_runtime` tests, `replay/test_backtest_with_engine.py`; removed dead `breadth_indicator` stub from `analytics/functions.py`.
- `trading/pyproject.toml`: `rx>=7.0,<8` → **`rx>=3.2,<4`** (correct — matches installed reality).

---

## 3. Gap Analysis (Documented Target vs. Current Tree)

| # | Documented target | Current state | Effort |
|---|-------------------|---------------|--------|
| G1 | `strategy/core/` subpackage (protocols, engine, scanner, scanner_runtime, ensemble, buy_and_hold) | Flat `strategy/` (6 modules, no `core/`) | Medium — move + update imports |
| G2 | `strategy/extensions/` auto-discovery (`__init__` aggregates, `strategies/`, `scanners/`, `shared/`) | **Does not exist** — the biggest functional gap | Medium — new package |
| G3 | `strategy/core/scanner_runtime.py` (wires scanner to session) | Deleted in working tree; `sdk/services/scanner.py` `ScannerService` covers wiring | Decision — see §5.3 |
| G4 | `reactive/operators.py` | Actual: `stream_operators.py` (zero production imports; 1 test ref) | Trivial |
| G5 | `domain/pyproject.toml` `rx>=7.0,<8` | Stale vs. installed `rx==3.2.0`; conflicts with trading's new `>=3.2,<4` | Trivial |
| G6 | `feature-parity.md` / `DEEP_REVIEW.md` describe old state | Say "4 reactive files", `operators.py`, `scanner_runtime.py`, `rx>=7.0`, 113 files, 2,307 tests | Small |
| G7 | Uncommitted refactor (see §2) | Green but uncommitted | Small |

**Everything else in the documented architecture is present and green** — do not re-implement domain, brokers, execution, SDK, datalake, replay, analytics, runtime, interface, or config.

---

## 4. Workstream Decomposition & Dependency Graph

```
Phase 0 (sequential foundation)
  WS-0  Stabilize + commit working tree, align rx constraint ── coordinator
                     │
Phase 1 (4 agents AUTHOR IN PARALLEL — disjoint file scopes)
  ┌───────────────┬──────────────────┬──────────────────┬──────────────────┐
  WS-1            WS-2               WS-3               WS-4
  strategy/core + reactive/          SDK Scanner       lockfile/boundary
  extensions/     operators rename   wiring audit      verification
  (Agent A)       (Agent B)          (Agent C)         (Agent D)
  └───────────────┴──────────────────┴──────────────────┴──────────────────┘
                     │ (coordinator integrates each landing, then verifies)
Phase 2 (sequential)
  WS-5  Docs sync (feature-parity.md, DEEP_REVIEW.md) ── Agent E (after WS-1..4)
  WS-6  Final gate: full suites + ruff + compileall + code review ── coordinator
```

**Dependency edges:** WS-1..WS-4 depend only on WS-0. WS-5 depends on WS-1..WS-4 landing. WS-6 depends on everything.

**Execution model (shared working tree — no branch isolation):** This repo is a **single shared checkout**; agents are NOT isolated on branches. Therefore the plan uses **parallel authoring + sequential verification**: the four Phase-1 agents may *write* concurrently because their file scopes are disjoint by construction (§6), but (a) no agent may run the full test suite while another agent's changes are mid-flight — the coordinator runs the validation gate after each workstream lands; (b) any agent that needs to *read* a file being actively moved by another workstream must read the pre-merge state and re-verify after integration (WS-1 is a pure move — no semantics change — so pre-move reads stay valid); (c) no agent commits — the coordinator integrates each workstream in sequence, re-running that workstream's tests before moving on.

**Conflict rule:** file scopes are disjoint by construction (ownership table §6). No two agents ever edit the same file. If an agent needs to touch a file owned by another workstream, it must report back instead of editing.

---

## 5. Workstream Specifications

### WS-0 — Stabilize & commit (coordinator, no parallel agents)

1. Confirm full baseline green (already verified today: 2,533 pass).
2. Align dependency metadata to installed reality:
   - `domain/pyproject.toml`: `rx>=7.0,<8` → `rx>=3.2,<4`.
   - `brokers/pyproject.toml`: check constraint; align to `rx>=3.2,<4` if it pins 7.x.
   - Re-run `uv lock` / `uv sync --check` so `uv.lock` satisfies all three packages.
3. Commit the working-tree refactor (it is coherent and green — do not split or rewrite it). **Shared-checkout hygiene:** first inspect `git status` and `git diff` to confirm which uncommitted hunks belong to this refactor; scope `git add` to exactly those files (never `git add -A`). If ownership is ambiguous (hunks that look like another thread's work), leave the commit to the owner and proceed. Message style per repo history.
4. **Exit criterion:** 3 repos green + lockfile resolves + refactor committed (or ownership-conflict noted).

### 5.3 — Decision (G3): `scanner_runtime.py` status

**Recommendation: keep it deleted.** The working tree deliberately removed `scanner_runtime.py` + its tests, and the branch direction is dead-code cleanup (`v2-cleanup-remove-dead-temp-legacy`). `sdk/services/scanner.py` (`ScannerService`) already wires `ScannerEngine` to sessions, which is the documented flow. Therefore `strategy/core/` ships with **5 modules** (protocols, engine, scanner, ensemble, buy_and_hold) — the documented `scanner_runtime.py` is folded into the SDK service, and WS-3 provides the evidence. WS-5 documents this deviation explicitly. Revisit only if the user overrides.

### WS-1 — `strategy/core/` + `strategy/extensions/` (Agent A)

**Scope (exclusive):** `trading/src/tradex_trading/strategy/**`, `trading/tests/strategy/**`, plus the 3 known external refs: `trading/src/tradex_trading/replay/backtest.py`, `trading/tests/replay/test_replay_backtest.py`, `trading/tests/integration/test_full_stack.py`. Grep for `tradex_trading.strategy` to catch stragglers before finishing.

**Deliverables:**
1. Move to `strategy/core/`: `protocols.py`, `engine.py`, `scanner.py`, `ensemble.py`, `buy_and_hold.py` (module-relative imports inside strategy/** updated accordingly).
2. Create `strategy/extensions/` auto-discovery skeleton (documented layout):
   - `extensions/__init__.py` — aggregates all strategies/scanners from the sub-packages.
   - `extensions/strategies/__init__.py` — imports user strategy classes; `extensions/strategies/` package dir.
   - `extensions/scanners/__init__.py` — imports `ScannerDefinition` objects; package dir.
   - `extensions/shared/` — custom indicators/helpers (package dir with `__init__.py`).
   - Discovery contract (pinned): `extensions/__init__.py` imports the `strategies/` and `scanners/` sub-packages and exposes two lists — `all_strategies` and `all_scanners` — built with `isinstance(obj, Strategy)` (the protocol is `@runtime_checkable`) and `isinstance(obj, ScannerDefinition)`. `ReactiveStrategyEngine.register()` and `ScannerEngine` consume these lists. Ship with empty-but-wired packages; **optionally** add one example each (e.g. `SmaCrossStrategy`, a `momentum` scanner definition) to prove the mechanism — marked clearly as reference material in `extensions/`, **core files untouched**.
3. Keep the public surface: `strategy/__init__.py` must still export `Strategy`, `ReactiveStrategyEngine`, `ScannerEngine`, `BuyAndHoldStrategy`, `StrategyEnsemble`, `StrategyEntry`. Decide per-module `core/` imports update all callers within scope.
4. Tests: update the 8 in-scope test files' imports; add `trading/tests/strategy/test_extensions_discovery.py` proving auto-discovery.

**Constraints:** Do NOT touch `sdk/`, `runtime/`, `execution/`, or `reactive/`. Do NOT remove the `Signal → PlaceOrderCommand` bridge in `engine.py`. Return summary of moved modules + import changes.

### WS-2 — `reactive/operators.py` naming (Agent B)

**Scope (exclusive):** `trading/src/tradex_trading/reactive/**`, `trading/tests/reactive/test_reactive_regression.py`.

**Decision (recommended):** rename `stream_operators.py` → `operators.py` for doc fidelity — blast radius is 1 test file (`test_reactive_regression.py` imports `from tradex_trading.reactive import stream_operators`); zero production imports. Update `reactive/__init__.py` to export the documented public surface — `of_type`, `share`, `replay_buffer`, `distinct_until_changed`, `map_to`, `filter_safe` — so `from tradex_trading.reactive import operators` and the operators work without importing internals. Alternative (acceptable): keep `stream_operators.py` and note the name in WS-5 docs.

**Exit criterion:** `trading/tests/reactive/` green; no reference to `stream_operators` remains (or a single justified one).

### WS-3 — SDK scanner wiring audit (Agent C)

**Scope (exclusive):** read-mostly on `trading/src/tradex_trading/sdk/services/scanner.py`, `sdk/session.py`, `strategy/scanner.py`; writes only under `trading/tests/sdk/` and `trading/tests/contracts/`.

**Deliverables:**
1. Verify the documented scanner flow end-to-end: `session.scanner.run(definition)` → `ScannerEngine.run()` → `ScannerResult`; and the order flow: strategy `Signal` → `PlaceOrderCommand` (engine bridge) → `ExecutionEngine` → `FillSource`.
2. Add contract tests (in `trading/tests/contracts/` or `tests/sdk/`) proving the CQRS bridge works through the real bus: strategy registered on `ReactiveStrategyEngine` emits a `Signal` on a bar; assert a `PlaceOrderCommand` lands on the bus and the `ExecutionEngine` (with `SimulatedFillSource`) produces an `OrderPlaced` event.
3. Confirm `ScannerService` makes `core/scanner_runtime.py` unnecessary (see WS-1 G3 note) — report evidence.

**Constraints:** read-only on `strategy/**` (WS-1 owns it this cycle); do not edit `sdk/services/scanner.py` unless a real bug is found — if so, report it rather than fix (or coordinate). Return the flow verification + test names.

### WS-4 — Lockfile & boundary verification (Agent D)

**Scope (exclusive):** `domain/pyproject.toml`, `brokers/pyproject.toml`, `uv.lock`, boundary tests (`tests/test_import_boundaries.py`, `brokers/tests/test_paper_broker_no_trading_import.py`).

**Deliverables (verification-only — WS-0 already owns pyproject/lockfile edits):**
1. Verify `uv.lock` resolves with WS-0's aligned constraints (`uv lock --check` or `uv sync --check`).
2. Re-assert boundaries: `tradex-domain` imports only stdlib + rx; `tradex-brokers` never imports `tradex_trading`; `tradex-trading` imports both (grep + existing boundary tests).
3. **No file edits.** If WS-0's aligned constraints still fail to resolve, or a boundary violation is found, report the exact fix to the coordinator — do not edit `pyproject.toml`/`uv.lock` yourself (they are committed in WS-0; uncommitted edits would collide).

**Exit criterion:** `uv lock` resolves; boundary tests green.

### WS-5 — Docs sync (Agent E, after WS-1..4 merge)

**Scope (exclusive):** `docs/feature-parity.md`, `docs/DEEP_REVIEW.md`, and the pasted target doc if it lives in `docs/`.

**Deliverables:** reconcile with the *post-implementation* tree:
- Reactive: 9 modules (`bus`, `bounded_bus`, `thread_safe_bus`, `operators`, `backpressure`, `subscription`, `async_dispatch`, `message_log`, + `__init__`).
- Strategy: `core/` + `extensions/` layout; `scanner_runtime.py` status per the G3 decision; count `strategy/` as 6 core modules.
- Domain deps: `rx>=3.2,<4` (not 7.x).
- Source count: ~126 files; test count: 2,533+ (re-measure after WS-1..4).
- Feature-parity matrix: keep ✅ statuses only where tests actually exist; mark new `extensions/` discovery with its test.
- **Constraint:** facts only — verify each claim against the tree before writing.

### WS-6 — Final gate (coordinator)

1. `requesting-code-review` skill: `BASE_SHA` = WS-0 commit, `HEAD_SHA` = integration head; dispatch reviewer with the workstream summary.
2. Run the full integration gate (see §7) on all three repos.
3. Act on Critical/Important reviewer findings; note Minor.

---

## 6. File Ownership Table (conflict avoidance)

| Path prefix | Owner |
|-------------|-------|
| `domain/pyproject.toml`, `brokers/pyproject.toml`, `uv.lock`, root `tests/`, `brokers/tests/test_paper_broker_no_trading_import.py` | WS-4 / WS-0 (sequential) |
| `trading/src/tradex_trading/strategy/**`, `trading/tests/strategy/**`, `replay/backtest.py`, `tests/replay/test_replay_backtest.py`, `tests/integration/test_full_stack.py` | WS-1 |
| `trading/src/tradex_trading/reactive/**`, `trading/tests/reactive/**` | WS-2 |
| `trading/tests/sdk/**`, `trading/tests/contracts/**` (new tests only) | WS-3 |
| `docs/*.md` | WS-5 |
| Everything else | Read-only for all agents this cycle |

---

## 7. Validation Gate (each workstream + final)

```bash
# Per repo, after each workstream lands (coordinator runs these; agents don't):
cd domain && python -m compileall -q src && python -m pytest -q && python -m ruff check src
cd brokers && python -m compileall -q src && python -m pytest -q && python -m ruff check src
cd trading && python -m compileall -q src && python -m pytest -q && python -m ruff check src
# Lockfile:
uv lock --check
# Boundaries (files live at repo root, not under trading/):
python -m pytest tests/test_import_boundaries.py -q
cd brokers && python -m pytest tests/test_paper_broker_no_trading_import.py -q
```

Baseline to beat: 343 + 899 + 1291 = **2,533 passed (2 skipped), ruff clean**.

---

## 8. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| WS-1 import fallout beyond the 3 known external refs | Agent A must grep `tradex_trading.strategy` repo-wide before finishing; WS-6 full-suite gate catches stragglers |
| WS-3 and WS-1 both touch the strategy bridge conceptually | Disjoint file scopes enforced; WS-3 is read-only on `strategy/**` and reads the pre-move layout (WS-1 is a pure move) |
| Parallel agents on a shared checkout interfere | Execution model (§4): parallel *authoring* only, sequential *verification*; no agent commits or runs the full suite while another workstream is mid-flight |
| rx constraint change breaks `uv.lock` | WS-0 verifies with `uv lock` immediately after editing; WS-4 re-checks in parallel |
| Docs claim drift (WS-5) | Constraint: verify every claim against the tree; no aspirational statuses |
| Agents editing same files | Ownership table §6 is the contract; violations are reported, not edited |
| `scanner_runtime.py` reinstated against the cleanup direction of this branch | G3 decision (§5.3): keep it deleted, document `ScannerService` as canonical — revisit only if the user overrides |

---

## 9. Out of Scope

- No new broker adapters, no new SDK services, no new analytics.
- No mypy CI setup (DEEP_REVIEW notes 258 pre-existing mypy errors — tracked separately).
- No production deployments, no env/credential changes, no `.env.local` handling beyond the existing `--env-file` flow.
- No changes to `domain/src` beyond the rx constraint metadata in `pyproject.toml`.
- v3 is legacy and untouched.
