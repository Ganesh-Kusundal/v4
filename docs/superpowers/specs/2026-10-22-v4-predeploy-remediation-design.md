# v4 Pre-Deployment Remediation — Design

**Date:** 2026-10-22
**Status:** Approved for planning; implementation not started
**Source:** Pre-deployment system review, verdict NO-GO
**Branch:** `feat/tier1-3-remediation` (HEAD `824a772c`)

## 1. Context

The review found ten release blockers. They are not style issues. Each one
either prevents deployment, silently drops orders, or hides a failure:

1. `deploy/Dockerfile` copies `operations/` and `persistence/`, which were
   deleted, so the image cannot build.
2. The image never puts `/app/.venv/bin` on `PATH`, so `ENTRYPOINT ["tradex",
   "serve"]` cannot resolve `tradex`.
3. There is no `.dockerignore`; `COPY . .` bakes the host `.venv`, `.env.local`
   and runtime databases into the image.
4. `tradex_shared` is imported by eleven production modules but is not an
   installable workspace package. Editable installs and a stale `.pth` mask
   this locally; a clean install fails.
5. Live orders are disabled at the adapter boundary: every
   `build_*_from_env` defaults `allow_order_operations=False`, and nothing
   overrides it.
6. `tradex serve --broker DHAN` cannot boot: it passes `wire_strategies=True`
   with zero strategies and no persistence path, and `boot()` refuses both.
7. Paper order correlation ids are deterministic per engine instance and the
   idempotency guard is durable, so a second session silently replays the first
   session's orders as duplicates.
8. `SQLiteIdempotencyGuard.check_and_reserve` leaves the implicit SQLite write
   transaction open on the duplicate path, which produces `database is locked`
   under concurrency.
9. A non-durable event append in paper mode is swallowed, and the engine can
   return a success receipt for a fill whose log write failed.
10. The suite is red: 78 failures, including the real
    `test_no_foreign_private_writes` violation, and tests share the repo
    `runtime/` databases.

## 2. Goals

- A clean clone can install and run the test suite with no editable-install
  leftovers.
- `docker build` succeeds and the container can import the application and run
  `tradex --help` as a non-root user.
- Live mode has exactly one order gate, one strategy-selection source, and one
  durable idempotency scope.
- A duplicate submit returns the original `OrderReceipt`; it never silently
  drops an order and never leaves an open SQLite transaction.
- A failed durable append halts the order and surfaces a failure; it never
  reports success.
- The event log is the only persistence authority for the OMS.
- The full suite is green and hermetic.

## 3. Non-Goals

- No new trading features, indicators, brokers, or endpoints.
- No live venue authentication or real-money order in this work.
- No compatibility layer for deleted packages, modules, or correlation ids.
- No production database migration. The only durable databases in the tree are
  local development artifacts and are deleted.

## 4. Decisions

**D1 — Fold `tradex_shared` into `tradex_domain`.** The package is three
files, depends only on `tradex_domain`, and is not installable. It becomes
`tradex_domain.rate_limiting` and `tradex_domain.market_builder`; `shared/` is
deleted. There is no shim.

**D2 — One live-order gate.** `AppConfig.live_orders_enabled` is read by
`from_env`, passed to `build_broker_from_env(allow_order_operations=...)`, and
bound to the `RiskManager` master gate and the session gate. Default false. A
live session still boots when the gate is closed; every submit is refused.

**D3 — Explicit live strategies.** `ExecutionConfig.strategy_ids` names the
strategies a session may run. `wire_strategies=True` in live mode with an empty
selection raises before broker connection. `auto_register_examples` is
paper/backtest/replay only; setting it in live raises. `wire_strategies=False`
is manual API mode and is allowed.

**D4 — Session-scoped correlation ids.** `ReactiveStrategyEngine` takes a
`run_id` (fresh random id by default) and includes it in every strategy
correlation id. Two sessions processing the same tape no longer collide in the
durable guard.

**D5 — One receipt type on replay.** `submit()` always returns
`OrderReceipt`. The guard stores the receipt for the first submission and
returns it on replay. HTTP and CLI stop compensating for an `OrderId` result.

**D6 — Fail-closed durable writes in paper and live.** When an event store is
configured, `require_durable_events` is true for paper and live. A failed
append raises out of `submit`, trips the kill switch, releases the idempotency
reservation, and never returns a fabricated order id.

**D7 — Event log is the sole persistence authority.** `SQLiteOrderStore`,
`attach_order_persistence`, and `load_into` are deleted. OMS recovery replays
`SQLiteEventStore` through `recover_trading_cache`.

**D8 — Session readiness requires its monitors.** `TradingSession.start()`
starts the feed and reconciliation monitors first; a monitor that cannot start
leaves the session in `NEW` and raises. `TradingSession.stop()` calls
`engine.shutdown()` so the guard connection and fill subscription close.

**D9 — Delete dead code.** The verified dead/duplicate modules, helpers,
constants, shim packages, and stale docs from the review are removed. Tests
that exist only to exercise the deleted code are removed with it.

**D10 — Hermetic tests and one deploy gate.** Tests use `tmp_path` for every
durable path. The repo `runtime/` databases are deleted. CI uses the real
`duck_analytics` package path. A `deploy` gate runs clean install, lint,
types, tests, and a Docker build.

## 5. Work Packages

### WP0 — Baseline and repository hygiene

- Fix `conftest.py`: drop the nonexistent `trading/src` path and the stale
  three-project docstring; add `shared/src` only until WP1 removes it.
- Fix `tests/test_no_foreign_private_writes.py` to scan only directories that
  exist, and fix the one real violation in
  `market_data/parallel_fetcher.py` with a public seam.
- Fix `.github/workflows/quality-gate.yml` mypy path from
  `services/duckdb-analytics/src/tradex_duckdb` to `.../duck_analytics`.
- Record the baseline failure list before changes.

**Exit:** the foreign-private-write guard passes for the right reason; the
baseline is recorded.

### WP1 — Packaging and container

- Fold `shared/` into `tradex_domain` (D1), update all importers and tests,
  delete `shared/`, remove `tradex_shared` from import-linter.
- Dockerfile: remove `operations/` and `persistence/` copies, add
  `ENV PATH="/app/.venv/bin:${PATH}"`, keep the two build-time assertions.
- Add `.dockerignore` excluding `.git`, `.venv`, `.env*`, runtime state,
  caches, coverage, and data.
- Extend `observability/tests/test_deploy_artifacts.py` to assert every
  `COPY <dir>/pyproject.toml` path exists and `PATH` contains `.venv/bin`.

**Exit:** `docker build` succeeds; a container runs
`python -c "import tradex_domain, tradex_runtime"`; a clean `uv sync --frozen`
imports `tradex_domain` with no `shared/`.

### WP2 — Configuration and live boot gates

- `from_env()`: invalid `TRADEX_BROKER` raises instead of falling back to
  paper; read `TRADEX_LIVE_ENABLED`, `TRADEX_LIVE_ORDERS_ENABLED`,
  `TRADEX_PERSISTENCE_PATH`, `TRADEX_STRATEGY_IDS` (comma-separated).
- `ExecutionConfig.strategy_ids` (default empty, stripped, de-duplicated).
- `boot()`: live broker construction passes
  `allow_order_operations=cfg.live_orders_enabled`.
- `boot()` strategy selection: explicit ids only; unknown id raises; live with
  `wire_strategies=True` and no ids raises; `auto_register_examples` in live
  raises. Per-binding definitions remain available for named definition ids.
- `serve_app()` and `cmd_serve()`: live uses `wire_strategies=bool(ids)` and
  materializes a default persistence path under
  `<runtime_dir>/live/<broker>.sqlite` when none is set.
- `TradingSession.start()`/`stop()` per D8.

**Exit:** live boot fails closed for missing strategy selection and missing
persistence; live orders are refused at the adapter when the flag is false; a
live serve configuration can boot in manual API mode.

### WP3 — Idempotency, receipts, durability

- Add `run_id` to `ReactiveStrategyEngine`; include it in the cid seed.
- `SQLiteIdempotencyGuard`: wrap writes in `with self._conn:` and roll back
  before reading on `IntegrityError`; no open transaction on the duplicate
  path.
- `OrderReceipt` is stored on first completion and returned on replay; the
  `OrderId` marker is read only to migrate an existing row, then removed.
- `require_durable_events` is true for paper+live when an event store exists.
- `_apply_fill_to_book` raises when the fill append fails.
- Risk/dispatch durability failures release the reservation.
- Delete `_no_receipt` and the fabricated UUID receipt.

**Exit:** cross-session paper submits are both accepted; concurrent duplicate
reservation leaves no transaction open; replay returns the original receipt;
a failed append raises and leaves no completed reservation.

### WP4 — One persistence authority

- Remove `order_store` from `boot`/`_boot_tail`/session wiring.
- Delete `SQLiteOrderStore`, `attach_order_persistence`, and the order-store
  tests that duplicate event-log recovery.
- Rewrite the bracket-pipeline recovery test to use `SQLiteEventStore`.
- Delete `RuntimeContext` and `boot_context`; no production caller.
- Delete `ExecutionEngine.ingest_fill`; no production caller.

**Exit:** a paper/live restart recovers orders and cash from the event log
alone; deleting the mirror changes no test outcome.

### WP5 — Dead code and docs

Delete, with their tests and references:

- `research/` package and its workspace entries; `market_data/tests/
  test_pit_universe_artifacts.py` (tests only dead research code);
  `trading/tests/research/`.
- `interfaces/replay_run.py`; `execution/risk_manager.py` and its tests;
  `execution/fees.py:PricingService`; `execution/fill_sources.py:
  ReplayFillSource`; `execution/slippage.py:SlippageAwareFillSource`;
  `execution/audit_log.py`; `brokers/common/http_response.py`;
  `runtime/environments.py`; `strategy/artifacts.py:StrategyArtifactStore`;
  `replay/backtest.py:run_with_loader`; `brokers/common/rate_table.py:
  get_rate_limit_config`; `paper_book_fill_source.py:resolve_fill_path`;
  analytics helpers `naked_levels`, `row_of`, `cumulative_delta`,
  `stacked_imbalances`; `routes/__init__.py:ALL_ROUTES`;
  `market_data/universe.py`; `trading/pyproject.toml`; stale
  `scripts/extract_package.py` references; zero-reference domain constants.
- Stale docs: `PREDEPLOY_SYSTEM_REVIEW.md`, `trading-predeploy-review.md`,
  `MULTI_AGENT_CLEANUP_PLAN.md`, `docs/ARCHITECTURAL-AUDIT-2026-09-25.md`,
  `docs/architecture-review.md`, `docs/proposed-target-architecture.md`,
  `docs/target-architecture-design.md`.

**Exit:** a repo-wide grep for every deleted symbol is empty outside `docs/
superpowers/` history; the suite has no skipped/deleted-code tests.

### WP6 — Hermetic suite and deploy gate

- Every test durable path uses `tmp_path`; delete the repo `runtime/*.db`
  artifacts.
- Add the mandatory tests listed in Section 6.
- CI: correct mypy path, add a Docker build smoke job, add a clean-install
  import check.
- `scripts/predeploy_gate.sh`: clean install, ruff ratchet, mypy all packages,
  import-linter, full pytest, Docker build.
- Update README and `docs/ARCHITECTURE.md` to describe the actual packages and
  the live gates.

**Exit:** `scripts/predeploy_gate.sh` exits 0 on a clean tree.

## 6. Mandatory Tests

1. Clean clone: `uv sync --frozen` then import every package with `shared/`
   absent.
2. Docker build and non-root import/`tradex --help`.
3. Invalid `TRADEX_BROKER` raises.
4. Live `wire_strategies=True` with no `strategy_ids` raises before connect.
5. Explicit live strategy id registers; unknown id raises.
6. `auto_register_examples=True` in live raises.
7. Live `live_orders_enabled=False` reaches the broker builder as false and a
   mutation call raises `OrderRejectedError`.
8. Two paper sessions over the same tape both submit (no cid collision).
9. Concurrent duplicate reservation leaves `in_transaction` false and a
   subsequent write succeeds.
10. Replay returns an `OrderReceipt` with the original order id and status.
11. Failed event append raises, trips the kill switch, and releases the
    reservation.
12. Risk and dispatch durability failures release the reservation.
13. `TradingSession.stop()` calls `engine.shutdown()`; the guard is closed.
14. `TradingSession.start()` fails closed when a monitor cannot start.
15. Restart recovers orders/cash from the event log after the order mirror is
    deleted.
16. No foreign private writes; no imports of `tradex_shared` or
    `tradex_research`; no imports of deleted modules.

## 7. Acceptance Criteria

- All mandatory tests pass.
- Full pytest: zero failures, zero errors.
- Docker image builds and starts in PAPER mode.
- A live configuration with `live_orders_enabled=false` boots and refuses
  every submit; with `true` and explicit strategies it reaches the existing
  feed/reconciliation gates.
- No test writes outside `tmp_path`.
- The only persistence authority is `SQLiteEventStore`.

## 8. Rollback

The work is a branch with one commit per task. No database migration is
performed. If the branch is abandoned, the prior HEAD remains deployable only
in the sense that it was never deployable; there is no production cutover in
this work.

## 9. Out of Scope

Venue authentication, real-money validation, multi-process live service,
new strategy content, and improving the 85% aggregate coverage floor.
