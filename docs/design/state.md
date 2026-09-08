# TradeX v4 — Living Design State

**Purpose:** Single source for the platform's current design posture. Update after every architectural commit.
**Source of truth for:** invariants, known gaps, in-flight work, sequencing.
**Companion:** `docs/reviews/architecture-design-review-2026-08-29.md` is the *baseline* design review; this file is *current state*.

---

## Invariants (do not regress)

The 10 design invariants from the baseline review. If a change threatens one, escalate to Architecture Review.

1. One order path (`ExecutionEngine._run_pipeline`).
2. Money is `Decimal`.
3. Frozen domain (`@dataclass(frozen=True, slots=True)`).
4. Capability-loud adapters (`require_capability` + `Literal["supports_*"]`).
5. Single composition root (`runtime/startup.boot()`).
6. One resilience stack (`ProviderHttpClient` → `ResiliencePipeline`).
7. Fail-closed live gate (`confirm=True`, `_allow_order_operations`).
8. Parity evidence (CI parity gate).
9. Capability drift fails CI.
10. Single-writer live (`SingleWriterLock`).

---

## Done — Sprints 1 + 2 closed

Every item below has a test pin and a commit. Suite at completion: **2808 passed, 0 failed**.

### Sprint 1 — live safety (5/5 🔴)
- ✅ **C1** `d6f65ea` + `df61073` — `RiskManager.bind_cash_provider` + cash check; `RiskConfig.cash_provider`; bound at boot for paper/live.
- ✅ **C2** `29e527e` — `_run_startup_reconciliation` runs before `session.start()`; refuses to start on a *new* critical-drift trip.
- ✅ **C3** `e766fb1` — `_safe_teardown(session, broker, bus, writer_lock)` for both boot branches.
- ✅ **C4** `91ab3dd` — defensive tz check raises `ValueError` with a clear message on awareness mismatch.
- ✅ **C5 / G1p** `0254054` — per-subscriber `max_queue_size` gate (drop + `on_backpressure` + `bus.messages.dropped`); clean `bus.drain.exceeded` counter.

### Sprint 1 hardening (5/8 🟠)
- ✅ **H1** `ab0808b` — `MetricsRegistry` `_Counter` / `_Histogram` own a `threading.Lock`; `inc`, `observe`, `set`, `value()`, `count`, `min`, `max` all acquire.
- ✅ **H2** `dc5e0aa` — `MarketFeed._stream_lock` (RLock) on `_instruments` + `_depth_instruments`; new `snapshot_instruments()`.
- ✅ **H5** `ab0808b` — `modify()` re-runs `risk_manager.check()`; rolls back the OMS cache on rejection.
- ✅ **H7** `ab0808b` — `bus.of_type(ErrorOccurred)` subscriber in boot; logger + `engine.errors.total` counter.
- ✅ **H8** `ab0808b` — `CandleReceived` YAGNI-deleted from `events.py` and `__init__.py`.

### Baseline gap tests + behavior (3)
- ✅ **G2** `aeb7c7b` — `RiskBudget` per-strategy envelope; `RiskManager(budgets=...)`; `_active_budget(request)` resolves from `request.tag`.
- ✅ **G3** `0c9678e` — `_record_applied_fill()` fingerprint dedup; sync path records fingerprint pre-publish; the FILLED-status band-aid was a bug; **2 real bugs found and fixed** by the RED step.
- ✅ **H3** `9f95c2e` — `applied_fills_max` is an `__init__` parameter on `ExecutionEngine`; docstring on `_record_applied_fill` spells out the LRU cap.

### Sprint 2 — durability (7/7)
- ✅ **R1** `bb62e39` — `SQLEventLog`: durable SQLite append-only event log replacing the in-memory `deque(maxlen=10_000)`. `ReactiveBus.set_message_log` accepts the new log.
- ✅ **G5** `69c5aa9` — `ReactiveStrategyEngine._pending` indexed by `InstrumentId`; `pending_max_age_bars` cancels stale orders; `dispose_all` clears the dict.
- ✅ **H4** `fb6395f` — `SQLiteOrderStore.upsert_from_event(OrderFilled)`; live fill path writes to SQLite; `get_recent(limit)` for post-crash recovery.
- ✅ **H6** `75aed48` — `LiveFillBridge._engine_order_index: dict[OrderId, str]` for O(1) order lookup; populated from `OrderPlaced` events.
- ✅ **M1** `d00a3ea` — `FeeCalculator._calculate_legacy` deleted; canonical formula is the only path.
- ✅ **M2** `9f95c2e` — `cancel()` releases the idempotency reservation via `self._cid_for_order` side-table; risk-rejection path also releases.
- ✅ **H7** (already in hardening) — see above.

---

## Pending — what remains

### 2026-09-04 — Refactoring-plan Phase 1 (Safety: C1 + C2) — in working tree

Status: implemented in the uncommitted working tree (branch `refactor/execution-state`);
companion docs: `docs/design/tradex-target-architecture-and-refactoring-plan-2026-09-04.md`
and `docs/reviews/principal-architecture-review-2026-09-04.md`.

- ✅ **C1 end-to-end idempotency** — `frontend/src/trade-feed.ts` forwards
  `clientToken`/generated keys as `Idempotency-Key` on POST/PUT/DELETE;
  `routes/orders.py` requires the header (422 when absent) on place/modify/
  cancel/bracket and builds `OrderRequest.correlation_id`; `boot()` now always
  wires a guard (`MemoryIdempotencyGuard`, SQLite when a persistence path is
  set); the ACK-only submit path records the result durably so a client retry
  replays the original order id instead of double-submitting a live order
  (`engine.py` + `SQLiteIdempotencyGuard.result` column). Tests: route
  missing-key 422, same-key one-order, HTTP-retry dedupe, cross-engine SQLite
  replay, cancel-does-not-leak reservation.
- ✅ **C2 live mark-to-market** — new `execution/mark_to_market.py`
  (`MarkToMarketService`) subscribed to the canonical `Quote` lane;
  `Position` gains `mark_price/marked_at/mark_source`; `position_math`/
  `position_manager` preserve or reset marks; `RiskManager` gains
  `require_fresh_marks`/`max_mark_age_seconds` fail-closed gate (opening
  exposure needs fresh bid/ask/LTP marks; reductions stay free); `boot()`
  enables the gate for `mode=live`, binds the cache quote provider, and wires
  the service into the session lifecycle; `PositionUpdated` events drive
  `position` frames with mark fields on `/ws/stream`; `/positions` API
  serializes mark metadata + `mark_stale`.
- ✅ Phase-1 repair pass — fixed a mangled `stream.py` edit (orphaned
  `_send_fill` body + dropped `bus = session.bus`), replaced a
  foreign-private-write in `startup.py` with `PaperFillSource.bind_cache()`
  (repo meta test `test_no_foreign_private_writes` green), aligned stale
  interface tests with the mandatory-key contract, and fixed pre-existing
  frontend typecheck debt (`feed.ts`, `trade.ts`, `http.test.ts`, test files).
- ✅ Frontend typecheck (`tsc --noEmit`) clean; vitest 144 passed.
- ✅ Suite at working tree: domain 299, brokers 770+2 skipped, trading 1901,
  repo meta 4 — **2974 passed, 2 skipped**.

### Sprint 0 — pre-flight (CLOSED)
- ✅ **G17** `ec6c288` + `fd57963` — root `pyproject.toml` is a uv workspace manifest with `[tool.uv.workspace]` + `[tool.uv.sources]` for the three members and a `[dependency-groups].dev` listing ruff, mypy, pytest, pytest-cov, pytest-timeout, and the three workspace members. `uv lock` regenerates the lockfile (1393 lines). `uv sync --frozen` is now idempotent. `tradex-trading[datalake,api,full]` is requested so the parity and replay tests can collect. mypy on the domain kernel is clean.

### Sprint 3 — correctness at scale
- ✅ **G4** `3744211` — `BaseBroker` pass-throughs replaced by a generated wall; 242-line contract test pins it.
- ✅ **G6** `989ff2c` — first-class `IndicatorRegistry` (`analytics/registry.py`) with decorator self-registration; goldens co-located under `analytics/goldens/`; `AnalyticsEngine` resolves via `REGISTRY.get(name).compute(...)` (if/elif selector deleted).
- ✅ **G8** (2026-08-30, working tree) — SDK service layer deleted: `sdk/services/` (portfolio/scanner/stream/trade + 871-line service test suite) removed; routes/CLI call `session.engine.*` / `session.broker.*` / `session.engine.cache.*` directly; `sdk/streaming.py` subscription handles kept (still used by the WS route + session stop). Export surface now `TradingSession`, `SessionState`, `StreamSubscription`.
- ✅ **M3–M7** (`6634851`) — bus dispose guard, broker close dedup, `_session_date` type, `_make_order` id validation, writer lock scoped to `RuntimeContext`. Five fixes; full suite 2806 passed.
- 🟢 **G15** Update `docs/ARCHITECTURE.md` after G10 lands. **Status:** unstarted (depends on G10).
- 🟢 **G16** `tests/test_import_boundaries.py` — keep, update when G10 lands. **Status:** unstarted (depends on G10).

### Sprint 4 — design hygiene (deferrable)
- 🟡 **G10** `trading/` is 25.6k LOC across 10 modules. Split into `trading_core / _datalake / _strategy / _runtime / _interface / _sdk`. **Status:** unstarted; *only when team size justifies.*
- 🟡 **G11** `analytics/` flat dir of 30 modules. Sub-folders by family. **Status:** unstarted.
- 🟡 **G12** `replay/` mixes driver, walk-forward, optimization, synthetic ticks. **Status:** unstarted.
- ✅ **M3** (cross-listed under Sprint 3) — done.
- ✅ **M4** (cross-listed under Sprint 3) — done.
- ✅ **M5** (cross-listed under Sprint 3) — done.
- ✅ **M6** (cross-listed under Sprint 3) — done.

### Sprint 5 — observability
- ✅ **R2** (2026-08-30, working tree) — bus partitioned into lanes (`order` / `market` / `diagnostics` / `default`) — internal per-lane Subjects, routed by event class name; public `publish` / `subscribe` / `of_type` / `stream` / `dispose` API unchanged, no subscriber edits; +15 lane-isolation tests.
- ✅ **R3** (2026-08-30) — closed by G6: `IndicatorSpec` is the frozen dataclass contract in `analytics/registry.py`; the redundant `register_legacy_spec` bridge loop in `indicators.py` deleted — `register_indicator` is the single registration path (mirrors into legacy catalogue + first-class registry).
- ✅ **G14** (2026-08-30, working tree) — `MetricsRegistry.render_prometheus()` emits Prometheus text exposition (stdlib only, no client lib); `GET /metrics` on the FastAPI app; boot-time registry carried on `TradingSession.metrics` so served apps expose real runtime counters.

### Backlog
- ✅ **G7** (2026-08-30, working tree) — `ScannerEngine` streaming consumer: per-instrument `deque(maxlen=max_bars)` rolling buffer fed by `consume(candle)`; `_history()` falls back to a snapshot only before any bar streams; wired via a `bus.of_type(Candle).subscribe(...)` in `startup.py`; +5 tests.
- 🟠 **G9** `services/duckdb-analytics` re-implements `ScannerEngine` with no shared contract. **Status (2026-08-30):** decision = **delete** (untracked, not a uv workspace member, sole consumer is the scratch `candles_app.py`; nothing tracked imports either). Physical removal is pending — the delete is blocked by tool security policy; run `rm -rf services/duckdb-analytics candles_app.py` manually to close.
- ✅ **G13** (2026-08-30, working tree) — typed-error contract: `{"error": {"code", "message"}}` envelope via one FastAPI exception handler (status→stable code map) + `ErrorDetail` model; frontend `frontend/src/http.ts` `expectJson()` replaces all 11 `if (!resp.ok) throw` sites; +10 contract tests; `tsc --noEmit` clean.

---

## Process rules (from loaded skills)

From `using-superpowers`: invoke a relevant skill *before* any non-trivial action.
From `ponytail` (full): the ladder; bug fix = root cause; mark shortcuts with `ponytail:`.
From `test-driven-development`: red → verify red → green minimal → refactor.
From `tradexv2-org`: architecture first, then contracts, then tests, then implementation; TDD; no partial architectural branches.

---

## Update log

- 2026-08-29 — design state initialized; baseline = `docs/reviews/architecture-design-review-2026-08-29.md`.
- 2026-08-29 — Sprint 1 revised: principal-architect review surfaced 5 new 🔴 (C1–C5) and 8 🟠 (H1–H8) findings.
- 2026-08-29 — C1 GREEN (`d6f65ea`); C1 follow-up (`df61073`); C2 (`29e527e`); C3 (`e766fb1`); C4 (`91ab3dd`); C5 (`0254054`). All 5 Sprint-1 criticals closed.
- 2026-08-29 — H1+H5+H7+H8 batch (`ab0808b`); H2 (`dc5e0aa`); G3 (`0c9678e`); G2 (`aeb7c7b`).
- 2026-08-29 — Sprint 2 closed via parallel agent team: R1 (`bb62e39`), M1 (`d00a3ea`), H4 (`fb6395f`), H6 (`75aed48`), H3+M2 (`9f95c2e`), G5 (`69c5aa9`). 7/7 items done. Suite 2808 passed, 0 failed.
- 2026-08-29 — G17 CI pre-flight closed (`ec6c288` + `fd57963`): root `pyproject.toml` now a uv workspace manifest with `[tool.uv.workspace]`, `[tool.uv.sources]`, and `[dependency-groups].dev` listing the CI tooling. `uv lock` regenerates a 1393-line `uv.lock`; `uv sync --frozen` is idempotent. Mypy on the domain kernel is clean. CI is now verifiable end-to-end.
- 2026-08-29 — M3-M7 hardening batch (`6634851`): bus dispose guard, broker close dedup, `_session_date` type, `_make_order` id validation, writer lock scoped to `RuntimeContext`. Trading `[datalake]` extras now declare pandas + pyarrow. Per-package suite: domain 299, brokers 765, trading 1738, meta 4 — total 2806 passed (vs. prior 2808). **M3, M4, M5, M6, M7 all closed.**
- 2026-08-29 — state file consolidated: every closed item moved to a single "Done" section with commit hashes; per-item RED/GREEN test plan sections removed (they were stale). Sprint 3 / 4 / 5 / Backlog pending items preserved.
- 2026-08-30 — G4 (`3744211`) and G6 (`989ff2c`) landed via committed refactors. Then a two-wave parallel batch closed the rest in the working tree (uncommitted): **G8** SDK service layer deleted; **G14** Prometheus exposition on `/metrics`; **R3** legacy registry bridge removed; **R2** bus lane partition; **G7** scanner streaming consumer; **G13** typed-error contract end-to-end; plus integration repairs (2 stale POST-order tests fixed, missing `def` in a risk-gap test restored, `make_verify_api_key` / `api_key_header` dupes deleted, `/openapi.json` regression fixed). Per-package suite: domain 299, brokers 770, trading 1713 — **2782 passed, 0 failed**. Net diff −800 lines across 61 files. Remaining: G9 (keep-or-delete decision), Sprint 4 deferrables (G10–G12), G15/G16 (depend on G10).
